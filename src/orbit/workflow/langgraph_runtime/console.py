"""What a Handler's process printed, kept for the operator to read.

The one store in this adapter that is not a record. A subprocess's console is
an observation: non-deterministic, possibly truncated, and never something a
replay may read. It is written outside every transaction on purpose — the
opposite discipline to the run event log next door, which is appended inside
the transaction that changed the run precisely so it cannot disagree with it.
Here a full disk or a slow write must delay nobody's run and must never fail
an attempt that actually succeeded, so every failure is swallowed.

Two things make it worth keeping anyway: an Agent that runs for minutes is
otherwise a black box, and an attempt that ends `unknown` reports nothing at
all, so its console is the only account of what happened.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any


STREAMS = ("stdout", "stderr")
# Per attempt and per stream. The Agent clients already bound what they read
# from the pipes; this is the second, independent bound on what is stored, so
# a chatty CLI cannot grow the database without limit.
DEFAULT_MAX_BYTES = 262_144


class AttemptConsole:
    """Append-and-tail storage for Handler console chunks."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS langgraph_attempt_output("
                "chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "run_id TEXT NOT NULL,node_id TEXT NOT NULL,"
                "attempt_id TEXT NOT NULL,stream TEXT NOT NULL,"
                "text TEXT NOT NULL,created_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS langgraph_attempt_output_by_run"
                " ON langgraph_attempt_output(run_id, chunk_id)"
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def append(
        self, *, run_id: str, node_id: str, attempt_id: str, stream: str,
        text: str, now: str,
    ) -> None:
        if stream not in STREAMS or not text:
            return
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO langgraph_attempt_output"
                "(run_id,node_id,attempt_id,stream,text,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (run_id, node_id, attempt_id, stream, text, now),
            )
            connection.commit()

    def read(
        self, run_id: str, *, after_chunk_id: int = 0, limit: int = 200,
        node_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Chunks in the order they were printed, plus where to resume.

        A console is followed rather than paged: the caller asks "what is new
        since N", so the cursor it gets back is the last chunk it was given,
        not an opaque token, and `has_more` says whether to ask again now
        instead of waiting.
        """

        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        clauses = ["run_id = ?", "chunk_id > ?"]
        parameters: list[Any] = [run_id, int(after_chunk_id)]
        if node_id:
            clauses.append("node_id = ?")
            parameters.append(node_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT chunk_id,node_id,attempt_id,stream,text,created_at"
                f" FROM langgraph_attempt_output WHERE {' AND '.join(clauses)}"
                " ORDER BY chunk_id LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        chunks = [
            {
                "chunk_id": int(row["chunk_id"]),
                "node_id": row["node_id"],
                "attempt_id": row["attempt_id"],
                "stream": row["stream"],
                "text": row["text"],
                "created_at": row["created_at"],
            }
            for row in rows[:limit]
        ]
        after = chunks[-1]["chunk_id"] if chunks else int(after_chunk_id)
        return chunks, after, has_more

    def stored_bytes(self, attempt_id: str, stream: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(LENGTH(text)), 0) AS total"
                " FROM langgraph_attempt_output"
                " WHERE attempt_id = ? AND stream = ?",
                (attempt_id, stream),
            ).fetchone()
        return int(row["total"])


class AttemptConsoleSink:
    """The port a Handler sees: one attempt, bounded, never raising.

    A Handler that cannot print is still a Handler that ran. Every failure
    here is swallowed deliberately — the alternative is failing a real attempt
    because its console could not be saved.
    """

    def __init__(
        self, console: AttemptConsole, *, run_id: str, node_id: str,
        attempt_id: str, max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.console = console
        self.run_id = run_id
        self.node_id = node_id
        self.attempt_id = attempt_id
        self.max_bytes = max_bytes
        self._written = {stream: 0 for stream in STREAMS}

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def emit(self, stream: str, text: str) -> None:
        if stream not in STREAMS or not text:
            return
        remaining = self.max_bytes - self._written[stream]
        if remaining <= 0:
            return
        encoded = text.encode("utf-8")
        truncated = False
        if len(encoded) > remaining:
            text = encoded[:remaining].decode("utf-8", errors="ignore")
            truncated = True
        self._written[stream] += len(text.encode("utf-8"))
        try:
            self.console.append(
                run_id=self.run_id, node_id=self.node_id,
                attempt_id=self.attempt_id, stream=stream, text=text,
                now=self._now(),
            )
            if truncated:
                # Said once, in the stream it happened to, so a reader is not
                # left thinking the Agent simply stopped talking.
                self._written[stream] = self.max_bytes
                self.console.append(
                    run_id=self.run_id, node_id=self.node_id,
                    attempt_id=self.attempt_id, stream=stream,
                    text=f"\n… output truncated at {self.max_bytes} bytes\n",
                    now=self._now(),
                )
        except Exception:  # noqa: BLE001 - see the class docstring
            return
