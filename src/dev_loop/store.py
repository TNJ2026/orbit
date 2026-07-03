"""SQLite-backed store for agents and messages."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path.home() / ".dev_loop" / "messages.db"
DEFAULT_LEASE_SECONDS = 300

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    name          TEXT PRIMARY KEY,
    description   TEXT NOT NULL DEFAULT '',
    registered_at TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sender     TEXT NOT NULL,
    recipient  TEXT NOT NULL,
    content    TEXT NOT NULL,
    reply_to   INTEGER,
    created_at TEXT NOT NULL,
    read_at    TEXT,
    leased_until TEXT,
    lease_owner TEXT,
    lease_token TEXT,
    delivery_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_unread
    ON messages (recipient, read_at);
CREATE INDEX IF NOT EXISTS idx_messages_available
    ON messages (recipient, read_at, leased_until);
CREATE INDEX IF NOT EXISTS idx_messages_reply_to
    ON messages (reply_to);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )


class UnknownAgentError(ValueError):
    pass


class Store:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "leased_until" not in columns:
            self._conn.execute("ALTER TABLE messages ADD COLUMN leased_until TEXT")
        if "lease_owner" not in columns:
            self._conn.execute("ALTER TABLE messages ADD COLUMN lease_owner TEXT")
        if "lease_token" not in columns:
            self._conn.execute("ALTER TABLE messages ADD COLUMN lease_token TEXT")
        if "delivery_count" not in columns:
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN delivery_count INTEGER NOT NULL DEFAULT 0"
            )

    # -- agents ------------------------------------------------------------

    def register_agent(self, name: str, description: str) -> list[dict[str, Any]]:
        now = _now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO agents (name, description, registered_at, last_seen)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       description = excluded.description,
                       last_seen = excluded.last_seen""",
                (name, description, now, now),
            )
            self._conn.commit()
        return self.list_agents()

    def touch_agent(self, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE agents SET last_seen = ? WHERE name = ?", (_now(), name)
            )
            self._conn.commit()

    def list_agents(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, description, registered_at, last_seen FROM agents ORDER BY name"
            ).fetchall()
        return [dict(r) for r in rows]

    def agent_names(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT name FROM agents").fetchall()
        return [r["name"] for r in rows]

    def agent_exists(self, name: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM agents WHERE name = ?", (name,)
            ).fetchone()
        return row is not None

    # -- messages ----------------------------------------------------------

    def send_message(
        self, sender: str, to: str, content: str, reply_to: int | None = None
    ) -> list[int]:
        """Insert a message. Broadcast ("*") is expanded into one copy per
        registered agent other than the sender. Returns the message id(s)."""
        now = _now()
        ids: list[int] = []
        with self._lock:
            if not self._agent_exists_locked(sender):
                raise UnknownAgentError(f"unknown sender: {sender}")
            if to == "*":
                rows = self._conn.execute(
                    "SELECT name FROM agents WHERE name != ? ORDER BY name", (sender,)
                ).fetchall()
                recipients = [r["name"] for r in rows]
            else:
                if not self._agent_exists_locked(to):
                    raise UnknownAgentError(f"unknown recipient: {to}")
                recipients = [to]
            for recipient in recipients:
                cur = self._conn.execute(
                    """INSERT INTO messages (sender, recipient, content, reply_to, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (sender, recipient, content, reply_to, now),
                )
                ids.append(cur.lastrowid)
            self._conn.commit()
        return ids

    def fetch_unread(
        self, agent: str, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> list[dict[str, Any]]:
        """Lease unread messages for `agent`.

        Leased messages are invisible to later fetches until they are acked or the
        lease expires. This keeps a consumer crash from losing messages forever.
        """
        now = _now()
        lease_seconds = max(1, int(lease_seconds))
        leased_until = _future(lease_seconds)
        with self._lock:
            if not self._agent_exists_locked(agent):
                raise UnknownAgentError(f"unknown agent: {agent}")
            rows = self._conn.execute(
                """SELECT id, sender, recipient, content, reply_to, created_at,
                          delivery_count
                   FROM messages
                   WHERE recipient = ?
                     AND read_at IS NULL
                     AND (leased_until IS NULL OR leased_until <= ?)
                   ORDER BY id""",
                (agent, now),
            ).fetchall()
            tokens = {r["id"]: uuid.uuid4().hex for r in rows}
            if rows:
                self._conn.executemany(
                    """UPDATE messages
                       SET leased_until = ?,
                           lease_owner = ?,
                           lease_token = ?,
                           delivery_count = delivery_count + 1
                       WHERE id = ?""",
                    [(leased_until, agent, tokens[r["id"]], r["id"]) for r in rows],
                )
            self._conn.commit()
        messages = []
        for row in rows:
            message = dict(row)
            message["delivery_count"] += 1
            message["lease_expires_at"] = leased_until
            message["lease_token"] = tokens[row["id"]]
            messages.append(message)
        return messages

    def ack_message(self, agent: str, message_id: int, lease_token: str) -> bool:
        now = _now()
        with self._lock:
            if not self._agent_exists_locked(agent):
                raise UnknownAgentError(f"unknown agent: {agent}")
            cur = self._conn.execute(
                """UPDATE messages
                   SET read_at = ?,
                       leased_until = NULL,
                       lease_owner = NULL,
                       lease_token = NULL
                   WHERE id = ?
                     AND recipient = ?
                     AND lease_token = ?
                     AND read_at IS NULL""",
                (now, message_id, agent, lease_token),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def has_unread(self, agent: str) -> bool:
        now = _now()
        with self._lock:
            row = self._conn.execute(
                """SELECT 1 FROM messages
                   WHERE recipient = ?
                     AND read_at IS NULL
                     AND (leased_until IS NULL OR leased_until <= ?)
                   LIMIT 1""",
                (agent, now),
            ).fetchone()
        return row is not None

    def get_message(self, message_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT id, sender, recipient, content, reply_to, created_at, read_at,
                          leased_until, lease_owner, lease_token, delivery_count
                   FROM messages WHERE id = ?""",
                (message_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_thread(self, message_id: int) -> list[dict[str, Any]]:
        """Return the ancestor chain plus all descendants using recursive SQL."""
        with self._lock:
            rows = self._conn.execute(
                """WITH RECURSIVE
                   ancestors(id, sender, recipient, content, reply_to, created_at,
                             read_at, leased_until, lease_owner, lease_token,
                             delivery_count) AS (
                       SELECT id, sender, recipient, content, reply_to, created_at,
                              read_at, leased_until, lease_owner, lease_token,
                              delivery_count
                       FROM messages
                       WHERE id = ?
                       UNION
                       SELECT m.id, m.sender, m.recipient, m.content, m.reply_to,
                              m.created_at, m.read_at, m.leased_until, m.lease_owner,
                              m.lease_token, m.delivery_count
                       FROM messages m
                       JOIN ancestors a ON m.id = a.reply_to
                   ),
                   thread(id, sender, recipient, content, reply_to, created_at,
                          read_at, leased_until, lease_owner, lease_token,
                          delivery_count) AS (
                       SELECT id, sender, recipient, content, reply_to, created_at,
                              read_at, leased_until, lease_owner, lease_token,
                              delivery_count
                       FROM ancestors
                       UNION
                       SELECT m.id, m.sender, m.recipient, m.content, m.reply_to,
                              m.created_at, m.read_at, m.leased_until, m.lease_owner,
                              m.lease_token, m.delivery_count
                       FROM messages m
                       JOIN thread t ON m.reply_to = t.id
                   )
                   SELECT DISTINCT id, sender, recipient, content, reply_to,
                          created_at, read_at, leased_until, lease_owner,
                          lease_token, delivery_count
                   FROM thread
                   ORDER BY id""",
                (message_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _agent_exists_locked(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM agents WHERE name = ?", (name,)
        ).fetchone()
        return row is not None
