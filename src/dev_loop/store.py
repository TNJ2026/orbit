"""SQLite-backed store for agents and messages."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB_ROOT = Path.home() / ".dev_loop" / "projects"
DEFAULT_LEASE_SECONDS = 300
MESSAGE_KINDS = {"message", "task"}
TASK_STATUSES = {
    "",
    "created",
    "assigned",
    "in_progress",
    "testing",
    "bugfixing",
    "replied",
    "accepted",
    "needs_changes",
    "blocked",
    "closed",
}
# Must stay in sync with the scoring tables in server.py — an off-list value
# would silently score as the default there.
TASK_IMPORTANCE_LEVELS = {"low", "normal", "high", "critical"}
TASK_SIZES = {"small", "medium", "large"}
TASK_RISKS = {"low", "medium", "high"}

_TABLE_SCHEMA = """
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
    kind       TEXT NOT NULL DEFAULT 'message',
    title      TEXT NOT NULL DEFAULT '',
    task_status TEXT NOT NULL DEFAULT '',
    reply_to   INTEGER,
    created_at TEXT NOT NULL,
    read_at    TEXT,
    leased_until TEXT,
    lease_owner TEXT,
    lease_token TEXT,
    delivery_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tasks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_message_id INTEGER UNIQUE,
    title             TEXT NOT NULL DEFAULT '',
    content           TEXT NOT NULL,
    sender            TEXT NOT NULL,
    assignee          TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'created',
    role_required     TEXT NOT NULL DEFAULT 'implementer',
    importance        TEXT NOT NULL DEFAULT 'normal',
    size              TEXT NOT NULL DEFAULT 'medium',
    risk              TEXT NOT NULL DEFAULT 'medium',
    required_capabilities TEXT NOT NULL DEFAULT '',
    exclusive_workspace INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_transitions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL,
    from_step  TEXT NOT NULL DEFAULT '',
    to_step    TEXT NOT NULL DEFAULT '',
    actor      TEXT NOT NULL DEFAULT '',
    outcome    TEXT NOT NULL DEFAULT 'done',
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);
CREATE TABLE IF NOT EXISTS task_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL,
    attempt     INTEGER NOT NULL,
    worker      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'running',
    exit_code   INTEGER,
    log_dir     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    UNIQUE(task_id, attempt)
);
"""

_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_messages_unread
    ON messages (recipient, read_at);
CREATE INDEX IF NOT EXISTS idx_messages_available
    ON messages (recipient, read_at, leased_until);
CREATE INDEX IF NOT EXISTS idx_messages_reply_to
    ON messages (reply_to);
CREATE INDEX IF NOT EXISTS idx_tasks_status
    ON tasks (status);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee
    ON tasks (assignee);
CREATE INDEX IF NOT EXISTS idx_task_runs_task
    ON task_runs (task_id, attempt);
CREATE INDEX IF NOT EXISTS idx_task_transitions_task
    ON task_transitions (task_id, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )


def resolve_project_root(project_dir: Path | str | None = None) -> Path:
    """Walk up from `start` to the nearest directory containing a project
    marker (.git or pyproject.toml), so launching the daemon from a
    subdirectory resolves to the same database as launching from the root."""
    start = (
        Path.cwd().resolve()
        if project_dir is None
        else Path(project_dir).expanduser().resolve()
    )
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists() or (candidate / "pyproject.toml").exists():
            return candidate
    return start


def project_db_path(
    project_dir: Path | str | None = None,
    base_dir: Path | str | None = None,
) -> Path:
    """Return the default database path for a project directory.

    The path is stable for the absolute project path and includes a short hash so
    projects with the same leaf directory name do not collide. When project_dir
    is not given, the project root is detected from the current directory.
    """
    project_path = resolve_project_root(project_dir)
    root = Path(base_dir or DEFAULT_DB_ROOT).expanduser()
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", project_path.name).strip("-._")
    if not slug:
        slug = "project"
    digest = hashlib.sha256(str(project_path).encode("utf-8")).hexdigest()[:12]
    return root / f"{slug}-{digest}" / "messages.db"


class UnknownAgentError(ValueError):
    pass


class InvalidInputError(ValueError):
    pass


def _validate_kind(kind: str) -> str:
    kind = (kind or "message").strip()
    if kind not in MESSAGE_KINDS:
        raise InvalidInputError(
            f"invalid kind: {kind!r} (expected one of {sorted(MESSAGE_KINDS)})"
        )
    return kind


def _validate_task_status(task_status: str) -> str:
    task_status = (task_status or "").strip()
    if task_status not in TASK_STATUSES:
        raise InvalidInputError(
            f"invalid task_status: {task_status!r} "
            f"(expected one of {sorted(s for s in TASK_STATUSES if s)})"
        )
    return task_status


def _validate_agent_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise InvalidInputError("agent name must not be empty")
    if name == "*":
        raise InvalidInputError('agent name "*" is reserved for broadcast')
    return name


def _encode_capabilities(capabilities: list[str] | str) -> str:
    if isinstance(capabilities, str):
        parts = [part.strip() for part in capabilities.split(",")]
    else:
        parts = [str(part).strip() for part in capabilities]
    return ",".join(part for part in parts if part)


def _decode_capabilities(capabilities: str) -> list[str]:
    return [part.strip() for part in (capabilities or "").split(",") if part.strip()]


class Store:
    def __init__(self, db_path: Path | str | None = None):
        if db_path is None:
            db_path = project_db_path()
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.executescript(_TABLE_SCHEMA)
            self._migrate()
            self._conn.executescript(_INDEX_SCHEMA)
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
        if "kind" not in columns:
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'message'"
            )
        if "title" not in columns:
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN title TEXT NOT NULL DEFAULT ''"
            )
        if "task_status" not in columns:
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN task_status TEXT NOT NULL DEFAULT ''"
            )
        task_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "source_message_id" not in task_columns:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN source_message_id INTEGER")
        task_defaults = {
            "role_required": "TEXT NOT NULL DEFAULT 'implementer'",
            "importance": "TEXT NOT NULL DEFAULT 'normal'",
            "size": "TEXT NOT NULL DEFAULT 'medium'",
            "risk": "TEXT NOT NULL DEFAULT 'medium'",
            "required_capabilities": "TEXT NOT NULL DEFAULT ''",
            "exclusive_workspace": "INTEGER NOT NULL DEFAULT 1",
            "workflow_step": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in task_defaults.items():
            if column not in task_columns:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
        self._backfill_tasks_from_messages()

    def _backfill_tasks_from_messages(self) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO tasks (
                   source_message_id, title, content, sender, assignee,
                   status, created_at, updated_at
               )
               SELECT id, title, content, sender, recipient,
                      CASE WHEN task_status = '' THEN 'created' ELSE task_status END,
                      created_at, created_at
               FROM messages
               WHERE kind = 'task'"""
        )

    # -- agents ------------------------------------------------------------

    def register_agent(self, name: str, description: str) -> list[dict[str, Any]]:
        name = _validate_agent_name(name)
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
        self,
        sender: str,
        to: str,
        content: str,
        reply_to: int | None = None,
        kind: str = "message",
        title: str = "",
        task_status: str = "",
    ) -> list[int]:
        """Insert a message. Broadcast ("*") is expanded into one copy per
        registered agent other than the sender. Returns the message id(s)."""
        now = _now()
        kind = _validate_kind(kind)
        title = title.strip()
        if kind == "task" and not (task_status or "").strip():
            task_status = "created"
        task_status = _validate_task_status(task_status)
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
                    """INSERT INTO messages (
                           sender, recipient, content, kind, title, task_status,
                           reply_to, created_at
                       )
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (sender, recipient, content, kind, title, task_status, reply_to, now),
                )
                message_id = cur.lastrowid
                ids.append(message_id)
                if kind == "task":
                    self._conn.execute(
                        """INSERT OR IGNORE INTO tasks (
                               source_message_id, title, content, sender, assignee,
                               status, created_at, updated_at
                           )
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            message_id,
                            title,
                            content,
                            sender,
                            recipient,
                            task_status,
                            now,
                            now,
                        ),
                    )
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
                """SELECT id, sender, recipient, content, kind, title, task_status,
                          reply_to, created_at, delivery_count
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
                """SELECT id, sender, recipient, content, kind, title, task_status,
                          reply_to, created_at, read_at, leased_until, lease_owner,
                          lease_token, delivery_count
                   FROM messages WHERE id = ?""",
                (message_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_task_status(self, message_id: int, task_status: str) -> bool:
        task_status = _validate_task_status(task_status)
        if not task_status:
            raise InvalidInputError("task_status must not be empty for an update")
        with self._lock:
            cur = self._conn.execute(
                """UPDATE messages
                   SET task_status = ?
                   WHERE id = ? AND kind = 'task'""",
                (task_status, message_id),
            )
            if cur.rowcount:
                self._conn.execute(
                    """UPDATE tasks
                       SET status = ?, updated_at = ?
                       WHERE source_message_id = ?""",
                    (task_status, _now(), message_id),
                )
            self._conn.commit()
        return cur.rowcount > 0

    def list_tasks(
        self,
        status: str = "all",
        assignee: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return executable tasks for the Kanban UI."""
        limit = max(1, min(int(limit), 500))
        filters = []
        params: list[Any] = []
        if status != "all":
            filters.append("status = ?")
            params.append(_validate_task_status(status))
        if assignee:
            filters.append("assignee = ?")
            params.append(assignee)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        query = f"""SELECT id, source_message_id, title, content, sender,
                           assignee, assignee AS recipient, status AS task_status,
                           created_at, updated_at,
                           role_required, importance, size, risk,
                           required_capabilities, exclusive_workspace,
                           workflow_step
                    FROM tasks
                    {where}
                    ORDER BY id DESC
                    LIMIT ?"""
        with self._lock:
            rows = self._conn.execute(query, [*params, limit]).fetchall()
        return [self._task_row(row) for row in rows]

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT id, source_message_id, title, content, sender,
                          assignee, assignee AS recipient, status AS task_status,
                          created_at, updated_at, role_required, importance, size,
                          risk, required_capabilities, exclusive_workspace,
                          workflow_step
                   FROM tasks
                   WHERE id = ?""",
                (task_id,),
            ).fetchone()
        return self._task_row(row) if row else None

    def update_task_metadata(
        self,
        task_id: int,
        role_required: str | None = None,
        importance: str | None = None,
        size: str | None = None,
        risk: str | None = None,
        required_capabilities: list[str] | str | None = None,
        exclusive_workspace: bool | None = None,
    ) -> dict[str, Any] | None:
        updates: list[str] = []
        params: list[Any] = []
        for column, value, allowed in (
            ("role_required", role_required, None),
            ("importance", importance, TASK_IMPORTANCE_LEVELS),
            ("size", size, TASK_SIZES),
            ("risk", risk, TASK_RISKS),
        ):
            if value is None:
                continue
            value = str(value).strip()
            if allowed is not None and value not in allowed:
                raise InvalidInputError(
                    f"invalid {column}: {value!r} (expected one of {sorted(allowed)})"
                )
            if column == "role_required" and not value:
                raise InvalidInputError("role_required must not be empty")
            updates.append(f"{column} = ?")
            params.append(value)
        if required_capabilities is not None:
            updates.append("required_capabilities = ?")
            params.append(_encode_capabilities(required_capabilities))
        if exclusive_workspace is not None:
            updates.append("exclusive_workspace = ?")
            params.append(1 if exclusive_workspace else 0)
        if not updates:
            return self.get_task(task_id)
        updates.append("updated_at = ?")
        params.append(_now())
        params.append(task_id)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            self._conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_task(task_id)

    def update_task_item_status(self, task_id: int, task_status: str) -> bool:
        task_status = _validate_task_status(task_status)
        if not task_status:
            raise InvalidInputError("task_status must not be empty for an update")
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (task_status, now, task_id),
            )
            row = self._conn.execute(
                "SELECT source_message_id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row and row["source_message_id"] is not None:
                self._conn.execute(
                    "UPDATE messages SET task_status = ? WHERE id = ? AND kind = 'task'",
                    (task_status, row["source_message_id"]),
                )
            self._conn.commit()
        return cur.rowcount > 0

    def set_task_workflow_state(
        self,
        task_id: int,
        workflow_step: str | None = None,
        task_status: str | None = None,
        assignee: str | None = None,
    ) -> bool:
        updates: list[str] = []
        params: list[Any] = []
        if workflow_step is not None:
            updates.append("workflow_step = ?")
            params.append(workflow_step)
        if task_status is not None:
            updates.append("status = ?")
            params.append(_validate_task_status(task_status))
        if assignee is not None:
            updates.append("assignee = ?")
            params.append(assignee)
        if not updates:
            return False
        updates.append("updated_at = ?")
        params.append(_now())
        params.append(task_id)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", params
            )
            row = self._conn.execute(
                "SELECT source_message_id, status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task_status is not None and row and row["source_message_id"] is not None:
                self._conn.execute(
                    "UPDATE messages SET task_status = ? WHERE id = ? AND kind = 'task'",
                    (task_status, row["source_message_id"]),
                )
            self._conn.commit()
        return cur.rowcount > 0

    def record_task_transition(
        self,
        task_id: int,
        from_step: str,
        to_step: str,
        actor: str,
        outcome: str,
        note: str = "",
    ) -> dict[str, Any]:
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO task_transitions (
                       task_id, from_step, to_step, actor, outcome, note, created_at
                   )
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (task_id, from_step, to_step, actor, outcome, note[:2000], now),
            )
            self._conn.commit()
        return {
            "id": cur.lastrowid,
            "task_id": task_id,
            "from_step": from_step,
            "to_step": to_step,
            "actor": actor,
            "outcome": outcome,
            "note": note[:2000],
            "created_at": now,
        }

    def list_task_transitions(self, task_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, task_id, from_step, to_step, actor, outcome, note,
                          created_at
                   FROM task_transitions
                   WHERE task_id = ?
                   ORDER BY id""",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def active_task_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT worker, COUNT(*) AS active_count
                   FROM task_runs
                   WHERE status = 'running' AND worker != ''
                   GROUP BY worker"""
            ).fetchall()
        return {row["worker"]: int(row["active_count"]) for row in rows}

    def create_task_run(
        self,
        task_id: int,
        log_dir: str = "",
        worker: str = "",
        status: str = "running",
    ) -> dict[str, Any] | None:
        """Create a task execution attempt and return its run metadata."""
        now = _now()
        worker = (worker or "").strip()
        status = (status or "running").strip()
        with self._lock:
            task = self._conn.execute(
                "SELECT id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                return None
            row = self._conn.execute(
                "SELECT COALESCE(MAX(attempt), 0) + 1 AS next_attempt FROM task_runs WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            attempt = int(row["next_attempt"])
            cur = self._conn.execute(
                """INSERT INTO task_runs (
                       task_id, attempt, worker, status, log_dir, started_at
                   )
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (task_id, attempt, worker, status, log_dir, now),
            )
            run_id = cur.lastrowid
            self._conn.commit()
        return self.get_task_run(run_id)

    def update_task_run_log_dir(
        self, run_id: int, log_dir: str
    ) -> dict[str, Any] | None:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE task_runs SET log_dir = ? WHERE id = ?", (log_dir, run_id)
            )
            self._conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_task_run(run_id)

    def list_task_runs(self, task_id: int, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, task_id, attempt, worker, status, exit_code,
                          log_dir, started_at, finished_at
                   FROM task_runs
                   WHERE task_id = ?
                   ORDER BY attempt DESC
                   LIMIT ?""",
                (task_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_task_run(self, run_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT id, task_id, attempt, worker, status, exit_code,
                          log_dir, started_at, finished_at
                   FROM task_runs
                   WHERE id = ?""",
                (run_id,),
            ).fetchone()
        return dict(row) if row else None

    def finish_task_run(
        self,
        run_id: int,
        status: str,
        exit_code: int | None = None,
    ) -> dict[str, Any] | None:
        status = (status or "").strip()
        if not status:
            raise InvalidInputError("status is required")
        with self._lock:
            cur = self._conn.execute(
                """UPDATE task_runs
                   SET status = ?, exit_code = ?, finished_at = ?
                   WHERE id = ?""",
                (status, exit_code, _now(), run_id),
            )
            self._conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_task_run(run_id)

    def list_messages(
        self,
        agent: str | None = None,
        status: str = "all",
        kind: str = "all",
        task_status: str = "all",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return recent messages for the local UI without leasing them."""
        now = _now()
        limit = max(1, min(int(limit), 500))
        filters = []
        params: list[Any] = []
        if agent:
            filters.append("(sender = ? OR recipient = ?)")
            params.extend([agent, agent])
        if status == "available":
            filters.append(
                "read_at IS NULL AND (leased_until IS NULL OR leased_until <= ?)"
            )
            params.append(now)
        elif status == "leased":
            filters.append("read_at IS NULL AND leased_until > ?")
            params.append(now)
        elif status == "read":
            filters.append("read_at IS NOT NULL")
        elif status == "unread":
            filters.append("read_at IS NULL")
        if kind != "all":
            filters.append("kind = ?")
            params.append(_validate_kind(kind))
        if task_status != "all":
            filters.append("task_status = ?")
            params.append(_validate_task_status(task_status))

        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        query = f"""SELECT id, sender, recipient, content, reply_to, created_at,
                           kind, title, task_status, read_at, leased_until,
                           lease_owner, delivery_count,
                           CASE
                               WHEN read_at IS NOT NULL THEN 'read'
                               WHEN leased_until IS NOT NULL AND leased_until > ? THEN 'leased'
                               ELSE 'available'
                           END AS status
                    FROM messages
                    {where}
                    ORDER BY id DESC
                    LIMIT ?"""
        with self._lock:
            rows = self._conn.execute(query, [now, *params, limit]).fetchall()
        return [dict(row) for row in rows]

    def get_thread(self, message_id: int) -> list[dict[str, Any]]:
        """Return the ancestor chain plus all descendants using recursive SQL."""
        with self._lock:
            rows = self._conn.execute(
                """WITH RECURSIVE
                   ancestors(id, sender, recipient, content, reply_to, created_at,
                             kind, title, task_status,
                             read_at, leased_until, lease_owner, lease_token,
                             delivery_count) AS (
                       SELECT id, sender, recipient, content, reply_to, created_at,
                              kind, title, task_status,
                              read_at, leased_until, lease_owner, lease_token,
                              delivery_count
                       FROM messages
                       WHERE id = ?
                       UNION
                       SELECT m.id, m.sender, m.recipient, m.content, m.reply_to,
                              m.created_at, m.kind, m.title, m.task_status,
                              m.read_at, m.leased_until, m.lease_owner,
                              m.lease_token, m.delivery_count
                       FROM messages m
                       JOIN ancestors a ON m.id = a.reply_to
                   ),
                   thread(id, sender, recipient, content, reply_to, created_at,
                          kind, title, task_status,
                          read_at, leased_until, lease_owner, lease_token,
                          delivery_count) AS (
                       SELECT id, sender, recipient, content, reply_to, created_at,
                              kind, title, task_status,
                              read_at, leased_until, lease_owner, lease_token,
                              delivery_count
                       FROM ancestors
                       UNION
                       SELECT m.id, m.sender, m.recipient, m.content, m.reply_to,
                              m.created_at, m.kind, m.title, m.task_status,
                              m.read_at, m.leased_until, m.lease_owner,
                              m.lease_token, m.delivery_count
                       FROM messages m
                       JOIN thread t ON m.reply_to = t.id
                   )
                   SELECT DISTINCT id, sender, recipient, content, reply_to,
                          created_at, kind, title, task_status, read_at,
                          leased_until, lease_owner,
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

    def _task_row(self, row: sqlite3.Row) -> dict[str, Any]:
        task = dict(row)
        task["required_capabilities"] = _decode_capabilities(
            task.get("required_capabilities", "")
        )
        task["exclusive_workspace"] = bool(task.get("exclusive_workspace", 1))
        return task
