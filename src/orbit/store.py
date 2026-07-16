"""SQLite-backed store for agents and messages."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

def _resolve_state_dir(parent: Path) -> Path:
    """Return the Orbit state directory under `parent`, preferring the new
    `.orbit` name but falling back to a pre-rename `.dev_loop` if that is the
    one that already exists — so existing data keeps working without migration.
    A fresh location defaults to `.orbit`."""
    new = parent / ".orbit"
    if new.exists():
        return new
    legacy = parent / ".dev_loop"
    if legacy.exists():
        return legacy
    return new


def project_state_dir(project_root: Path | str) -> Path:
    """Per-project state dir (workflow/settings, task logs, worktrees).
    `.orbit`, or a legacy `.dev_loop` when that is what the project already has."""
    return _resolve_state_dir(Path(project_root))


# Home-level store root (per-project databases + the project index).
DEFAULT_DB_ROOT = _resolve_state_dir(Path.home()) / "projects"
DEFAULT_LEASE_SECONDS = 300
MESSAGE_KINDS = {"message", "task"}
TASK_STATUSES = {
    "",
    "created",
    "assigned",
    "in_progress",
    "blocked",
    "closed",
}
# Must stay in sync with the scoring tables in server.py — an off-list value
# would silently score as the default there.

# Goals (is_goal=1 rows) run a lifecycle of their own, decoupled from the
# per-task/step statuses above: a goal either traverses the workflow itself or
# reaches an explicit decompose step and then rolls up its work items. A goal
# row is validated against THIS set, never
# TASK_STATUSES, so a task state can never land on a goal
# and a goal phase (e.g. "decomposing") can never land on a task. The server
# owns the phase→status mapping; here we only police the vocabulary.
GOAL_STATUSES = {
    "new",           # created, intake pending
    "decomposing",   # at the decompose step, splitting into work items
    "running",       # traversing its workflow / work items executing
    "verifying",     # workflow done or all work items closed; goal_verify running
    "accepted",      # verified / accepted (terminal)
    "stalled",       # blocked work item, verify failed, or budget frozen
    "closed",        # explicitly closed (terminal)
}
TASK_IMPORTANCE_LEVELS = {"low", "normal", "high", "critical"}
# New-runtime record layer (workflow_runs / node_runs / workflow_tokens).
# Dual-written next to tasks/task_transitions/run_jobs; never consulted for
# routing decisions in this phase.
WORKFLOW_RUN_STATUSES = {"running", "succeeded", "failed", "blocked", "cancelled"}
NODE_RUN_STATUSES = {
    "pending", "running", "waiting", "succeeded", "failed",
    "blocked", "cancelled", "skipped",
}
WORKFLOW_TOKEN_STATUSES = {"pending", "consumed", "cancelled"}

_log = logging.getLogger("orbit.store")
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
    importance        TEXT NOT NULL DEFAULT 'normal',
    size              TEXT NOT NULL DEFAULT 'medium',
    risk              TEXT NOT NULL DEFAULT 'medium',
    required_capabilities TEXT NOT NULL DEFAULT '',
    exclusive_workspace INTEGER NOT NULL DEFAULT 1,
    step_inputs       TEXT NOT NULL DEFAULT '{}',
    result_summary    TEXT NOT NULL DEFAULT '',
    step_output       TEXT NOT NULL DEFAULT '{}',
    artifacts         TEXT NOT NULL DEFAULT '[]',
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
    port       TEXT NOT NULL DEFAULT '',
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
    command     TEXT NOT NULL DEFAULT '',
    tokens      INTEGER,
    pid         INTEGER,
    workflow_step TEXT NOT NULL DEFAULT '',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    cancel_reason TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    UNIQUE(task_id, attempt)
);
CREATE TABLE IF NOT EXISTS workflow_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER NOT NULL,
    action_type  TEXT NOT NULL,
    step         TEXT NOT NULL DEFAULT '',
    assignee     TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending',
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);
CREATE TABLE IF NOT EXISTS workflow_node_results (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL,
    step       TEXT NOT NULL,
    port       TEXT NOT NULL DEFAULT '',
    output     TEXT NOT NULL DEFAULT '{}',
    summary    TEXT NOT NULL DEFAULT '',
    artifacts  TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);
CREATE TABLE IF NOT EXISTS workflow_correlations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id           INTEGER NOT NULL,
    parent_correlation_id INTEGER,
    source_step       TEXT NOT NULL,
    join_step         TEXT NOT NULL,
    activation        INTEGER NOT NULL,
    policy            TEXT NOT NULL DEFAULT 'all_activated',
    status            TEXT NOT NULL DEFAULT 'open',
    transition_cursor INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    consumed_at       TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    FOREIGN KEY(parent_correlation_id) REFERENCES workflow_correlations(id),
    UNIQUE(task_id, join_step, activation)
);
CREATE TABLE IF NOT EXISTS workflow_correlation_branches (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id   INTEGER NOT NULL,
    predecessor_step TEXT NOT NULL,
    branch_root      TEXT NOT NULL DEFAULT '',
    state            TEXT NOT NULL DEFAULT 'selected',
    updated_at       TEXT NOT NULL,
    FOREIGN KEY(correlation_id) REFERENCES workflow_correlations(id),
    UNIQUE(correlation_id, predecessor_step)
);
CREATE TABLE IF NOT EXISTS workflow_item_groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER NOT NULL,
    foreach_step    TEXT NOT NULL,
    activation      INTEGER NOT NULL,
    parent_scope_id INTEGER,
    max_concurrency INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'active',
    transition_cursor INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    completed_at    TEXT,
    advanced_at     TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    FOREIGN KEY(parent_scope_id) REFERENCES workflow_item_scopes(id),
    UNIQUE(task_id, foreach_step, activation, parent_scope_id)
);
CREATE TABLE IF NOT EXISTS workflow_item_scopes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id     INTEGER NOT NULL,
    item_index   INTEGER NOT NULL,
    scope_key    TEXT NOT NULL,
    item_value   TEXT NOT NULL DEFAULT '{}',
    depends_on   TEXT NOT NULL DEFAULT '[]',
    status       TEXT NOT NULL DEFAULT 'pending',
    output       TEXT NOT NULL DEFAULT '{}',
    error        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY(group_id) REFERENCES workflow_item_groups(id),
    UNIQUE(group_id, item_index),
    UNIQUE(group_id, scope_key)
);
CREATE TABLE IF NOT EXISTS workflow_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             INTEGER NOT NULL UNIQUE,
    definition_snapshot TEXT NOT NULL DEFAULT '{}',
    status              TEXT NOT NULL DEFAULT 'running',
    variables           TEXT NOT NULL DEFAULT '{}',
    parent_run_id       INTEGER,
    parent_node_run_id  INTEGER,
    root_run_id         INTEGER,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    completed_at        TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    FOREIGN KEY(parent_run_id) REFERENCES workflow_runs(id),
    FOREIGN KEY(parent_node_run_id) REFERENCES node_runs(id)
);
CREATE TABLE IF NOT EXISTS node_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_run_id INTEGER NOT NULL,
    task_id         INTEGER NOT NULL,
    step            TEXT NOT NULL,
    attempt         INTEGER NOT NULL DEFAULT 1,
    item_scope_id   INTEGER,
    executor        TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    port            TEXT NOT NULL DEFAULT '',
    summary         TEXT NOT NULL DEFAULT '',
    run_job_id      INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    completed_at    TEXT,
    FOREIGN KEY(workflow_run_id) REFERENCES workflow_runs(id),
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    FOREIGN KEY(item_scope_id) REFERENCES workflow_item_scopes(id),
    FOREIGN KEY(run_job_id) REFERENCES run_jobs(id)
);
CREATE TABLE IF NOT EXISTS workflow_tokens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_run_id INTEGER NOT NULL,
    node_run_id     INTEGER,
    from_step       TEXT NOT NULL DEFAULT '',
    to_step         TEXT NOT NULL,
    port            TEXT NOT NULL DEFAULT '',
    item_scope_id   INTEGER,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TEXT NOT NULL,
    consumed_at     TEXT,
    FOREIGN KEY(workflow_run_id) REFERENCES workflow_runs(id),
    FOREIGN KEY(node_run_id) REFERENCES node_runs(id),
    FOREIGN KEY(item_scope_id) REFERENCES workflow_item_scopes(id)
);
CREATE TABLE IF NOT EXISTS run_jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER NOT NULL,
    step            TEXT NOT NULL DEFAULT '',
    assignee        TEXT NOT NULL DEFAULT '',
    command         TEXT NOT NULL DEFAULT '',
    upstream_result TEXT NOT NULL DEFAULT '',
    item_scope_id   INTEGER,
    status          TEXT NOT NULL DEFAULT 'pending',
    claimed_by      TEXT NOT NULL DEFAULT '',
    leased_until    TEXT,
    note            TEXT NOT NULL DEFAULT '',
    outcome         TEXT NOT NULL DEFAULT '',
    result          TEXT NOT NULL DEFAULT '',
    applied_by      TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    completed_at    TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    FOREIGN KEY(item_scope_id) REFERENCES workflow_item_scopes(id)
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
CREATE INDEX IF NOT EXISTS idx_transitions_task
    ON task_transitions (task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee
    ON tasks (assignee);
CREATE INDEX IF NOT EXISTS idx_tasks_parent
    ON tasks (parent_task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_workflow_active
    ON tasks (workflow_step, status);
CREATE INDEX IF NOT EXISTS idx_tasks_goals
    ON tasks (is_goal);
CREATE INDEX IF NOT EXISTS idx_task_runs_task
    ON task_runs (task_id, attempt);
CREATE INDEX IF NOT EXISTS idx_task_transitions_task
    ON task_transitions (task_id, id);
CREATE INDEX IF NOT EXISTS idx_task_transitions_step_outcome
    ON task_transitions (to_step, outcome);
CREATE INDEX IF NOT EXISTS idx_workflow_actions_pending
    ON workflow_actions (status, task_id, id);
CREATE INDEX IF NOT EXISTS idx_workflow_node_results_task_step
    ON workflow_node_results (task_id, step, id);
CREATE INDEX IF NOT EXISTS idx_workflow_correlations_open
    ON workflow_correlations (task_id, join_step, status, id);
CREATE INDEX IF NOT EXISTS idx_workflow_item_groups_task_step
    ON workflow_item_groups (task_id, foreach_step, activation);
CREATE INDEX IF NOT EXISTS idx_workflow_item_scopes_group_status
    ON workflow_item_scopes (group_id, status, item_index);
CREATE INDEX IF NOT EXISTS idx_run_jobs_item_scope
    ON run_jobs (item_scope_id, status, id);
CREATE INDEX IF NOT EXISTS idx_workflow_correlation_branches_group
    ON workflow_correlation_branches (correlation_id, predecessor_step);
CREATE INDEX IF NOT EXISTS idx_run_jobs_available
    ON run_jobs (status, leased_until, id);
CREATE INDEX IF NOT EXISTS idx_run_jobs_task
    ON run_jobs (task_id, step, id);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_status
    ON workflow_runs (status, id);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_root
    ON workflow_runs (root_run_id, id);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_parent
    ON workflow_runs (parent_run_id, id);
CREATE INDEX IF NOT EXISTS idx_node_runs_run_step
    ON node_runs (workflow_run_id, step, id);
CREATE INDEX IF NOT EXISTS idx_node_runs_task_step
    ON node_runs (task_id, step, id);
CREATE INDEX IF NOT EXISTS idx_node_runs_item_scope
    ON node_runs (item_scope_id, id);
CREATE INDEX IF NOT EXISTS idx_node_runs_run_job
    ON node_runs (run_job_id);
CREATE INDEX IF NOT EXISTS idx_workflow_tokens_pending
    ON workflow_tokens (workflow_run_id, to_step, status, id);
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
    if task_status in TASK_STATUSES:
        return task_status
    raise InvalidInputError(
        f"invalid task_status: {task_status!r} "
        f"(expected one of {sorted(s for s in TASK_STATUSES if s)})"
    )


# Engine paths that drive a *task* through the workflow also drive a goal
# through its own steps, and would otherwise write a task-status onto the goal
# row. Map those to the goal's own vocabulary so a goal never carries a step
# column, without every call site having to special-case is_goal. (The dispatch
# path picks richer phase names — new/decomposing/running — before we get here.)
_TASK_TO_GOAL_STATUS = {
    "created": "new",
    "assigned": "running",
    "in_progress": "running",
    "blocked": "stalled",
}


def _validate_goal_status(status: str) -> str:
    status = (status or "").strip()
    if status in GOAL_STATUSES:
        return status
    mapped = _TASK_TO_GOAL_STATUS.get(status)
    if mapped:
        return mapped
    raise InvalidInputError(
        f"invalid goal status: {status!r} "
        f"(expected one of {sorted(GOAL_STATUSES)})"
    )


def _validate_status_for(is_goal: bool, status: str) -> str:
    """Route a status through the right vocabulary: goal rows against
    GOAL_STATUSES, everything else against TASK_STATUSES."""
    return _validate_goal_status(status) if is_goal else _validate_task_status(status)


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


def _encode_depends_on(ids: list[int] | None) -> str:
    if not ids:
        return ""
    seen: list[int] = []
    for value in ids:
        try:
            n = int(value)
        except (TypeError, ValueError):
            continue
        if n not in seen:
            seen.append(n)
    return json.dumps(seen) if seen else ""


def _decode_depends_on(raw: str) -> list[int]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out: list[int] = []
    for value in data:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return out


def _encode_step_inputs(value: dict[str, Any] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False)


def _decode_step_inputs(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _encode_artifacts(values: list[str] | None) -> str:
    cleaned: list[str] = []
    for value in values or []:
        text = str(value).strip()
        if text and text not in cleaned:
            cleaned.append(text[:1000])
        if len(cleaned) >= 100:
            break
    return json.dumps(cleaned, ensure_ascii=False)


def _decode_artifacts(raw: str) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]


class Store:
    def __init__(self, db_path: Path | str | None = None):
        if db_path is None:
            db_path = project_db_path()
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._lock = threading.Lock()
        self._job_condition = threading.Condition()
        self._job_generation = 0
        self._closed = False
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            # Multiple processes (UI/scheduler + N runners) write this DB; wait
            # for a contended write lock instead of failing with "database is
            # locked" immediately.
            self._conn.execute("PRAGMA busy_timeout=5000;")
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
        transition_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(task_transitions)").fetchall()
        }
        if transition_columns and "port" not in transition_columns:
            self._conn.execute(
                "ALTER TABLE task_transitions ADD COLUMN port TEXT NOT NULL DEFAULT ''"
            )
        task_defaults = {
            "importance": "TEXT NOT NULL DEFAULT 'normal'",
            "size": "TEXT NOT NULL DEFAULT 'medium'",
            "risk": "TEXT NOT NULL DEFAULT 'medium'",
            "required_capabilities": "TEXT NOT NULL DEFAULT ''",
            "exclusive_workspace": "INTEGER NOT NULL DEFAULT 1",
            "workflow_step": "TEXT NOT NULL DEFAULT ''",
            "parent_task_id": "INTEGER",
            "is_goal": "INTEGER NOT NULL DEFAULT 0",
            # Per-goal token ceiling (raw tokens); 0 = unlimited.
            "token_budget": "INTEGER NOT NULL DEFAULT 0",
            # Human-facing hierarchical label for step cards, e.g. "2474.3".
            "display_id": "TEXT NOT NULL DEFAULT ''",
            # Per-goal convergence-verify command; '' falls back to auto-detect.
            "goal_verify": "TEXT NOT NULL DEFAULT ''",
            # JSON list of prerequisite task ids a business subtask waits on; the
            # engine holds it until they all close. '' / '[]' = no dependency.
            "depends_on": "TEXT NOT NULL DEFAULT ''",
            # Name of the subflow whose graph this task traverses; '' = the
            # main workflow graph (see workflow_config_for_task).
            "workflow_ref": "TEXT NOT NULL DEFAULT ''",
            # Structured data for one materialized workflow-step execution.
            "step_inputs": "TEXT NOT NULL DEFAULT '{}'",
            "result_summary": "TEXT NOT NULL DEFAULT ''",
            "step_output": "TEXT NOT NULL DEFAULT '{}'",
            "artifacts": "TEXT NOT NULL DEFAULT '[]'",
        }
        for column, definition in task_defaults.items():
            if column not in task_columns:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
        # Backfill hierarchical labels for existing step cards (one-time; only
        # touches cards still missing a display_id).
        if "display_id" not in task_columns:
            self._conn.execute(
                """UPDATE tasks SET display_id = parent_task_id || '.' || (
                       SELECT COUNT(*) FROM tasks t2
                       WHERE t2.parent_task_id = tasks.parent_task_id
                         AND t2.source_message_id IS NULL
                         AND t2.id <= tasks.id
                   )
                   WHERE parent_task_id IS NOT NULL
                     AND source_message_id IS NULL
                     AND display_id = ''"""
            )
        run_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(task_runs)").fetchall()
        }
        if run_columns and "command" not in run_columns:
            self._conn.execute(
                "ALTER TABLE task_runs ADD COLUMN command TEXT NOT NULL DEFAULT ''"
            )
        if run_columns and "tokens" not in run_columns:
            self._conn.execute("ALTER TABLE task_runs ADD COLUMN tokens INTEGER")
        if run_columns and "pid" not in run_columns:
            self._conn.execute("ALTER TABLE task_runs ADD COLUMN pid INTEGER")
        if run_columns and "workflow_step" not in run_columns:
            self._conn.execute(
                "ALTER TABLE task_runs ADD COLUMN workflow_step TEXT NOT NULL DEFAULT ''"
            )
        if run_columns and "cancel_requested" not in run_columns:
            self._conn.execute(
                "ALTER TABLE task_runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"
            )
        if run_columns and "cancel_reason" not in run_columns:
            self._conn.execute(
                "ALTER TABLE task_runs ADD COLUMN cancel_reason TEXT NOT NULL DEFAULT ''"
            )
        # Older databases predate runner jobs. The main schema creates the
        # table for fresh databases; this keeps existing project DBs compatible.
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS run_jobs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id         INTEGER NOT NULL,
                step            TEXT NOT NULL DEFAULT '',
                assignee        TEXT NOT NULL DEFAULT '',
                command         TEXT NOT NULL DEFAULT '',
                upstream_result TEXT NOT NULL DEFAULT '',
                status          TEXT NOT NULL DEFAULT 'pending',
                claimed_by      TEXT NOT NULL DEFAULT '',
                leased_until    TEXT,
                note            TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                completed_at    TEXT,
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            )"""
        )
        job_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(run_jobs)").fetchall()
        }
        # Runner reports the run's outcome/result onto the job; the scheduler
        # reads them to advance the workflow (runner no longer advances itself).
        if job_columns and "outcome" not in job_columns:
            self._conn.execute(
                "ALTER TABLE run_jobs ADD COLUMN outcome TEXT NOT NULL DEFAULT ''"
            )
        if job_columns and "result" not in job_columns:
            self._conn.execute(
                "ALTER TABLE run_jobs ADD COLUMN result TEXT NOT NULL DEFAULT ''"
            )
        # Scheduler-side applying-lease owner, kept separate from claimed_by so
        # the runner that executed the job stays visible.
        if job_columns and "applied_by" not in job_columns:
            self._conn.execute(
                "ALTER TABLE run_jobs ADD COLUMN applied_by TEXT NOT NULL DEFAULT ''"
            )
        if job_columns and "item_scope_id" not in job_columns:
            self._conn.execute("ALTER TABLE run_jobs ADD COLUMN item_scope_id INTEGER")
        correlation_columns = {
            row["name"]
            for row in self._conn.execute(
                "PRAGMA table_info(workflow_correlations)"
            ).fetchall()
        }
        if correlation_columns and "parent_correlation_id" not in correlation_columns:
            self._conn.execute(
                "ALTER TABLE workflow_correlations ADD COLUMN parent_correlation_id INTEGER"
            )
        item_group_columns = {
            row["name"]
            for row in self._conn.execute(
                "PRAGMA table_info(workflow_item_groups)"
            ).fetchall()
        }
        if item_group_columns and "transition_cursor" not in item_group_columns:
            self._conn.execute(
                "ALTER TABLE workflow_item_groups ADD COLUMN transition_cursor INTEGER NOT NULL DEFAULT 0"
            )
        if item_group_columns and "advanced_at" not in item_group_columns:
            self._conn.execute(
                "ALTER TABLE workflow_item_groups ADD COLUMN advanced_at TEXT"
            )
        # Cross-run lineage: which run (and which node execution in it) spawned
        # this run, plus the root of the whole lineage tree for one-query reads.
        run_record_columns = {
            row["name"]
            for row in self._conn.execute(
                "PRAGMA table_info(workflow_runs)"
            ).fetchall()
        }
        if run_record_columns:
            for column in ("parent_run_id", "parent_node_run_id", "root_run_id"):
                if column not in run_record_columns:
                    self._conn.execute(
                        f"ALTER TABLE workflow_runs ADD COLUMN {column} INTEGER"
                    )
            if "root_run_id" not in run_record_columns:
                # Pre-lineage runs are their own roots.
                self._conn.execute(
                    "UPDATE workflow_runs SET root_run_id = id WHERE root_run_id IS NULL"
                )
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
                    # A task replying to another task's source message is a
                    # subtask of it (goal splitting): link the parent.
                    parent_task_id = None
                    if reply_to is not None:
                        parent = self._conn.execute(
                            "SELECT id FROM tasks WHERE source_message_id = ?",
                            (reply_to,),
                        ).fetchone()
                        parent_task_id = parent["id"] if parent else None
                    self._conn.execute(
                        """INSERT OR IGNORE INTO tasks (
                               source_message_id, title, content, sender, assignee,
                               status, parent_task_id, created_at, updated_at
                           )
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            message_id,
                            title,
                            content,
                            sender,
                            recipient,
                            task_status,
                            parent_task_id,
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
        limit_val = int(limit)
        if limit_val >= 0:
            limit_val = max(1, min(limit_val, 500))
        else:
            limit_val = 999999999
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
                           importance, size, risk,
                           required_capabilities, exclusive_workspace,
                           workflow_step, parent_task_id, is_goal, display_id, token_budget, goal_verify, depends_on, workflow_ref,
                           step_inputs, result_summary, step_output, artifacts
                    FROM tasks
                    {where}
                    ORDER BY id DESC
                    LIMIT ?"""
        with self._lock:
            rows = self._conn.execute(query, [*params, limit_val]).fetchall()
        return [self._task_row(row) for row in rows]

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT id, source_message_id, title, content, sender,
                          assignee, assignee AS recipient, status AS task_status,
                          created_at, updated_at, importance, size,
                          risk, required_capabilities, exclusive_workspace,
                          workflow_step, parent_task_id, is_goal, display_id, token_budget, goal_verify, depends_on, workflow_ref,
                          step_inputs, result_summary, step_output, artifacts
                   FROM tasks
                   WHERE id = ?""",
                (task_id,),
            ).fetchone()
        return self._task_row(row) if row else None

    def get_task_by_source_message(self, message_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT id, source_message_id, title, content, sender,
                          assignee, assignee AS recipient, status AS task_status,
                          created_at, updated_at, importance, size,
                          risk, required_capabilities, exclusive_workspace,
                          workflow_step, parent_task_id, is_goal, display_id, token_budget, goal_verify, depends_on, workflow_ref,
                          step_inputs, result_summary, step_output, artifacts
                   FROM tasks
                   WHERE source_message_id = ?""",
                (message_id,),
            ).fetchone()
        return self._task_row(row) if row else None

    def list_tasks_by_parent(self, parent_task_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, source_message_id, title, content, sender,
                          assignee, assignee AS recipient, status AS task_status,
                          created_at, updated_at, importance, size,
                          risk, required_capabilities, exclusive_workspace,
                          workflow_step, parent_task_id, is_goal, display_id, token_budget, goal_verify, depends_on, workflow_ref,
                          step_inputs, result_summary, step_output, artifacts
                   FROM tasks
                   WHERE parent_task_id = ?
                   ORDER BY id DESC""",
                (parent_task_id,),
            ).fetchall()
        return [self._task_row(row) for row in rows]

    def list_goals_with_children(self) -> list[dict[str, Any]]:
        """Return goals and their direct children for the Goals page."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, source_message_id, title, content, sender,
                          assignee, assignee AS recipient, status AS task_status,
                          created_at, updated_at, importance, size,
                          risk, required_capabilities, exclusive_workspace,
                          workflow_step, parent_task_id, is_goal, display_id, token_budget, goal_verify, depends_on, workflow_ref,
                          step_inputs, result_summary, step_output, artifacts
                   FROM tasks
                   WHERE is_goal = 1
                      OR parent_task_id IN (SELECT id FROM tasks WHERE is_goal = 1)
                   ORDER BY id DESC"""
            ).fetchall()
        return [self._task_row(row) for row in rows]

    def list_active_workflow_tasks(self) -> list[dict[str, Any]]:
        """Return workflow tasks that can affect scheduling or timeouts."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, source_message_id, title, content, sender,
                          assignee, assignee AS recipient, status AS task_status,
                          created_at, updated_at, importance, size,
                          risk, required_capabilities, exclusive_workspace,
                          workflow_step, parent_task_id, is_goal, display_id, token_budget, goal_verify, depends_on, workflow_ref,
                          step_inputs, result_summary, step_output, artifacts
                   FROM tasks
                   WHERE workflow_step != ''
                     AND status NOT IN ('blocked', 'closed')
                   ORDER BY id DESC"""
            ).fetchall()
        return [self._task_row(row) for row in rows]

    def list_non_terminal_tasks(self) -> list[dict[str, Any]]:
        """Return tasks that are not finished, for the health watchdog — avoids
        scanning closed history every cycle. A task is finished when it is
        'closed', or — for a goal — 'accepted' (goals use a separate lifecycle
        vocabulary and rest there)."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, source_message_id, title, content, sender,
                          assignee, assignee AS recipient, status AS task_status,
                          created_at, updated_at, importance, size,
                          risk, required_capabilities, exclusive_workspace,
                          workflow_step, parent_task_id, is_goal, display_id, token_budget, goal_verify, depends_on, workflow_ref,
                          step_inputs, result_summary, step_output, artifacts
                   FROM tasks
                   WHERE status != 'closed'
                     AND NOT (status = 'accepted' AND is_goal = 1)
                   ORDER BY id DESC"""
            ).fetchall()
        return [self._task_row(row) for row in rows]

    def update_task_metadata(
        self,
        task_id: int,
        importance: str | None = None,
        size: str | None = None,
        risk: str | None = None,
        required_capabilities: list[str] | str | None = None,
        exclusive_workspace: bool | None = None,
        is_goal: bool | None = None,
        token_budget: int | None = None,
        goal_verify: str | None = None,
        depends_on: list[int] | None = None,
    ) -> dict[str, Any] | None:
        updates: list[str] = []
        params: list[Any] = []
        for column, value, allowed in (
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
            updates.append(f"{column} = ?")
            params.append(value)
        if required_capabilities is not None:
            updates.append("required_capabilities = ?")
            params.append(_encode_capabilities(required_capabilities))
        if exclusive_workspace is not None:
            updates.append("exclusive_workspace = ?")
            params.append(1 if exclusive_workspace else 0)
        if is_goal is not None:
            updates.append("is_goal = ?")
            params.append(1 if is_goal else 0)
        if token_budget is not None:
            updates.append("token_budget = ?")
            params.append(max(0, int(token_budget)))
        if goal_verify is not None:
            updates.append("goal_verify = ?")
            params.append(str(goal_verify).strip())
        if depends_on is not None:
            updates.append("depends_on = ?")
            params.append(_encode_depends_on(depends_on))
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
        if token_budget is not None:
            # Migration-period dual-write (design §11): tasks.token_budget stays
            # the authoritative source every guard reads; the workflow run's
            # variables only mirror it at the record layer.
            run = self.get_workflow_run_by_task(task_id)
            if run is not None:
                self.update_workflow_run_variables(
                    int(run["id"]), token_budget=max(0, int(token_budget))
                )
        return self.get_task(task_id)

    def update_task_item_status(self, task_id: int, task_status: str) -> bool:
        if not (task_status or "").strip():
            raise InvalidInputError("task_status must not be empty for an update")
        now = _now()
        with self._lock:
            # Goal rows (is_goal=1) validate against GOAL_STATUSES; the manual
            # status API (e.g. close a goal) shares this path.
            task_status = _validate_status_for(
                self._row_is_goal_locked(task_id), task_status
            )
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

    def create_step_card(
        self,
        parent_task_id: int,
        workflow_step: str,
        title: str,
        content: str,
        sender: str,
        assignee: str,
        status: str,
        step_inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Materialize one workflow step of a goal as its own task card."""
        status = _validate_task_status(status) or "created"
        now = _now()
        with self._lock:
            # Hierarchical label tied to the parent task, e.g. "2474.3" for the
            # third step card of task 2474 — stable, assigned once in order.
            row = self._conn.execute(
                """SELECT COUNT(*) AS n FROM tasks
                   WHERE parent_task_id = ? AND source_message_id IS NULL""",
                (parent_task_id,),
            ).fetchone()
            display_id = f"{parent_task_id}.{int(row['n']) + 1}"
            cur = self._conn.execute(
                """INSERT INTO tasks (
                       title, content, sender, assignee, status,
                       parent_task_id, workflow_step, display_id, step_inputs,
                       created_at, updated_at
                   )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    title, content, sender, assignee, status,
                    parent_task_id, workflow_step, display_id,
                    _encode_step_inputs(step_inputs), now, now,
                ),
            )
            self._conn.commit()
        return self.get_task(cur.lastrowid)

    def create_subflow_task(
        self,
        parent_task_id: int,
        workflow_ref: str,
        title: str,
        content: str,
        sender: str,
    ) -> dict[str, Any]:
        """Create the child task a subflow node executes. Like a step card it
        hangs off parent_task_id with no source message — so goal roll-up and
        business-subtask queries ignore it — while `workflow_ref` routes the
        engine to the subflow's own graph instead of the main workflow."""
        workflow_ref = (workflow_ref or "").strip()
        if not workflow_ref:
            raise InvalidInputError("workflow_ref is required for a subflow task")
        now = _now()
        with self._lock:
            parent = self._conn.execute(
                "SELECT id FROM tasks WHERE id = ?", (parent_task_id,)
            ).fetchone()
            if parent is None:
                raise InvalidInputError(f"unknown task: {parent_task_id}")
            cur = self._conn.execute(
                """INSERT INTO tasks (
                       title, content, sender, assignee, status,
                       parent_task_id, workflow_ref, created_at, updated_at
                   )
                   VALUES (?, ?, ?, ?, 'created', ?, ?, ?, ?)""",
                (
                    title, content, sender, "",
                    parent_task_id, workflow_ref, now, now,
                ),
            )
            self._conn.commit()
        return self.get_task(cur.lastrowid)

    def find_open_step_card(
        self, parent_task_id: int, workflow_step: str
    ) -> dict[str, Any] | None:
        with self._lock:
            # workflow_ref = '' excludes subflow child tasks: they also hang off
            # the parent but traverse their own graph, and a subflow step id that
            # happens to match a main-graph step must not shadow the real card.
            row = self._conn.execute(
                """SELECT id FROM tasks
                   WHERE parent_task_id = ? AND workflow_step = ?
                     AND workflow_ref = '' AND status != 'closed'
                   ORDER BY id DESC LIMIT 1""",
                (parent_task_id, workflow_step),
            ).fetchone()
        return self.get_task(row["id"]) if row else None

    def update_task_step_details(
        self,
        task_id: int,
        *,
        step_inputs: dict[str, Any] | None = None,
        result_summary: str | None = None,
        step_output: dict[str, Any] | None = None,
        artifacts: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Persist structured workflow-step data on a step card (or, for a
        non-materialized workflow, directly on its task row)."""
        updates: list[str] = []
        params: list[Any] = []
        if step_inputs is not None:
            if not isinstance(step_inputs, dict):
                raise InvalidInputError("step_inputs must be an object")
            updates.append("step_inputs = ?")
            params.append(_encode_step_inputs(step_inputs))
        if result_summary is not None:
            updates.append("result_summary = ?")
            params.append(str(result_summary).strip()[:20000])
        if step_output is not None:
            if not isinstance(step_output, dict):
                raise InvalidInputError("step_output must be an object")
            updates.append("step_output = ?")
            params.append(_encode_step_inputs(step_output))
        if artifacts is not None:
            if not isinstance(artifacts, list):
                raise InvalidInputError("artifacts must be a list")
            updates.append("artifacts = ?")
            params.append(_encode_artifacts(artifacts))
        if not updates:
            return self.get_task(task_id)
        updates.append("updated_at = ?")
        params.extend([_now(), task_id])
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", params
            )
            self._conn.commit()
        return self.get_task(task_id) if cur.rowcount else None

    def record_workflow_node_result(
        self,
        task_id: int,
        step: str,
        *,
        port: str = "",
        output: dict[str, Any] | None = None,
        summary: str = "",
        artifacts: list[Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(output or {}, dict):
            raise InvalidInputError("workflow node output must be an object")
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO workflow_node_results
                   (task_id, step, port, output, summary, artifacts, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    str(step),
                    str(port),
                    _encode_step_inputs(output or {}),
                    str(summary).strip()[:20000],
                    json.dumps(artifacts or [], ensure_ascii=False),
                    now,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM workflow_node_results WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return self._workflow_node_result_row(row)

    def list_workflow_node_results(
        self, task_id: int, step: str = ""
    ) -> list[dict[str, Any]]:
        where = "task_id = ?" + (" AND step = ?" if step else "")
        params: list[Any] = [task_id] + ([step] if step else [])
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM workflow_node_results WHERE {where} ORDER BY id",
                params,
            ).fetchall()
        return [self._workflow_node_result_row(row) for row in rows]

    def ensure_workflow_correlation(
        self,
        task_id: int,
        source_step: str,
        join_step: str,
        policy: str,
        branches: list[dict[str, str]],
        parent_correlation_id: int | None = None,
    ) -> dict[str, Any]:
        """Create or extend the current open join activation atomically."""
        now = _now()
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM workflow_correlations
                   WHERE task_id = ? AND join_step = ? AND status = 'open'
                   ORDER BY id DESC LIMIT 1""",
                (task_id, join_step),
            ).fetchone()
            if row is None:
                activation_row = self._conn.execute(
                    """SELECT COALESCE(MAX(activation), 0) AS activation
                       FROM workflow_correlations
                       WHERE task_id = ? AND join_step = ?""",
                    (task_id, join_step),
                ).fetchone()
                cursor_row = self._conn.execute(
                    "SELECT COALESCE(MAX(id), 0) AS cursor FROM task_transitions WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                activation = int(activation_row["activation"] or 0) + 1
                cur = self._conn.execute(
                    """INSERT INTO workflow_correlations
                       (task_id, parent_correlation_id, source_step, join_step,
                        activation, policy, status,
                        transition_cursor, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
                    (
                        task_id, parent_correlation_id, source_step, join_step,
                        activation, policy,
                        int(cursor_row["cursor"] or 0), now, now,
                    ),
                )
                correlation_id = int(cur.lastrowid)
            else:
                correlation_id = int(row["id"])
                if parent_correlation_id is not None:
                    self._conn.execute(
                        """UPDATE workflow_correlations
                           SET parent_correlation_id = COALESCE(parent_correlation_id, ?)
                           WHERE id = ?""",
                        (parent_correlation_id, correlation_id),
                    )
            for branch in branches:
                predecessor = str(branch.get("predecessor_step") or "")
                if not predecessor:
                    continue
                existing = self._conn.execute(
                    """SELECT state FROM workflow_correlation_branches
                       WHERE correlation_id = ? AND predecessor_step = ?""",
                    (correlation_id, predecessor),
                ).fetchone()
                new_state = str(branch.get("state") or "selected")
                if existing is not None and existing["state"] == "arrived":
                    new_state = "arrived"
                self._conn.execute(
                    """INSERT INTO workflow_correlation_branches
                       (correlation_id, predecessor_step, branch_root, state, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(correlation_id, predecessor_step) DO UPDATE SET
                         branch_root = excluded.branch_root,
                         state = excluded.state,
                         updated_at = excluded.updated_at""",
                    (
                        correlation_id, predecessor,
                        str(branch.get("branch_root") or ""), new_state, now,
                    ),
                )
            self._conn.execute(
                "UPDATE workflow_correlations SET updated_at = ? WHERE id = ?",
                (now, correlation_id),
            )
            self._conn.commit()
            return self._workflow_correlation_locked(correlation_id)

    def sync_workflow_correlation(
        self, task_id: int, join_step: str
    ) -> dict[str, Any] | None:
        """Project post-creation transition arrivals into the open activation."""
        now = _now()
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM workflow_correlations
                   WHERE task_id = ? AND join_step = ? AND status = 'open'
                   ORDER BY id DESC LIMIT 1""",
                (task_id, join_step),
            ).fetchone()
            if row is None:
                return None
            state_by_outcome = {
                "done": "arrived", "skipped": "arrived",
                "not_selected": "not_selected", "cancelled": "cancelled",
                "blocked": "blocked",
            }
            transitions = self._conn.execute(
                """SELECT from_step, outcome FROM task_transitions
                   WHERE task_id = ? AND to_step = ? AND id > ?
                   ORDER BY id""",
                (task_id, join_step, int(row["transition_cursor"] or 0)),
            ).fetchall()
            for transition in transitions:
                state = state_by_outcome.get(str(transition["outcome"]))
                if not state:
                    continue
                self._conn.execute(
                    """UPDATE workflow_correlation_branches
                       SET state = ?, updated_at = ?
                       WHERE correlation_id = ? AND predecessor_step = ?""",
                    (state, now, int(row["id"]), str(transition["from_step"])),
                )
            self._conn.execute(
                "UPDATE workflow_correlations SET updated_at = ? WHERE id = ?",
                (now, int(row["id"])),
            )
            self._conn.commit()
            return self._workflow_correlation_locked(int(row["id"]))

    def cancel_workflow_correlation_branches(self, correlation_id: int) -> list[str]:
        """Mark every still-selected branch of one activation cancelled.

        Returns the predecessor steps that were cancelled, so the engine can
        settle their in-flight work.
        """
        now = _now()
        with self._lock:
            rows = self._conn.execute(
                """SELECT predecessor_step FROM workflow_correlation_branches
                   WHERE correlation_id = ? AND state = 'selected'
                   ORDER BY id""",
                (correlation_id,),
            ).fetchall()
            cancelled = [str(row["predecessor_step"]) for row in rows]
            if cancelled:
                self._conn.execute(
                    """UPDATE workflow_correlation_branches
                       SET state = 'cancelled', updated_at = ?
                       WHERE correlation_id = ? AND state = 'selected'""",
                    (now, correlation_id),
                )
                self._conn.execute(
                    "UPDATE workflow_correlations SET updated_at = ? WHERE id = ?",
                    (now, correlation_id),
                )
            self._conn.commit()
        return cancelled

    def consume_workflow_correlation(self, correlation_id: int) -> bool:
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                """UPDATE workflow_correlations
                   SET status = 'consumed', consumed_at = ?, updated_at = ?
                   WHERE id = ? AND status = 'open'""",
                (now, now, correlation_id),
            )
            self._conn.commit()
        return cur.rowcount == 1

    def list_workflow_correlations(
        self, task_id: int, join_step: str = ""
    ) -> list[dict[str, Any]]:
        where = "task_id = ?" + (" AND join_step = ?" if join_step else "")
        params: list[Any] = [task_id] + ([join_step] if join_step else [])
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id FROM workflow_correlations WHERE {where} ORDER BY id",
                params,
            ).fetchall()
            return [self._workflow_correlation_locked(int(row["id"])) for row in rows]

    def _workflow_correlation_locked(self, correlation_id: int) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM workflow_correlations WHERE id = ?", (correlation_id,)
        ).fetchone()
        result = dict(row)
        branches = self._conn.execute(
            """SELECT predecessor_step, branch_root, state, updated_at
               FROM workflow_correlation_branches
               WHERE correlation_id = ? ORDER BY id""",
            (correlation_id,),
        ).fetchall()
        result["branches"] = [dict(branch) for branch in branches]
        return result

    def create_workflow_item_group(
        self,
        task_id: int,
        foreach_step: str,
        items: list[dict[str, Any]],
        max_concurrency: int = 1,
        parent_scope_id: int | None = None,
    ) -> dict[str, Any]:
        """Persist one foreach activation and its dependency-isolated scopes."""
        if not isinstance(items, list):
            raise InvalidInputError("workflow foreach items must be a list")
        try:
            concurrency = int(max_concurrency)
        except (TypeError, ValueError):
            raise InvalidInputError("workflow foreach max_concurrency must be an integer") from None
        if concurrency < 1:
            raise InvalidInputError("workflow foreach max_concurrency must be >= 1")

        normalized: list[dict[str, Any]] = []
        keys: set[str] = set()
        for index, raw in enumerate(items):
            if not isinstance(raw, dict):
                raise InvalidInputError("workflow foreach scope must be an object")
            key = str(raw.get("key", index)).strip()
            if not key:
                raise InvalidInputError("workflow foreach scope key must not be empty")
            if key in keys:
                raise InvalidInputError(f"duplicate workflow foreach scope key: {key}")
            keys.add(key)
            try:
                encoded_value = json.dumps(raw.get("value"), ensure_ascii=False)
            except (TypeError, ValueError):
                raise InvalidInputError(
                    f"workflow foreach scope {key!r} value must be JSON-serializable"
                ) from None
            dependencies = raw.get("depends_on") or []
            if not isinstance(dependencies, list):
                raise InvalidInputError(
                    f"workflow foreach scope {key!r} depends_on must be a list"
                )
            normalized.append(
                {
                    "key": key,
                    "value_json": encoded_value,
                    "depends_on": [str(value).strip() for value in dependencies],
                }
            )
        for item in normalized:
            unknown = [key for key in item["depends_on"] if key not in keys]
            if unknown:
                raise InvalidInputError(
                    f"workflow foreach scope {item['key']!r} has unknown dependencies: "
                    + ", ".join(unknown)
                )
            if item["key"] in item["depends_on"]:
                raise InvalidInputError(
                    f"workflow foreach scope {item['key']!r} cannot depend on itself"
                )
        dependencies_by_key = {
            item["key"]: set(item["depends_on"]) for item in normalized
        }
        resolved: set[str] = set()
        while len(resolved) < len(normalized):
            ready = {
                key for key, dependencies in dependencies_by_key.items()
                if key not in resolved and dependencies <= resolved
            }
            if not ready:
                raise InvalidInputError("workflow foreach item dependencies contain a cycle")
            resolved.update(ready)

        now = _now()
        with self._lock:
            activation_row = self._conn.execute(
                """SELECT COALESCE(MAX(activation), 0) AS activation
                   FROM workflow_item_groups
                   WHERE task_id = ? AND foreach_step = ?
                     AND parent_scope_id IS ?""",
                (task_id, foreach_step, parent_scope_id),
            ).fetchone()
            activation = int(activation_row["activation"] or 0) + 1
            cursor_row = self._conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS cursor FROM task_transitions WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            cur = self._conn.execute(
                """INSERT INTO workflow_item_groups
                   (task_id, foreach_step, activation, parent_scope_id,
                    max_concurrency, status, transition_cursor,
                    created_at, updated_at, completed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id, foreach_step, activation, parent_scope_id, concurrency,
                    "completed" if not normalized else "active",
                    int(cursor_row["cursor"] or 0), now, now,
                    now if not normalized else None,
                ),
            )
            group_id = int(cur.lastrowid)
            for index, item in enumerate(normalized):
                self._conn.execute(
                    """INSERT INTO workflow_item_scopes
                       (group_id, item_index, scope_key, item_value, depends_on,
                        status, output, error, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'pending', '{}', '', ?, ?)""",
                    (
                        group_id, index, item["key"],
                        item["value_json"],
                        json.dumps(item["depends_on"], ensure_ascii=False), now, now,
                    ),
                )
            self._refresh_workflow_item_group_locked(group_id, now)
            self._conn.commit()
            return self._workflow_item_group_locked(group_id)

    def update_workflow_item_scope(
        self,
        scope_id: int,
        status: str,
        output: Any = None,
        error: str = "",
    ) -> dict[str, Any]:
        allowed = {"running", "completed", "blocked", "cancelled"}
        if status not in allowed:
            raise InvalidInputError(f"unsupported workflow item scope status: {status}")
        try:
            encoded_output = json.dumps(output or {}, ensure_ascii=False)
        except (TypeError, ValueError):
            raise InvalidInputError(
                "workflow item scope output must be JSON-serializable"
            ) from None
        now = _now()
        terminal = status in {"completed", "blocked", "cancelled"}
        with self._lock:
            row = self._conn.execute(
                "SELECT group_id, status FROM workflow_item_scopes WHERE id = ?", (scope_id,)
            ).fetchone()
            if row is None:
                raise InvalidInputError(f"unknown workflow item scope: {scope_id}")
            current = str(row["status"])
            valid_transition = (
                (status == "running" and current == "ready")
                or (status in {"completed", "blocked"} and current in {"ready", "running"})
                or (status == "cancelled" and current in {"pending", "ready", "running"})
            )
            if not valid_transition:
                raise InvalidInputError(
                    f"workflow item scope cannot transition from {current!r} to {status!r}"
                )
            self._conn.execute(
                """UPDATE workflow_item_scopes
                   SET status = ?, output = ?, error = ?, updated_at = ?, completed_at = ?
                   WHERE id = ?""",
                (
                    status, encoded_output, str(error or ""),
                    now, now if terminal else None, scope_id,
                ),
            )
            if terminal:
                # Dual-write: settle the per-item node run with the scope.
                self._guarded_mirror_locked(
                    "item_scope",
                    lambda: self._mirror_item_scope_settled_locked(
                        scope_id, status, output, str(error or ""), now
                    ),
                )
            group_id = int(row["group_id"])
            self._refresh_workflow_item_group_locked(group_id, now)
            self._conn.commit()
            return self._workflow_item_scope_locked(scope_id)

    def list_workflow_item_groups(
        self, task_id: int, foreach_step: str = ""
    ) -> list[dict[str, Any]]:
        where = "task_id = ?" + (" AND foreach_step = ?" if foreach_step else "")
        params: list[Any] = [task_id] + ([foreach_step] if foreach_step else [])
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id FROM workflow_item_groups WHERE {where} ORDER BY id", params
            ).fetchall()
            return [self._workflow_item_group_locked(int(row["id"])) for row in rows]

    def get_workflow_item_group(self, group_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM workflow_item_groups WHERE id = ?", (group_id,)
            ).fetchone()
            return self._workflow_item_group_locked(group_id) if row else None

    def get_workflow_item_scope(self, scope_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM workflow_item_scopes WHERE id = ?", (scope_id,)
            ).fetchone()
            return self._workflow_item_scope_locked(scope_id) if row else None

    def list_recoverable_workflow_item_groups(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT id FROM workflow_item_groups
                   WHERE status = 'active'
                      OR (status = 'completed' AND advanced_at IS NULL)
                   ORDER BY id"""
            ).fetchall()
            return [self._workflow_item_group_locked(int(row["id"])) for row in rows]

    def cancel_open_workflow_item_scopes(self, group_id: int, error: str = "") -> int:
        now = _now()
        with self._lock:
            scope_rows = self._conn.execute(
                """SELECT id FROM workflow_item_scopes
                   WHERE group_id = ? AND status IN ('pending', 'ready')""",
                (group_id,),
            ).fetchall()
            scope_ids = [int(row["id"]) for row in scope_rows]
            if scope_ids:
                placeholders = ",".join("?" for _ in scope_ids)
                self._conn.execute(
                    f"""UPDATE workflow_item_scopes
                        SET status = 'cancelled', error = ?, updated_at = ?, completed_at = ?
                        WHERE id IN ({placeholders})""",
                    (str(error or ""), now, now, *scope_ids),
                )
                self._conn.execute(
                    f"""UPDATE run_jobs
                        SET status = 'cancelled', note = ?, leased_until = NULL,
                            updated_at = ?, completed_at = ?
                        WHERE item_scope_id IN ({placeholders}) AND status = 'pending'""",
                    (str(error or "")[:2000], now, now, *scope_ids),
                )
                # Dual-write: cancel the per-item node runs of the scopes
                # that never got to run.
                self._guarded_mirror_locked(
                    "item_scope_cancel",
                    lambda: self._conn.execute(
                        f"""UPDATE node_runs
                            SET status = 'cancelled', summary = ?, updated_at = ?,
                                completed_at = ?
                            WHERE item_scope_id IN ({placeholders})
                              AND completed_at IS NULL""",
                        (str(error or "")[:2000], now, now, *scope_ids),
                    ),
                )
            self._conn.execute(
                """UPDATE workflow_item_groups
                   SET status = 'blocked', updated_at = ? WHERE id = ?""",
                (now, group_id),
            )
            self._conn.commit()
        return len(scope_ids)

    def mark_workflow_item_group_advanced(self, group_id: int) -> bool:
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                """UPDATE workflow_item_groups
                   SET advanced_at = COALESCE(advanced_at, ?), updated_at = ?
                   WHERE id = ?""",
                (now, now, group_id),
            )
            self._conn.commit()
        return cur.rowcount == 1

    def _refresh_workflow_item_group_locked(self, group_id: int, now: str) -> None:
        group = self._conn.execute(
            "SELECT max_concurrency FROM workflow_item_groups WHERE id = ?", (group_id,)
        ).fetchone()
        rows = self._conn.execute(
            "SELECT * FROM workflow_item_scopes WHERE group_id = ? ORDER BY item_index",
            (group_id,),
        ).fetchall()
        if not rows:
            return
        states = {str(row["scope_key"]): str(row["status"]) for row in rows}
        if any(state in {"blocked", "cancelled"} for state in states.values()):
            self._conn.execute(
                """UPDATE workflow_item_groups
                   SET status = 'blocked', updated_at = ? WHERE id = ?""",
                (now, group_id),
            )
            return
        if all(state == "completed" for state in states.values()):
            self._conn.execute(
                """UPDATE workflow_item_groups
                   SET status = 'completed', updated_at = ?, completed_at = ? WHERE id = ?""",
                (now, now, group_id),
            )
            return
        occupied = sum(state in {"ready", "running"} for state in states.values())
        available = max(0, int(group["max_concurrency"]) - occupied)
        for row in rows:
            if available <= 0:
                break
            if row["status"] != "pending":
                continue
            try:
                dependencies = json.loads(row["depends_on"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                dependencies = []
            if all(states.get(str(key)) == "completed" for key in dependencies):
                self._conn.execute(
                    """UPDATE workflow_item_scopes
                       SET status = 'ready', updated_at = ? WHERE id = ?""",
                    (now, int(row["id"])),
                )
                states[str(row["scope_key"])] = "ready"
                available -= 1

    def _workflow_item_group_locked(self, group_id: int) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM workflow_item_groups WHERE id = ?", (group_id,)
        ).fetchone()
        result = dict(row)
        scopes = self._conn.execute(
            "SELECT id FROM workflow_item_scopes WHERE group_id = ? ORDER BY item_index",
            (group_id,),
        ).fetchall()
        result["scopes"] = [
            self._workflow_item_scope_locked(int(scope["id"])) for scope in scopes
        ]
        return result

    def _workflow_item_scope_locked(self, scope_id: int) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM workflow_item_scopes WHERE id = ?", (scope_id,)
        ).fetchone()
        result = dict(row)
        for field, fallback in (("item_value", None), ("depends_on", []), ("output", {})):
            try:
                result[field] = json.loads(result.get(field) or "null")
            except (TypeError, ValueError, json.JSONDecodeError):
                result[field] = fallback
        return result

    @staticmethod
    def _workflow_node_result_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["output"] = _decode_step_inputs(result.get("output", ""))
        try:
            artifacts = json.loads(result.get("artifacts") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            artifacts = []
        result["artifacts"] = artifacts if isinstance(artifacts, list) else []
        return result

    # ------------------------------------------------------------------
    # Workflow-run record layer (workflow_runs / node_runs / workflow_tokens)
    #
    # These tables are the persistence model of the general workflow runtime
    # (docs/general-workflow-design.md §10.3). During the transition they are
    # DUAL-WRITTEN from the existing engine events — task transitions, run-job
    # creation, item-scope settlement and task status convergence — while
    # tasks/task_transitions/run_jobs stay the source of truth for every
    # routing decision. A dual-write failure must never break the primary
    # flow: mirror writes run inside a SAVEPOINT and roll themselves back on
    # error (see _guarded_mirror_locked).
    # ------------------------------------------------------------------

    @staticmethod
    def _workflow_run_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for key in ("definition_snapshot", "variables"):
            try:
                item[key] = json.loads(item.get(key) or "{}")
            except (TypeError, ValueError):
                item[key] = {}
        return item

    def create_workflow_run(
        self,
        task_id: int,
        definition: dict[str, Any] | None = None,
        variables: dict[str, Any] | None = None,
        entry_steps: list[str] | None = None,
        parent_run_id: int | None = None,
        parent_node_run_id: int | None = None,
    ) -> dict[str, Any]:
        """Create the workflow_run record for a task entering the workflow.

        Idempotent per task (task_id is UNIQUE): an existing run is returned
        unchanged. Entry tokens ('' -> entry step) are emitted in the same
        transaction so the token chain starts with the run itself.

        `parent_run_id`/`parent_node_run_id` record cross-run lineage: the run
        (and the node execution in it) that spawned this run — a goal's
        decompose split or a subflow node. `root_run_id` denormalizes the tree
        root so a whole lineage tree reads in one indexed query."""
        now = _now()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflow_runs WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is not None:
                return self._workflow_run_row(row)
            task = self._conn.execute(
                "SELECT id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise InvalidInputError(f"unknown task: {task_id}")
            root_run_id: int | None = None
            if parent_run_id is not None:
                parent = self._conn.execute(
                    "SELECT id, root_run_id FROM workflow_runs WHERE id = ?",
                    (parent_run_id,),
                ).fetchone()
                if parent is None:
                    raise InvalidInputError(
                        f"unknown parent workflow run: {parent_run_id}"
                    )
                root_run_id = int(parent["root_run_id"] or parent["id"])
            cur = self._conn.execute(
                """INSERT INTO workflow_runs (
                       task_id, definition_snapshot, status, variables,
                       parent_run_id, parent_node_run_id, root_run_id,
                       created_at, updated_at
                   ) VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    json.dumps(definition or {}, ensure_ascii=False),
                    json.dumps(variables or {}, ensure_ascii=False),
                    parent_run_id,
                    parent_node_run_id,
                    root_run_id,
                    now,
                    now,
                ),
            )
            run_id = cur.lastrowid
            if root_run_id is None:
                # A run with no parent roots its own lineage tree.
                self._conn.execute(
                    "UPDATE workflow_runs SET root_run_id = ? WHERE id = ?",
                    (run_id, run_id),
                )
            for step in entry_steps or []:
                step = (step or "").strip()
                if step:
                    self._insert_workflow_token_locked(
                        run_id, "", step, "", None, None, now
                    )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM workflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return self._workflow_run_row(row)

    def get_workflow_run(self, run_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return self._workflow_run_row(row) if row else None

    def get_workflow_run_by_task(self, task_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflow_runs WHERE task_id = ?", (task_id,)
            ).fetchone()
        return self._workflow_run_row(row) if row else None

    def update_workflow_run_variables(
        self, run_id: int, **patch: Any
    ) -> dict[str, Any] | None:
        """Merge `patch` into a workflow run's `variables` JSON (record layer
        only — variables never drive routing). Merge semantics: existing keys
        outside the patch are kept, a key patched to None is removed. Returns
        the updated run, or None for an unknown run."""
        with self._lock:
            row = self._conn.execute(
                "SELECT variables FROM workflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            try:
                variables = json.loads(row["variables"] or "{}")
            except (TypeError, ValueError):
                variables = {}
            if not isinstance(variables, dict):
                variables = {}
            for key, value in patch.items():
                if value is None:
                    variables.pop(key, None)
                else:
                    variables[key] = value
            self._conn.execute(
                "UPDATE workflow_runs SET variables = ?, updated_at = ? WHERE id = ?",
                (json.dumps(variables, ensure_ascii=False), _now(), run_id),
            )
            self._conn.commit()
        return self.get_workflow_run(run_id)

    def list_workflow_run_lineage(self, root_run_id: int) -> list[dict[str, Any]]:
        """Every run in the lineage tree rooted at `root_run_id`, the root
        itself included, ordered by id (parents always precede children)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflow_runs WHERE root_run_id = ? ORDER BY id",
                (root_run_id,),
            ).fetchall()
        return [self._workflow_run_row(row) for row in rows]

    def set_workflow_run_status(self, run_id: int, status: str) -> bool:
        status = (status or "").strip()
        if status not in WORKFLOW_RUN_STATUSES:
            raise InvalidInputError(
                f"invalid workflow run status: {status!r} "
                f"(expected one of {sorted(WORKFLOW_RUN_STATUSES)})"
            )
        now = _now()
        terminal = status in {"succeeded", "failed", "cancelled"}
        with self._lock:
            cur = self._conn.execute(
                """UPDATE workflow_runs
                   SET status = ?, updated_at = ?, completed_at = ?
                   WHERE id = ?""",
                (status, now, now if terminal else None, run_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def record_node_run(
        self,
        workflow_run_id: int,
        step: str,
        *,
        executor: str = "",
        status: str = "running",
        item_scope_id: int | None = None,
        run_job_id: int | None = None,
        consume_tokens: bool = True,
    ) -> dict[str, Any]:
        """Record a node execution attempt. Consumes the pending token(s)
        targeting the step in the same transaction (a join consumes several)."""
        step = (step or "").strip()
        if not step:
            raise InvalidInputError("node run step is required")
        if status not in NODE_RUN_STATUSES:
            raise InvalidInputError(
                f"invalid node run status: {status!r} "
                f"(expected one of {sorted(NODE_RUN_STATUSES)})"
            )
        now = _now()
        with self._lock:
            run = self._conn.execute(
                "SELECT id, task_id FROM workflow_runs WHERE id = ?",
                (workflow_run_id,),
            ).fetchone()
            if run is None:
                raise InvalidInputError(f"unknown workflow run: {workflow_run_id}")
            if consume_tokens:
                self._consume_workflow_tokens_locked(workflow_run_id, step, now)
            node_id = self._create_node_run_locked(
                workflow_run_id,
                int(run["task_id"]),
                step,
                executor=executor,
                status=status,
                item_scope_id=item_scope_id,
                run_job_id=run_job_id,
                now=now,
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM node_runs WHERE id = ?", (node_id,)
            ).fetchone()
        return dict(row)

    def finish_node_run(
        self,
        node_run_id: int,
        status: str,
        *,
        port: str = "",
        summary: str = "",
        tokens: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Settle a node run and emit its outgoing tokens in ONE transaction —
        the §10.1 invariant: result row and output tokens commit together."""
        if status not in NODE_RUN_STATUSES:
            raise InvalidInputError(
                f"invalid node run status: {status!r} "
                f"(expected one of {sorted(NODE_RUN_STATUSES)})"
            )
        now = _now()
        with self._lock:
            row = self._conn.execute(
                "SELECT id, workflow_run_id FROM node_runs WHERE id = ?",
                (node_run_id,),
            ).fetchone()
            if row is None:
                return None
            self._finish_node_run_locked(node_run_id, status, port, summary, now)
            for token in tokens or []:
                to_step = str(token.get("to_step") or "").strip()
                if not to_step:
                    raise InvalidInputError("workflow token to_step is required")
                self._insert_workflow_token_locked(
                    int(row["workflow_run_id"]),
                    str(token.get("from_step") or ""),
                    to_step,
                    str(token.get("port") or ""),
                    node_run_id,
                    token.get("item_scope_id"),
                    now,
                )
            self._conn.commit()
            settled = self._conn.execute(
                "SELECT * FROM node_runs WHERE id = ?", (node_run_id,)
            ).fetchone()
        return dict(settled) if settled else None

    def emit_workflow_token(
        self,
        workflow_run_id: int,
        to_step: str,
        *,
        from_step: str = "",
        port: str = "",
        node_run_id: int | None = None,
        item_scope_id: int | None = None,
    ) -> dict[str, Any]:
        to_step = (to_step or "").strip()
        if not to_step:
            raise InvalidInputError("workflow token to_step is required")
        now = _now()
        with self._lock:
            token_id = self._insert_workflow_token_locked(
                workflow_run_id, from_step, to_step, port,
                node_run_id, item_scope_id, now,
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM workflow_tokens WHERE id = ?", (token_id,)
            ).fetchone()
        return dict(row)

    def consume_workflow_tokens(self, workflow_run_id: int, to_step: str) -> int:
        """Mark every pending token targeting `to_step` consumed; returns count."""
        now = _now()
        with self._lock:
            consumed = self._consume_workflow_tokens_locked(
                workflow_run_id, (to_step or "").strip(), now
            )
            self._conn.commit()
        return consumed

    def get_node_run(self, node_run_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM node_runs WHERE id = ?", (node_run_id,)
            ).fetchone()
        return dict(row) if row else None

    def latest_node_run(
        self, workflow_run_id: int, step: str
    ) -> dict[str, Any] | None:
        """Most recent step-level (non-item) node run for `step` in the run,
        settled or not — used to anchor a child run's lineage to the node
        execution that spawned it."""
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM node_runs
                   WHERE workflow_run_id = ? AND step = ? AND item_scope_id IS NULL
                   ORDER BY id DESC LIMIT 1""",
                (workflow_run_id, (step or "").strip()),
            ).fetchone()
        return dict(row) if row else None

    def list_node_runs(
        self,
        workflow_run_id: int | None = None,
        task_id: int | None = None,
        step: str = "",
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        params: list[Any] = []
        if workflow_run_id is not None:
            filters.append("workflow_run_id = ?")
            params.append(workflow_run_id)
        if task_id is not None:
            filters.append("task_id = ?")
            params.append(task_id)
        if step:
            filters.append("step = ?")
            params.append(step)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM node_runs {where} ORDER BY id", params
            ).fetchall()
        return [dict(row) for row in rows]

    def list_workflow_tokens(
        self, workflow_run_id: int, status: str = ""
    ) -> list[dict[str, Any]]:
        where = "WHERE workflow_run_id = ?" + (" AND status = ?" if status else "")
        params: list[Any] = [workflow_run_id] + ([status] if status else [])
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM workflow_tokens {where} ORDER BY id", params
            ).fetchall()
        return [dict(row) for row in rows]

    # -- internal record-layer primitives (called with self._lock held; no
    #    commit of their own so callers control transaction boundaries) -----

    def _insert_workflow_token_locked(
        self,
        workflow_run_id: int,
        from_step: str,
        to_step: str,
        port: str,
        node_run_id: int | None,
        item_scope_id: int | None,
        now: str,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO workflow_tokens (
                   workflow_run_id, node_run_id, from_step, to_step, port,
                   item_scope_id, status, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (
                workflow_run_id, node_run_id, from_step or "", to_step,
                port or "", item_scope_id, now,
            ),
        )
        return int(cur.lastrowid)

    def _consume_workflow_tokens_locked(
        self, workflow_run_id: int, to_step: str, now: str
    ) -> int:
        cur = self._conn.execute(
            """UPDATE workflow_tokens
               SET status = 'consumed', consumed_at = ?
               WHERE workflow_run_id = ? AND to_step = ? AND status = 'pending'""",
            (now, workflow_run_id, to_step),
        )
        return int(cur.rowcount)

    def _create_node_run_locked(
        self,
        workflow_run_id: int,
        task_id: int,
        step: str,
        *,
        executor: str,
        status: str,
        item_scope_id: int | None,
        run_job_id: int | None,
        now: str,
    ) -> int:
        # Attempt counts retries of the same execution slot: the step itself,
        # or one foreach item of the step (per item_scope).
        if item_scope_id is None:
            row = self._conn.execute(
                """SELECT COALESCE(MAX(attempt), 0) AS n FROM node_runs
                   WHERE workflow_run_id = ? AND step = ? AND item_scope_id IS NULL""",
                (workflow_run_id, step),
            ).fetchone()
        else:
            row = self._conn.execute(
                """SELECT COALESCE(MAX(attempt), 0) AS n FROM node_runs
                   WHERE workflow_run_id = ? AND step = ? AND item_scope_id = ?""",
                (workflow_run_id, step, item_scope_id),
            ).fetchone()
        attempt = int(row["n"]) + 1
        cur = self._conn.execute(
            """INSERT INTO node_runs (
                   workflow_run_id, task_id, step, attempt, item_scope_id,
                   executor, status, run_job_id, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                workflow_run_id, task_id, step, attempt, item_scope_id,
                (executor or "")[:120], status, run_job_id, now, now,
            ),
        )
        return int(cur.lastrowid)

    def _finish_node_run_locked(
        self, node_run_id: int, status: str, port: str, summary: str, now: str
    ) -> None:
        self._conn.execute(
            """UPDATE node_runs
               SET status = ?, port = ?, summary = ?, updated_at = ?, completed_at = ?
               WHERE id = ?""",
            (status, port or "", (summary or "")[:2000], now, now, node_run_id),
        )

    def _open_node_run_locked(
        self, workflow_run_id: int, step: str
    ) -> sqlite3.Row | None:
        return self._conn.execute(
            """SELECT id, status FROM node_runs
               WHERE workflow_run_id = ? AND step = ? AND item_scope_id IS NULL
                 AND completed_at IS NULL
               ORDER BY id DESC LIMIT 1""",
            (workflow_run_id, step),
        ).fetchone()

    def _guarded_mirror_locked(self, label: str, mirror: Callable[[], None]) -> None:
        """Run one dual-write mirror inside a SAVEPOINT: the mirror commits
        with the primary write (same transaction) but rolls only itself back
        on failure, so a record-layer bug can never break the engine."""
        try:
            self._conn.execute("SAVEPOINT wf_mirror")
        except sqlite3.Error:
            _log.exception("workflow record mirror (%s): savepoint failed", label)
            return
        try:
            mirror()
            self._conn.execute("RELEASE SAVEPOINT wf_mirror")
        except Exception:
            try:
                self._conn.execute("ROLLBACK TO SAVEPOINT wf_mirror")
                self._conn.execute("RELEASE SAVEPOINT wf_mirror")
            except sqlite3.Error:
                pass
            _log.exception(
                "workflow record mirror (%s) failed; primary write unaffected",
                label,
            )

    # Engine transition outcome -> node run execution status (§10.2: execution
    # state and business result stay orthogonal; the business result is `port`).
    _TRANSITION_NODE_STATUS = {
        "rework": "succeeded",   # reviewer ran fine; the business port says redo
        "skipped": "skipped",
        "blocked": "blocked",
        "cancelled": "cancelled",
        "error": "failed",
        "timeout": "failed",
        "reassigned": "failed",  # normalization retry / stalled-runner takeover
    }
    # Recovery routes keep transition outcome `done` while the port carries the
    # real reason; map those ports back to the execution status.
    _FAILURE_PORT_NODE_STATUS = {
        "error": "failed",
        "timeout": "failed",
        "blocked": "blocked",
        "cancelled": "cancelled",
    }
    _TOKEN_EMITTING_OUTCOMES = {"done", "rework", "skipped"}

    def _mirror_transition_locked(
        self,
        task_id: int,
        from_step: str,
        to_step: str,
        outcome: str,
        note: str,
        port: str,
        now: str,
    ) -> None:
        run = self._conn.execute(
            "SELECT id FROM workflow_runs WHERE task_id = ?", (task_id,)
        ).fetchone()
        if run is None:
            return  # task not enrolled in the record layer (pre-existing data)
        run_id = int(run["id"])
        if outcome == "dispatched":
            if not to_step:
                return
            self._consume_workflow_tokens_locked(run_id, to_step, now)
            stale = self._open_node_run_locked(run_id, to_step)
            if stale is not None:
                # A manual re-run (or takeover) supersedes the open attempt.
                self._finish_node_run_locked(
                    int(stale["id"]), "cancelled", "",
                    "superseded by re-dispatch", now,
                )
            self._create_node_run_locked(
                run_id, task_id, to_step,
                executor=note, status="running",
                item_scope_id=None, run_job_id=None, now=now,
            )
            return
        if not from_step:
            return
        if outcome == "approval":
            open_run = self._open_node_run_locked(run_id, from_step)
            if open_run is not None:
                self._conn.execute(
                    """UPDATE node_runs
                       SET status = 'waiting', port = ?, summary = ?, updated_at = ?
                       WHERE id = ?""",
                    (port or "", (note or "")[:2000], now, int(open_run["id"])),
                )
            return
        if outcome == "done":
            status = self._FAILURE_PORT_NODE_STATUS.get(port, "succeeded")
        else:
            status = self._TRANSITION_NODE_STATUS.get(outcome)
        if status is None:
            return  # bookkeeping outcome (e.g. not_selected) — no node effect
        node_run_id: int | None = None
        open_run = self._open_node_run_locked(run_id, from_step)
        if open_run is not None:
            effective_port = port or (
                "rework" if outcome == "rework"
                else "success" if outcome == "done" else ""
            )
            self._finish_node_run_locked(
                int(open_run["id"]), status, effective_port, note, now
            )
            node_run_id = int(open_run["id"])
        if (
            outcome in self._TOKEN_EMITTING_OUTCOMES
            and to_step
            and to_step != from_step
        ):
            if node_run_id is None:
                # Multi-target completion: the first transition already settled
                # the node run; attach later tokens to the same node.
                latest = self._conn.execute(
                    """SELECT id FROM node_runs
                       WHERE workflow_run_id = ? AND step = ? AND item_scope_id IS NULL
                       ORDER BY id DESC LIMIT 1""",
                    (run_id, from_step),
                ).fetchone()
                node_run_id = int(latest["id"]) if latest else None
            token_port = port or ("rework" if outcome == "rework" else "success")
            self._insert_workflow_token_locked(
                run_id, from_step, to_step, token_port, node_run_id, None, now
            )

    def _mirror_run_job_locked(
        self,
        job_id: int,
        task_id: int,
        step: str,
        assignee: str,
        item_scope_id: int | None,
        now: str,
    ) -> None:
        run = self._conn.execute(
            "SELECT id FROM workflow_runs WHERE task_id = ?", (task_id,)
        ).fetchone()
        if run is None:
            return
        run_id = int(run["id"])
        if item_scope_id is None:
            row = self._conn.execute(
                """SELECT id FROM node_runs
                   WHERE workflow_run_id = ? AND step = ? AND item_scope_id IS NULL
                     AND completed_at IS NULL AND run_job_id IS NULL
                   ORDER BY id DESC LIMIT 1""",
                (run_id, step),
            ).fetchone()
            if row is not None:
                self._conn.execute(
                    """UPDATE node_runs
                       SET run_job_id = ?, executor = ?, updated_at = ?
                       WHERE id = ?""",
                    (job_id, (assignee or "")[:120], now, int(row["id"])),
                )
            return
        # A foreach item execution gets its own node run scoped to the item.
        self._create_node_run_locked(
            run_id, task_id, step,
            executor=assignee, status="running",
            item_scope_id=item_scope_id, run_job_id=job_id, now=now,
        )

    def _mirror_item_scope_settled_locked(
        self, scope_id: int, status: str, output: Any, error: str, now: str
    ) -> None:
        node_status = {
            "completed": "succeeded",
            "blocked": "blocked",
            "cancelled": "cancelled",
        }.get(status)
        if node_status is None:
            return
        row = self._conn.execute(
            """SELECT id FROM node_runs
               WHERE item_scope_id = ? AND completed_at IS NULL
               ORDER BY id DESC LIMIT 1""",
            (scope_id,),
        ).fetchone()
        if row is None:
            return
        port = ""
        summary = str(error or "")
        if isinstance(output, dict):
            port = str(output.get("port") or "")
            summary = str(output.get("summary") or "") or summary
        if not port:
            port = "success" if node_status == "succeeded" else node_status
        self._finish_node_run_locked(int(row["id"]), node_status, port, summary, now)

    def _mirror_task_status_locked(
        self, task_id: int, status: str, is_goal: bool, now: str
    ) -> None:
        run = self._conn.execute(
            "SELECT id, status FROM workflow_runs WHERE task_id = ?", (task_id,)
        ).fetchone()
        if run is None:
            return
        if is_goal:
            new_status = {
                "accepted": "succeeded",
                "closed": "cancelled",
                "stalled": "blocked",
            }.get(status, "running")
        else:
            new_status = {
                "closed": "succeeded",
                "blocked": "blocked",
            }.get(status, "running")
        if new_status == str(run["status"]):
            return
        terminal = new_status in {"succeeded", "failed", "cancelled"}
        self._conn.execute(
            """UPDATE workflow_runs
               SET status = ?, updated_at = ?, completed_at = ?
               WHERE id = ?""",
            (new_status, now, now if terminal else None, int(run["id"])),
        )

    def set_task_workflow_state(
        self,
        task_id: int,
        workflow_step: str | None = None,
        task_status: str | None = None,
        assignee: str | None = None,
    ) -> bool:
        if workflow_step is None and task_status is None and assignee is None:
            return False
        with self._lock:
            # Validate the status against the row's own vocabulary (goal rows use
            # GOAL_STATUSES, tasks/cards use TASK_STATUSES) — so this single method
            # serves both without ever letting a status cross the boundary.
            is_goal = self._row_is_goal_locked(task_id)
            if task_status is not None:
                task_status = _validate_status_for(is_goal, task_status)
            updates: list[str] = []
            params: list[Any] = []
            if workflow_step is not None:
                updates.append("workflow_step = ?")
                params.append(workflow_step)
            if task_status is not None:
                updates.append("status = ?")
                params.append(task_status)
            if assignee is not None:
                updates.append("assignee = ?")
                params.append(assignee)
            if not updates:
                return False
            updates.append("updated_at = ?")
            params.append(_now())
            params.append(task_id)
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
            if task_status is not None:
                # Dual-write: converge the workflow_run status with the task's
                # lifecycle (accepted -> succeeded, stalled/blocked -> blocked,
                # force close -> cancelled, resume -> running).
                status_value = task_status
                self._guarded_mirror_locked(
                    "task_status",
                    lambda: self._mirror_task_status_locked(
                        task_id, status_value, is_goal, _now()
                    ),
                )
            self._conn.commit()
        return cur.rowcount > 0

    def _row_is_goal_locked(self, task_id: int) -> bool:
        row = self._conn.execute(
            "SELECT is_goal FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return bool(row["is_goal"]) if row else False

    def record_task_transition(
        self,
        task_id: int,
        from_step: str,
        to_step: str,
        actor: str,
        outcome: str,
        note: str = "",
        port: str = "",
    ) -> dict[str, Any]:
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO task_transitions (
                       task_id, from_step, to_step, actor, outcome, port, note, created_at
                   )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, from_step, to_step, actor, outcome, port, note[:2000], now),
            )
            # Dual-write the new-runtime record layer in the SAME transaction:
            # a completion settles its node run and emits the outgoing token
            # atomically with the transition (§10.1).
            self._guarded_mirror_locked(
                "transition",
                lambda: self._mirror_transition_locked(
                    task_id, from_step, to_step, outcome, note[:2000], port, now
                ),
            )
            self._conn.commit()
        return {
            "id": cur.lastrowid,
            "task_id": task_id,
            "from_step": from_step,
            "to_step": to_step,
            "actor": actor,
            "outcome": outcome,
            "port": port,
            "note": note[:2000],
            "created_at": now,
        }

    def list_task_transitions(self, task_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, task_id, from_step, to_step, actor, outcome, port, note,
                          created_at
                   FROM task_transitions
                   WHERE task_id = ?
                   ORDER BY id""",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_task_transitions_for_tasks(
        self, task_ids: list[int]
    ) -> dict[int, list[dict[str, Any]]]:
        """Load transitions for many tasks without one query per board row."""
        ids = list(dict.fromkeys(int(task_id) for task_id in task_ids))
        grouped = {task_id: [] for task_id in ids}
        if not ids:
            return grouped
        with self._lock:
            for offset in range(0, len(ids), 500):
                chunk = ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = self._conn.execute(
                    f"""SELECT id, task_id, from_step, to_step, actor, outcome,
                               port, note, created_at
                        FROM task_transitions
                        WHERE task_id IN ({placeholders})
                        ORDER BY id""",
                    chunk,
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    grouped[int(item["task_id"])].append(item)
        return grouped

    def count_step_dispatches(self, step_id: str) -> int:
        """Round-robin cursor source: how many distinct tasks have entered this
        step. Distinct — not raw dispatch rows — so a task's reworks (which
        re-dispatch the same step) never skew the rotation for later tasks."""
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(DISTINCT task_id) AS n FROM task_transitions
                   WHERE outcome = 'dispatched' AND to_step = ?""",
                (step_id,),
            ).fetchone()
        return int(row["n"] if row else 0)

    def create_workflow_action(
        self,
        task_id: int,
        action_type: str,
        step: str = "",
        assignee: str = "",
        status: str = "pending",
        note: str = "",
    ) -> dict[str, Any] | None:
        now = _now()
        action_type = (action_type or "").strip()
        status = (status or "pending").strip()
        if not action_type:
            raise InvalidInputError("action_type is required")
        if not status:
            raise InvalidInputError("status is required")
        with self._lock:
            task = self._conn.execute(
                "SELECT id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                return None
            cur = self._conn.execute(
                """INSERT INTO workflow_actions (
                       task_id, action_type, step, assignee, status, note,
                       created_at, updated_at
                   )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    action_type,
                    step,
                    assignee,
                    status,
                    note[:2000],
                    now,
                    now,
                ),
            )
            self._conn.commit()
        return self.get_workflow_action(cur.lastrowid)

    def get_workflow_action(self, action_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT id, task_id, action_type, step, assignee, status, note,
                          created_at, updated_at, completed_at
                   FROM workflow_actions
                   WHERE id = ?""",
                (action_id,),
            ).fetchone()
        return dict(row) if row else None

    def finish_workflow_action(
        self, action_id: int, status: str = "done", note: str = ""
    ) -> dict[str, Any] | None:
        status = (status or "").strip()
        if not status:
            raise InvalidInputError("status is required")
        now = _now()
        completed_at = now if status in {"done", "failed", "alerted"} else None
        with self._lock:
            cur = self._conn.execute(
                """UPDATE workflow_actions
                   SET status = ?, note = CASE WHEN ? = '' THEN note ELSE ? END,
                       updated_at = ?, completed_at = ?
                   WHERE id = ?""",
                (status, note[:2000], note[:2000], now, completed_at, action_id),
            )
            self._conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_workflow_action(action_id)

    def has_workflow_action(self, task_id: int, action_type: str) -> bool:
        """Whether any workflow_action of this type exists for the task (any
        status). Used to make one-shot actions (e.g. budget-exceeded) idempotent
        across the many callers that recompute goal status."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM workflow_actions WHERE task_id = ? AND action_type = ? LIMIT 1",
                (task_id, (action_type or "").strip()),
            ).fetchone()
        return row is not None

    def has_pending_workflow_action(self, task_id: int, action_type: str) -> bool:
        """Whether an in-flight (pending/running) workflow_action of this type
        exists. Unlike has_workflow_action, a done/failed action does NOT count —
        so a retryable action (e.g. goal verification) can be re-queued after a
        prior attempt finished, without duplicating one that is still queued."""
        with self._lock:
            row = self._conn.execute(
                """SELECT 1 FROM workflow_actions
                   WHERE task_id = ? AND action_type = ?
                     AND status IN ('pending', 'running') LIMIT 1""",
                (task_id, (action_type or "").strip()),
            ).fetchone()
        return row is not None

    def list_workflow_actions(
        self, status: str = "pending", limit: int = 100
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        params: list[Any] = []
        where = ""
        if status != "all":
            where = "WHERE status = ?"
            params.append((status or "").strip())
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT id, task_id, action_type, step, assignee, status, note,
                           created_at, updated_at, completed_at
                    FROM workflow_actions
                    {where}
                    ORDER BY id DESC
                    LIMIT ?""",
                (*params, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_run_job(
        self,
        task_id: int,
        step: str,
        assignee: str,
        command: str,
        upstream_result: str = "",
        note: str = "",
        item_scope_id: int | None = None,
    ) -> dict[str, Any] | None:
        now = _now()
        with self._lock:
            task = self._conn.execute(
                "SELECT id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                return None
            if item_scope_id is not None:
                scope = self._conn.execute(
                    """SELECT s.status, g.task_id, g.foreach_step
                       FROM workflow_item_scopes s
                       JOIN workflow_item_groups g ON g.id = s.group_id
                       WHERE s.id = ?""",
                    (item_scope_id,),
                ).fetchone()
                if (
                    scope is None
                    or int(scope["task_id"]) != task_id
                    or str(scope["foreach_step"]) != (step or "").strip()
                    or str(scope["status"]) != "ready"
                ):
                    return None
                open_job = self._conn.execute(
                    """SELECT 1 FROM run_jobs
                       WHERE item_scope_id = ?
                         AND status IN ('pending', 'running', 'finished', 'applying')
                       LIMIT 1""",
                    (item_scope_id,),
                ).fetchone()
                if open_job is not None:
                    return None
                self._conn.execute(
                    """UPDATE workflow_item_scopes
                       SET status = 'running', updated_at = ?
                       WHERE id = ? AND status = 'ready'""",
                    (now, item_scope_id),
                )
            cur = self._conn.execute(
                """INSERT INTO run_jobs (
                       task_id, step, assignee, command, upstream_result, item_scope_id,
                       status, note, created_at, updated_at
                   )
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (
                    task_id,
                    (step or "").strip(),
                    (assignee or "").strip(),
                    (command or "").strip(),
                    upstream_result or "",
                    item_scope_id,
                    note[:2000],
                    now,
                    now,
                ),
            )
            job_id = int(cur.lastrowid)
            # Dual-write: link the queued job to its open node run (regular
            # step) or record a per-item node run (foreach item scope).
            self._guarded_mirror_locked(
                "run_job",
                lambda: self._mirror_run_job_locked(
                    job_id, task_id, (step or "").strip(),
                    (assignee or "").strip(), item_scope_id, now,
                ),
            )
            self._conn.commit()
        with self._job_condition:
            self._job_generation += 1
            self._job_condition.notify_all()
        return self.get_run_job(cur.lastrowid)

    def run_job_generation(self) -> int:
        with self._job_condition:
            return self._job_generation

    def wait_for_run_job(
        self,
        generation: int,
        stop_event: threading.Event,
        timeout: float = 30.0,
    ) -> int:
        """Wait until this in-process Store receives a newly queued run job."""
        with self._job_condition:
            self._job_condition.wait_for(
                lambda: self._job_generation != generation or stop_event.is_set(),
                timeout=max(0.1, float(timeout)),
            )
            return self._job_generation

    def wake_run_job_waiters(self) -> None:
        with self._job_condition:
            self._job_condition.notify_all()

    def get_run_job(self, job_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT id, task_id, step, assignee, command, upstream_result,
                          item_scope_id,
                          status, claimed_by, leased_until, note, outcome, result,
                          applied_by, created_at, updated_at, completed_at
                   FROM run_jobs
                   WHERE id = ?""",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_run_jobs_for_item_scope(self, item_scope_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM run_jobs WHERE item_scope_id = ? ORDER BY id",
                (item_scope_id,),
            ).fetchall()
        return [self.get_run_job(int(row["id"])) for row in rows]

    def has_open_run_job(self, task_id: int, step: str) -> bool:
        """Whether a dispatched step still has work queued or being applied."""
        with self._lock:
            row = self._conn.execute(
                """SELECT 1 FROM run_jobs
                   WHERE task_id = ? AND step = ?
                     AND status IN ('pending', 'running', 'finished', 'applying')
                   LIMIT 1""",
                (task_id, (step or "").strip()),
            ).fetchone()
        return row is not None

    def has_open_item_run_job(self, item_scope_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                """SELECT 1 FROM run_jobs
                   WHERE item_scope_id = ?
                     AND status IN ('pending', 'running', 'finished', 'applying')
                   LIMIT 1""",
                (item_scope_id,),
            ).fetchone()
        return row is not None

    def claim_next_run_job(
        self,
        runner_name: str,
        agents: list[str] | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        steps: list[str] | None = None,
    ) -> dict[str, Any] | None:
        now = _now()
        lease_until = _future(max(1, int(lease_seconds)))
        runner_name = (runner_name or "").strip()
        agents = [a.strip() for a in (agents or []) if a.strip()]
        steps = [s.strip() for s in (steps or []) if s.strip()]
        with self._lock:
            filters = [
                "(status = 'pending' OR (status = 'running' AND leased_until <= ?))"
            ]
            params: list[Any] = [now]
            if agents:
                placeholders = ",".join("?" for _ in agents)
                filters.append(f"assignee IN ({placeholders})")
                params.extend(agents)
            if steps:
                placeholders = ",".join("?" for _ in steps)
                filters.append(f"step IN ({placeholders})")
                params.extend(steps)
            row = self._conn.execute(
                f"""SELECT id FROM run_jobs
                    WHERE {' AND '.join(filters)}
                    ORDER BY id
                    LIMIT 1""",
                params,
            ).fetchone()
            if row is None:
                return None
            job_id = int(row["id"])
            cur = self._conn.execute(
                """UPDATE run_jobs
                   SET status = 'running',
                       claimed_by = ?,
                       leased_until = ?,
                       updated_at = ?
                   WHERE id = ?
                     AND (status = 'pending'
                          OR (status = 'running' AND leased_until <= ?))""",
                (runner_name, lease_until, now, job_id, now),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get_run_job(job_id)

    def renew_run_job(
        self, job_id: int, runner_name: str, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> bool:
        now = _now()
        lease_until = _future(max(1, int(lease_seconds)))
        with self._lock:
            cur = self._conn.execute(
                """UPDATE run_jobs
                   SET leased_until = ?, updated_at = ?
                   WHERE id = ? AND status = 'running' AND claimed_by = ?""",
                (lease_until, now, job_id, runner_name),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def finish_run_job(
        self,
        job_id: int,
        status: str = "done",
        note: str = "",
        outcome: str | None = None,
        result: str | None = None,
        runner_name: str | None = None,
        current_status: str | None = None,
        applied_by: str | None = None,
    ) -> dict[str, Any] | None:
        status = (status or "").strip()
        if not status:
            raise InvalidInputError("status is required")
        now = _now()
        # 'finished' means the runner executed and reported an outcome; the
        # scheduler still has to advance it, so it is not yet completed.
        completed_at = now if status in {"done", "failed", "cancelled"} else None
        # Compare-and-set guards: a runner only finishes a job it still holds
        # (claimed_by); a scheduler only finishes one it is applying (applied_by).
        filters = ["id = ?"]
        params_tail: list[Any] = [job_id]
        if runner_name is not None:
            filters.append("claimed_by = ?")
            params_tail.append((runner_name or "").strip())
        if applied_by is not None:
            filters.append("applied_by = ?")
            params_tail.append((applied_by or "").strip())
        if current_status is not None:
            filters.append("status = ?")
            params_tail.append((current_status or "").strip())
        with self._lock:
            cur = self._conn.execute(
                f"""UPDATE run_jobs
                   SET status = ?,
                       note = CASE WHEN ? = '' THEN note ELSE ? END,
                       outcome = CASE WHEN ? IS NULL THEN outcome ELSE ? END,
                       result = CASE WHEN ? IS NULL THEN result ELSE ? END,
                       leased_until = NULL,
                       updated_at = ?,
                       completed_at = ?
                   WHERE {' AND '.join(filters)}""",
                (
                    status,
                    note[:2000], note[:2000],
                    outcome, outcome,
                    result, (result or "")[:8000],
                    now, completed_at, *params_tail,
                ),
            )
            self._conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_run_job(job_id)

    def claim_finished_run_job(
        self,
        scheduler_name: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> dict[str, Any] | None:
        """Atomically claim one finished job for scheduler-side advancement.

        This keeps multiple UI/scheduler processes from applying the same
        runner result concurrently. An expired applying lease can be reclaimed.
        """
        now = _now()
        lease_until = _future(max(1, int(lease_seconds)))
        scheduler_name = (scheduler_name or "").strip()
        with self._lock:
            row = self._conn.execute(
                """SELECT id FROM run_jobs
                   WHERE status = 'finished'
                      OR (status = 'applying' AND leased_until <= ?)
                   ORDER BY id
                   LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            job_id = int(row["id"])
            cur = self._conn.execute(
                """UPDATE run_jobs
                   SET status = 'applying',
                       applied_by = ?,
                       leased_until = ?,
                       updated_at = ?
                   WHERE id = ?
                     AND (status = 'finished'
                          OR (status = 'applying' AND leased_until <= ?))""",
                (scheduler_name, lease_until, now, job_id, now),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get_run_job(job_id)

    def cancel_pending_run_jobs(
        self, task_id: int, step: str, note: str = ""
    ) -> int:
        """Cancel queued runner jobs for a step that was settled elsewhere.

        Running jobs are left alone: their subprocess already owns a task_run
        and should finish or time out normally.
        """
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                """UPDATE run_jobs
                   SET status = 'cancelled',
                       note = CASE WHEN ? = '' THEN note ELSE ? END,
                       leased_until = NULL,
                       updated_at = ?,
                       completed_at = ?
                   WHERE task_id = ? AND step = ? AND status = 'pending'""",
                (note[:2000], note[:2000], now, now, task_id, step),
            )
            self._conn.commit()
        return cur.rowcount

    def list_run_jobs(
        self, status: str = "all", limit: int = 100
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        params: list[Any] = []
        where = ""
        if status != "all":
            where = "WHERE status = ?"
            params.append((status or "").strip())
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT id, task_id, step, assignee, command, upstream_result,
                           item_scope_id,
                           status, claimed_by, leased_until, note, outcome, result,
                           applied_by, created_at, updated_at, completed_at
                    FROM run_jobs
                    {where}
                    ORDER BY id DESC
                    LIMIT ?""",
                (*params, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def reap_stale_runs(self) -> int:
        """Mark every 'running' task_run as orphaned. Runners live as threads
        inside the server process, so at server startup none can actually be
        running — leftovers would otherwise count against their worker's
        max_concurrent_tasks forever and starve assignment."""
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                """UPDATE task_runs
                   SET status = 'orphaned', finished_at = ?
                   WHERE status = 'running'""",
                (now,),
            )
            self._conn.commit()
        return cur.rowcount

    def count_running_run_jobs(self) -> int:
        """How many run_jobs a runner is actively executing right now (claimed,
        lease still valid). Used as a global concurrency cap before claiming."""
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS n FROM run_jobs
                   WHERE status = 'running' AND leased_until > ?""",
                (_now(),),
            ).fetchone()
        return int(row["n"]) if row else 0

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
        command: str = "",
        workflow_step: str = "",
    ) -> dict[str, Any] | None:
        """Create a task execution attempt and return its run metadata."""
        now = _now()
        worker = (worker or "").strip()
        status = (status or "running").strip()
        command = (command or "").strip()
        workflow_step = (workflow_step or "").strip()
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
                       task_id, attempt, worker, status, log_dir, command,
                       workflow_step, started_at
                   )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, attempt, worker, status, log_dir, command, workflow_step, now),
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
                          log_dir, command, tokens, workflow_step,
                          cancel_requested, cancel_reason, started_at, finished_at
                   FROM task_runs
                   WHERE task_id = ?
                   ORDER BY attempt DESC
                   LIMIT ?""",
                (task_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_running_task_runs(self) -> list[dict[str, Any]]:
        """Every task_run still marked running, with the fields the hub-inspect
        sweep needs (log_dir to gauge output/age, workflow_step for context)."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, task_id, worker, pid, log_dir, workflow_step,
                          cancel_requested, cancel_reason, started_at
                   FROM task_runs WHERE status = 'running'
                   ORDER BY id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def request_run_kill(self, run_id: int, note: str = "") -> bool:
        """Flag a running run for its owning runner to kill. The sweep sets this
        instead of killing a pid itself, so only the runner that owns the process
        (and its host) ever signals it — avoiding killing a reused/foreign pid."""
        with self._lock:
            cur = self._conn.execute(
                """UPDATE task_runs
                   SET cancel_requested = 1, cancel_reason = ?
                   WHERE id = ? AND status = 'running'""",
                ((note or "")[:2000], run_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def run_cancel_requested(self, run_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT cancel_requested FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def get_task_run(self, run_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT id, task_id, attempt, worker, status, exit_code,
                          log_dir, command, tokens, workflow_step,
                          cancel_requested, cancel_reason, started_at, finished_at
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
        tokens: int | None = None,
    ) -> dict[str, Any] | None:
        status = (status or "").strip()
        if not status:
            raise InvalidInputError("status is required")
        with self._lock:
            cur = self._conn.execute(
                """UPDATE task_runs
                   SET status = ?, exit_code = ?, finished_at = ?,
                       tokens = COALESCE(?, tokens),
                       cancel_requested = 0
                   WHERE id = ?""",
                (status, exit_code, _now(), tokens, run_id),
            )
            self._conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_task_run(run_id)

    def sum_goal_tokens(self, goal_id: int) -> int:
        """Total tokens across a goal's whole task subtree — the goal, its
        business subtasks, and every step card beneath them (runs are recorded
        on subtasks and cards). Walks parent_task_id recursively."""
        with self._lock:
            row = self._conn.execute(
                """WITH RECURSIVE tree(id) AS (
                       SELECT ?
                       UNION
                       SELECT t.id FROM tasks t JOIN tree ON t.parent_task_id = tree.id
                   )
                   SELECT COALESCE(SUM(r.tokens), 0) AS total
                   FROM task_runs r
                   WHERE r.task_id IN (SELECT id FROM tree)""",
                (goal_id,),
            ).fetchone()
        return int(row["total"]) if row else 0

    def set_task_run_pid(self, run_id: int, pid: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE task_runs SET pid = ? WHERE id = ?", (pid, run_id)
            )
            self._conn.commit()

    def running_run_pids_in_tree(self, root_id: int) -> list[int]:
        """PIDs of runners still running anywhere in a task's subtree (the task,
        its subtasks, and their step cards) — for force-terminating a goal."""
        with self._lock:
            rows = self._conn.execute(
                """WITH RECURSIVE tree(id) AS (
                       SELECT ?
                       UNION
                       SELECT t.id FROM tasks t JOIN tree ON t.parent_task_id = tree.id
                   )
                   SELECT pid FROM task_runs
                   WHERE status = 'running' AND pid IS NOT NULL
                     AND task_id IN (SELECT id FROM tree)""",
                (root_id,),
            ).fetchall()
        return [int(r["pid"]) for r in rows]

    def close_task_tree(self, root_id: int) -> int:
        """Close a task and its whole subtree and orphan any running runs there.
        Returns the number of tasks closed."""
        now = _now()
        tree_cte = """WITH RECURSIVE tree(id) AS (
                          SELECT ?
                          UNION
                          SELECT t.id FROM tasks t JOIN tree ON t.parent_task_id = tree.id
                      )"""
        with self._lock:
            # rowcount is unreliable for CTE UPDATEs, so count up front.
            row = self._conn.execute(
                tree_cte + """
                   SELECT COUNT(*) AS n FROM tasks
                   WHERE status NOT IN ('closed', 'accepted')
                     AND id IN (SELECT id FROM tree)""",
                (root_id,),
            ).fetchone()
            n = int(row["n"]) if row else 0
            self._conn.execute(
                tree_cte + """
                   UPDATE task_runs SET status = 'orphaned', finished_at = ?
                   WHERE status = 'running' AND task_id IN (SELECT id FROM tree)""",
                (root_id, now),
            )
            self._conn.execute(
                tree_cte + """
                   UPDATE tasks SET status = 'closed', updated_at = ?
                   WHERE status NOT IN ('closed', 'accepted')
                     AND id IN (SELECT id FROM tree)""",
                (root_id, now),
            )
            # Dual-write: a force close cancels every non-terminal workflow_run
            # in the tree, its open node runs, and its pending tokens.
            self._guarded_mirror_locked(
                "close_tree",
                lambda: self._mirror_close_tree_locked(tree_cte, root_id, now),
            )
            self._conn.commit()
        return n

    def _mirror_close_tree_locked(self, tree_cte: str, root_id: int, now: str) -> None:
        run_filter = """workflow_run_id IN (
                            SELECT id FROM workflow_runs
                            WHERE task_id IN (SELECT id FROM tree)
                        )"""
        self._conn.execute(
            tree_cte + f"""
               UPDATE node_runs
               SET status = 'cancelled', updated_at = ?, completed_at = ?
               WHERE completed_at IS NULL AND {run_filter}""",
            (root_id, now, now),
        )
        self._conn.execute(
            tree_cte + f"""
               UPDATE workflow_tokens
               SET status = 'cancelled', consumed_at = ?
               WHERE status = 'pending' AND {run_filter}""",
            (root_id, now),
        )
        self._conn.execute(
            tree_cte + """
               UPDATE workflow_runs
               SET status = 'cancelled', updated_at = ?, completed_at = ?
               WHERE status NOT IN ('succeeded', 'failed', 'cancelled')
                 AND task_id IN (SELECT id FROM tree)""",
            (root_id, now, now),
        )

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
            if self._closed:
                return
            self._closed = True
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
        task["is_goal"] = bool(task.get("is_goal", 0))
        task["depends_on"] = _decode_depends_on(task.get("depends_on", ""))
        task["step_inputs"] = _decode_step_inputs(task.get("step_inputs", ""))
        task["step_output"] = _decode_step_inputs(task.get("step_output", ""))
        task["artifacts"] = _decode_artifacts(task.get("artifacts", ""))
        return task
