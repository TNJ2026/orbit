"""Background WebSocket listener and durable local event Inbox."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class EventInbox:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._condition = threading.Condition()
        self._revision = 0
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS app_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    acknowledged_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_app_events_pending
                    ON app_events(acknowledged_at, received_at);
                CREATE TABLE IF NOT EXISTS app_event_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def cursor(self) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT value FROM app_event_state WHERE key = 'cursor'"
            ).fetchone()
            return None if row is None else str(row["value"])

    def accept(self, frame: Mapping[str, Any]) -> bool:
        frame_type = frame.get("type")
        cursor = frame.get("cursor")
        inserted = False
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if frame_type == "runtime_event":
                    event_id = frame.get("event_id")
                    event_type = frame.get("event_type")
                    if not isinstance(event_id, str) or not isinstance(event_type, str):
                        raise ValueError("Runtime event frame is missing its identity")
                    result = connection.execute(
                        """
                        INSERT OR IGNORE INTO app_events(
                            event_id, event_type, payload_json, received_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            event_id, event_type,
                            json.dumps(frame, ensure_ascii=False, sort_keys=True),
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                    inserted = result.rowcount == 1
                if isinstance(cursor, str) and cursor:
                    connection.execute(
                        """
                        INSERT INTO app_event_state(key, value) VALUES ('cursor', ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        (cursor,),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if inserted:
            with self._condition:
                self._revision += 1
                self._condition.notify_all()
        return inserted

    def pending(
        self, *, limit: int = 50, event_types: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
        selected = tuple(dict.fromkeys(str(item) for item in event_types if str(item)))
        sql = "SELECT payload_json FROM app_events WHERE acknowledged_at IS NULL"
        parameters: list[Any] = []
        if selected:
            placeholders = ",".join("?" for _ in selected)
            sql += f" AND event_type IN ({placeholders})"
            parameters.extend(selected)
        sql += " ORDER BY received_at, event_id LIMIT ?"
        parameters.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def wait(
        self, *, timeout_seconds: float, event_types: Iterable[str] = (),
    ) -> dict[str, Any] | None:
        if timeout_seconds < 0 or timeout_seconds > 300:
            raise ValueError("timeout_seconds must be between 0 and 300")
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            observed_revision = self._revision
        while True:
            events = self.pending(limit=1, event_types=event_types)
            if events:
                return events[0]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            with self._condition:
                # An accept between pending() and acquiring this lock already
                # changed the revision. Re-query instead of missing its notify
                # and sleeping until the caller's full timeout.
                if self._revision != observed_revision:
                    observed_revision = self._revision
                    continue
                self._condition.wait(timeout=remaining)
                observed_revision = self._revision

    def acknowledge(self, event_id: str) -> bool:
        with closing(self._connect()) as connection:
            result = connection.execute(
                """
                UPDATE app_events SET acknowledged_at = ?
                WHERE event_id = ? AND acknowledged_at IS NULL
                """,
                (datetime.now(timezone.utc).isoformat(), event_id),
            )
        return result.rowcount == 1


def _resume_url(url: str, cursor: str | None) -> str:
    if cursor is None:
        return url
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("cursor", cursor))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


class AgentAppEventBridge:
    def __init__(
        self,
        url: str,
        inbox: EventInbox,
        *,
        connector: Callable[..., Any] | None = None,
        reconnect_seconds: float = 1.0,
    ) -> None:
        self.url = url
        self.inbox = inbox
        self.connector = connector or self._connect
        self.reconnect_seconds = reconnect_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    @staticmethod
    def _connect(url: str):
        from websockets.sync.client import connect

        return connect(url, open_timeout=5, close_timeout=1)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("event bridge already started")
        self._thread = threading.Thread(
            target=self._run, name="agent-app-events", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self.connector(_resume_url(self.url, self.inbox.cursor())) as socket:
                    self.last_error = None
                    while not self._stop.is_set():
                        try:
                            raw = socket.recv(timeout=0.5)
                        except TimeoutError:
                            continue
                        frame = json.loads(raw)
                        if isinstance(frame, Mapping):
                            self.inbox.accept(frame)
            except Exception as exc:
                self.last_error = str(exc)
                self._stop.wait(self.reconnect_seconds)
