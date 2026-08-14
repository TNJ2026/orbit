"""Durable application service for the isolated LangGraph execution adapter."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Callable, Mapping, Sequence
import uuid

from langgraph.checkpoint.sqlite import SqliteSaver

from ..api.graph_layout import graph_layout
from ..domain.serialization import canonical_json, definition_hash, to_primitive
from ..domain.ir_schema import workflow_ir_from_primitive
from .compiler import (
    LangGraphCompileError, LangGraphHandlerRegistry,
    LangGraphJoinDeadlineExceeded,
    LangGraphRetryRequested, LangGraphUnknownExternalResult, compile_workflow,
)


class LangGraphRunConflict(ValueError):
    pass


# A run that has not finished and could still do something. `unknown` is not
# here: nobody can tell whether it is still working, and holding the one goal
# slot against a run that may never settle would leave the operator with no
# way to start anything at all.
ACTIVE_STATUSES = ("running", "waiting", "interrupted")


class ActiveGoalExists(LangGraphRunConflict):
    """One goal at a time, and this actor already has one.

    Carries the run that holds the slot so the caller can be taken to it
    rather than told only that it exists.
    """

    def __init__(self, active: Mapping[str, Any]) -> None:
        super().__init__(
            f"an active goal already exists: {active['run_id']}"
        )
        self.active_goal = dict(active)


# A goal is display text, not an input. It is bounded because it is stored
# and shown, and refused rather than truncated: a person who pasted a page
# into the box should be told, not handed a run labelled with the first
# paragraph of what they meant.
MAX_GOAL_LENGTH = 4000


@dataclass(frozen=True)
class LangGraphRun:
    run_id: str
    workflow_id: str
    workflow_version: int
    status: str
    revision: int
    result: Any
    interrupts: tuple[Mapping[str, Any], ...]
    error: str | None
    created_at: str
    updated_at: str
    owner_actor: str
    template_id: str | None = None
    goal: str = ""
    artifact_count: int = 0


EVENT_LOG_DDL = """
    CREATE TABLE IF NOT EXISTS langgraph_run_events (
        position INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        revision INTEGER NOT NULL,
        occurred_at TEXT NOT NULL,
        node_id TEXT,
        attempt_id TEXT
    );
    CREATE INDEX IF NOT EXISTS langgraph_run_events_by_run
        ON langgraph_run_events(run_id, position);
"""


def ensure_event_log(connection) -> None:
    """Create the log if it is not there, and carry an older one forward.

    Both writers call this. The service owns the run tables and the Handler
    journal owns the attempt table, but the log is one stream that they share,
    so neither can assume the other built it first.
    """

    connection.executescript(EVENT_LOG_DDL)
    columns = {
        row["name"] for row in connection.execute(
            "PRAGMA table_info(langgraph_run_events)"
        )
    }
    for column in ("node_id", "attempt_id"):
        if column not in columns:
            connection.execute(
                f"ALTER TABLE langgraph_run_events ADD COLUMN {column} TEXT"
            )


def _goal(value: str) -> str:
    """A goal is text a person typed; this is all that is asked of it."""

    goal = "" if value is None else str(value).strip()
    if len(goal) > MAX_GOAL_LENGTH:
        raise ValueError(f"goal must be at most {MAX_GOAL_LENGTH} characters")
    return goal


def append_event(
    connection,
    run_id: str,
    event_type: str,
    *,
    revision: int | None = None,
    occurred_at: str | None = None,
    node_id: str | None = None,
    attempt_id: str | None = None,
) -> None:
    """Append one event to the run log, on a connection already in a write.

    Never on its own connection. An event the stream carries for a change that
    rolled back would tell a consumer to re-read state that was never written,
    and a change with no event would strand one waiting for it — so the append
    belongs to the transaction that made the change, and dies with it.

    `revision` is the run's, including on a node event: it is what a consumer
    needs to re-read the run and to pass back as `expected_version`. When it is
    not supplied it is read here, which is one lookup on a primary key inside a
    transaction that is already open.
    """

    if revision is None or occurred_at is None:
        try:
            row = connection.execute(
                "SELECT revision,updated_at FROM langgraph_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            # A Handler journal bound to a database with no run store — an
            # embedder using the adapter's Handler binding on its own. The
            # attempt still happened, so it is still recorded.
            row = None
        if row is None:
            revision = 0 if revision is None else revision
            occurred_at = occurred_at or datetime.now(timezone.utc).isoformat(
            ).replace("+00:00", "Z")
        else:
            if revision is None:
                revision = int(row["revision"])
            if occurred_at is None:
                occurred_at = row["updated_at"]
    connection.execute(
        "INSERT INTO langgraph_run_events"
        "(run_id,event_type,revision,occurred_at,node_id,attempt_id)"
        " VALUES (?,?,?,?,?,?)",
        (run_id, event_type, int(revision), occurred_at, node_id, attempt_id),
    )


class LangGraphWorkflowService:
    """Load published IR and execute it with durable, idempotent run metadata."""

    def __init__(
        self,
        workflow_versions,
        handlers: LangGraphHandlerRegistry,
        *,
        run_db_path: Path | str,
        checkpoint_db_path: Path | str,
        artifact_store=None,
        console=None,
        single_goal: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.workflow_versions = workflow_versions
        self.handlers = handlers
        self.run_db_path = Path(run_db_path)
        self.checkpoint_db_path = Path(checkpoint_db_path)
        self.artifacts = artifact_store
        # Read-side only: the Handler adapters write it, and they were handed
        # their own binding when they were built.
        self.console = console
        # A product shape rather than an engine invariant: one goal at a time
        # is what the single-agent UI is built around. Off by default, so an
        # embedder running many workflows is not quietly serialised.
        self.single_goal = bool(single_goal)
        # Runs started by a caller that did not wait. Created on first use so
        # a service that never defers one starts no threads at all.
        self._background: ThreadPoolExecutor | None = None
        self._background_runs: set = set()
        self._background_lock = threading.Lock()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # Runs this process is executing right now. `running` in the database
        # means "a process was executing this", and recovery exists because
        # that process may be gone — but it cannot tell a stranded run from
        # one in flight here, and re-entering a live thread_id makes the
        # attempt journal find a sibling's `started` row and settle a healthy
        # run as `unknown`. This is the part of that question this process can
        # answer for certain.
        self._in_flight: set[str] = set()
        self._in_flight_lock = threading.Lock()
        # Whether a definition compiles under this engine, by definition hash.
        # The catalog asks once per workflow per GET and the UI polls it on
        # every render and every composer submit, so a library of N workflows
        # meant N full `StateGraph` builds per poll. All three things the
        # answer depends on are immutable: the definition (content-addressed
        # by this key), the compiler, and this service's sealed Handler
        # registry — a registry is fixed at composition, so the instance is
        # its identity and the cache lives here rather than globally.
        self._compatibility: dict[str, Mapping[str, Any]] = {}
        self._compatibility_lock = threading.Lock()
        self.run_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint_db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS langgraph_runs (
                    run_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    workflow_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    input_json TEXT NOT NULL,
                    result_json TEXT,
                    interrupts_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    goal TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS langgraph_run_receipts (
                    idempotency_key TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    run_id TEXT NOT NULL REFERENCES langgraph_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS langgraph_handler_attempts (
                    attempt_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,status TEXT NOT NULL,output_json TEXT,
                    error TEXT,updated_at TEXT NOT NULL,
                    handler_name TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS langgraph_timers (
                    timer_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,node_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,due_at TEXT NOT NULL,
                    status TEXT NOT NULL,purpose TEXT NOT NULL DEFAULT 'retry',
                    target_id TEXT NOT NULL DEFAULT '',
                    UNIQUE(run_id,purpose,target_id,attempt_number)
                );
                """
            )
            ensure_event_log(connection)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(langgraph_runs)")
            }
            if "goal" not in columns:
                connection.execute(
                    "ALTER TABLE langgraph_runs ADD COLUMN goal TEXT NOT NULL"
                    " DEFAULT ''"
                )
            if "input_json" not in columns:
                connection.execute(
                    "ALTER TABLE langgraph_runs ADD COLUMN input_json"
                    " TEXT NOT NULL DEFAULT '{}'"
                )
            if "interrupts_json" not in columns:
                connection.execute(
                    "ALTER TABLE langgraph_runs ADD COLUMN interrupts_json"
                    " TEXT NOT NULL DEFAULT '[]'"
                )
            if "owner_actor" not in columns:
                connection.execute(
                    "ALTER TABLE langgraph_runs ADD COLUMN owner_actor"
                    " TEXT NOT NULL DEFAULT 'system:langgraph'"
                )
            if "interrupt_responses_json" not in columns:
                connection.execute(
                    "ALTER TABLE langgraph_runs ADD COLUMN interrupt_responses_json"
                    " TEXT NOT NULL DEFAULT '{}'"
                )
            if "template_id" not in columns:
                connection.execute(
                    "ALTER TABLE langgraph_runs ADD COLUMN template_id TEXT"
                )
            if "graph_snapshot_json" not in columns:
                connection.execute(
                    "ALTER TABLE langgraph_runs ADD COLUMN graph_snapshot_json TEXT"
                )
            timer_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(langgraph_timers)")
            }
            if "purpose" not in timer_columns:
                connection.execute(
                    "ALTER TABLE langgraph_timers ADD COLUMN purpose"
                    " TEXT NOT NULL DEFAULT 'retry'"
                )
            if "target_id" not in timer_columns:
                connection.execute(
                    "ALTER TABLE langgraph_timers ADD COLUMN target_id"
                    " TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                "UPDATE langgraph_timers SET target_id=node_id"
                " WHERE purpose='retry' AND target_id=''"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.run_db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _stamp(self) -> str:
        return self.clock().astimezone(timezone.utc).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")

    def _refuse_second_goal(self, connection, actor: str) -> None:
        """Refuse a start while this actor already has a run in flight.

        Called inside the transaction that inserts the run, never before it:
        two starts arriving together would otherwise both find nothing and
        both insert. SQLite's write lock is what serialises them, so this
        needs no lock file of its own — the previous engine kept one because
        its check and its insert were in separate transactions.

        Scoped to the owner. Runs are read by owner everywhere else, so a
        global slot would let one actor block another with a run the second
        cannot even open to see why.
        """

        if not self.single_goal:
            return
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        row = connection.execute(
            "SELECT run_id,goal,workflow_id,status FROM langgraph_runs"
            f" WHERE owner_actor=? AND status IN ({placeholders})"
            " ORDER BY created_at DESC,run_id LIMIT 1",
            (actor, *ACTIVE_STATUSES),
        ).fetchone()
        if row is not None:
            raise ActiveGoalExists(dict(row))

    def _workflow(self, workflow_id: str, version: int | None, *, starting=False):
        if starting and getattr(self.workflow_versions, "is_archived", None) is not None:
            if self.workflow_versions.is_archived(workflow_id):
                # The catalog card that offered this start was rendered before
                # somebody deleted the workflow. Retrying cannot help, so it is
                # not a conflict to reload past — the id is gone.
                raise LookupError(f"workflow was deleted: {workflow_id}")
        resolved = self.workflow_versions.latest_version(workflow_id) if version is None else version
        if resolved < 1:
            raise LookupError(f"workflow not found: {workflow_id}")
        record = self.workflow_versions.get(workflow_id, resolved)
        if record is None:
            raise LookupError(f"workflow version not found: {workflow_id}@{resolved}")
        return record

    def start(
        self,
        workflow_id: str,
        inputs: Mapping[str, Any],
        *,
        idempotency_key: str,
        workflow_version: int | None = None,
        actor: str = "system:langgraph",
        goal: str = "",
        wait: bool = True,
    ) -> LangGraphRun:
        """Start a run, and by default see it through.

        `wait=False` returns as soon as the run exists and executes it on a
        thread of this service's own. Two callers want different things and
        the difference is not incidental: an agent asking over MCP wants the
        result, and a person clicking start wants the page — the run they
        started is worth watching, and waiting for the whole thing before
        being told its id is what made watching impossible.

        Everything that decides whether the run may exist at all — the
        receipt, the single-goal slot, the archived workflow — has already
        happened either way by the time this returns. What `wait=False` gives
        up is being told how the run *ended*, which is on the run.
        """

        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        if not actor.strip():
            raise ValueError("actor is required")
        goal = _goal(goal)
        record = self._workflow(workflow_id, workflow_version, starting=True)
        request_hash = definition_hash({
            "workflow_id": workflow_id,
            "workflow_version": record.version.value,
            "inputs": inputs,
            "actor": actor,
            # In the hash, so the same key with a different goal is a conflict
            # rather than the first run handed back under the wrong label.
            "goal": goal,
        }).value
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt = connection.execute(
                "SELECT request_hash,run_id FROM langgraph_run_receipts"
                " WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if receipt is not None:
                connection.commit()
                if receipt["request_hash"] != request_hash:
                    raise LangGraphRunConflict(
                        "idempotency key was already used for another request"
                    )
                return self.get(receipt["run_id"])
            # After the receipt, so replaying a start is never refused for
            # conflicting with the run it started.
            self._refuse_second_goal(connection, actor)
            run_id = "langgraph_run:" + uuid.uuid4().hex
            now = self._stamp()
            connection.execute(
                "INSERT INTO langgraph_runs("
                "run_id,workflow_id,workflow_version,status,revision,input_json,"
                "result_json,error,created_at,updated_at,owner_actor,goal)"
                " VALUES (?,?,?,'running',0,?,NULL,NULL,?,?,?,?)",
                (
                    run_id, workflow_id, record.version.value,
                    canonical_json(inputs), now, now, actor, goal,
                ),
            )
            connection.execute(
                "INSERT INTO langgraph_run_receipts VALUES (?,?,?)",
                (idempotency_key, request_hash, run_id),
            )
            self._append_event(connection, run_id)
            connection.commit()
        if wait:
            return self._execute(run_id, record.ir, inputs=inputs)
        self._in_background(run_id, record.ir, inputs=inputs)
        return self.get(run_id)

    def start_snapshot(
        self,
        workflow_id: str,
        ir,
        inputs: Mapping[str, Any],
        *,
        template_id: str,
        idempotency_key: str,
        actor: str = "system:langgraph",
        goal: str = "",
    ) -> LangGraphRun:
        """Start an immutable per-Run graph without publishing a Workflow version."""

        goal = _goal(goal)
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        if not actor.strip():
            raise ValueError("actor is required")
        snapshot = to_primitive(ir)
        # Fail before recording a Run if the template cannot bind to this Runtime.
        compile_workflow(ir, self.handlers)
        request_hash = definition_hash({
            "template_id": template_id, "graph": snapshot,
            "inputs": inputs, "actor": actor, "goal": goal,
        }).value
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt = connection.execute(
                "SELECT request_hash,run_id FROM langgraph_run_receipts"
                " WHERE idempotency_key=?", (idempotency_key,),
            ).fetchone()
            if receipt is not None:
                connection.commit()
                if receipt["request_hash"] != request_hash:
                    raise LangGraphRunConflict(
                        "idempotency key was already used for another request"
                    )
                return self.get(receipt["run_id"])
            # A template run is a goal like any other, and holds the slot.
            self._refuse_second_goal(connection, actor)
            run_id = "langgraph_run:" + uuid.uuid4().hex
            now = self._stamp()
            connection.execute(
                "INSERT INTO langgraph_runs("
                "run_id,workflow_id,workflow_version,status,revision,input_json,"
                "result_json,error,created_at,updated_at,owner_actor,template_id,"
                "graph_snapshot_json,goal)"
                " VALUES (?,?,0,'running',0,?,NULL,NULL,?,?,?,?,?,?)",
                (
                    run_id, workflow_id, canonical_json(inputs), now, now, actor,
                    template_id, canonical_json(snapshot), goal,
                ),
            )
            connection.execute(
                "INSERT INTO langgraph_run_receipts VALUES (?,?,?)",
                (idempotency_key, request_hash, run_id),
            )
            self._append_event(connection, run_id)
            connection.commit()
        return self._execute(run_id, ir, inputs=inputs)

    def _run_ir(self, run: LangGraphRun):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT graph_snapshot_json FROM langgraph_runs WHERE run_id=?",
                (run.run_id,),
            ).fetchone()
        if row is not None and row["graph_snapshot_json"]:
            return workflow_ir_from_primitive(json.loads(row["graph_snapshot_json"]))
        return self._workflow(run.workflow_id, run.workflow_version).ir

    def compatibility(
        self, workflow_id: str, workflow_version: int | None = None
    ) -> Mapping[str, Any]:
        """Explain whether an immutable workflow version can use this engine."""

        try:
            # Resolved before the cache is consulted: `workflow_version=None`
            # means "the latest", and answering that from a key taken before
            # a publish would report the previous definition's verdict.
            record = self._workflow(workflow_id, workflow_version)
        except LookupError as exc:
            return {"compatible": False, "reason": "workflow_not_found", "detail": str(exc)}
        key = record.definition_hash.value
        with self._compatibility_lock:
            cached = self._compatibility.get(key)
        if cached is not None:
            return cached
        try:
            compile_workflow(record.ir, self.handlers)
        except (LangGraphCompileError, ValueError, TypeError) as exc:
            answer: Mapping[str, Any] = {
                "compatible": False,
                "reason": "unsupported_workflow",
                "detail": str(exc),
            }
        else:
            answer = {
                "compatible": True,
                "workflow_version": record.version.value,
                "engine": "langgraph",
            }
        with self._compatibility_lock:
            # Bounded, because a workflow republished many times leaves an
            # entry per version. Cleared wholesale rather than evicted one at
            # a time: every entry is equally cheap to recompute, and a policy
            # that has to choose is more machinery than the saving is worth.
            if len(self._compatibility) >= 512:
                self._compatibility.clear()
            self._compatibility[key] = answer
        return answer

    def resume(
        self,
        run_id: str,
        value: Any,
        *,
        expected_revision: int,
        idempotency_key: str,
        interrupt_id: str | None = None,
        actor: str | None = None,
    ) -> LangGraphRun:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        # Before the receipt is consulted, so that replaying somebody else's
        # idempotency key cannot answer with their run either.
        self.get(run_id, actor=actor)
        request_hash = definition_hash({
            "command": "resume",
            "run_id": run_id,
            "expected_revision": expected_revision,
            "value": value,
            "interrupt_id": interrupt_id,
        }).value
        with self._connect() as connection:
            receipt = connection.execute(
                "SELECT request_hash,run_id FROM langgraph_run_receipts"
                " WHERE idempotency_key=?", (idempotency_key,),
            ).fetchone()
        if receipt is not None:
            if receipt["request_hash"] != request_hash:
                raise LangGraphRunConflict(
                    "idempotency key was already used for another request"
                )
            return self.get(receipt["run_id"])
        current = self.get(run_id)
        available = {item["id"] for item in current.interrupts}
        if interrupt_id is None:
            if len(available) != 1:
                raise ValueError(
                    "interrupt_id is required when a run has multiple interrupts"
                )
            interrupt_id = next(iter(available))
        elif interrupt_id not in available:
            raise ValueError(f"interrupt is not pending: {interrupt_id}")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt = connection.execute(
                "SELECT request_hash,run_id FROM langgraph_run_receipts"
                " WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if receipt is not None:
                connection.commit()
                if receipt["request_hash"] != request_hash:
                    raise LangGraphRunConflict(
                        "idempotency key was already used for another request"
                    )
                return self.get(receipt["run_id"])
            row = connection.execute(
                "SELECT interrupt_responses_json FROM langgraph_runs"
                " WHERE run_id=? AND status='interrupted' AND revision=?",
                (run_id, expected_revision),
            ).fetchone()
            responses = {} if row is None else json.loads(
                row["interrupt_responses_json"]
            )
            responses[interrupt_id] = value
            ready = available <= set(responses)
            pending_interrupts = tuple(
                item for item in current.interrupts
                if item["id"] not in responses
            )
            claimed = connection.execute(
                "UPDATE langgraph_runs SET status=?,revision=revision+?,"
                "interrupt_responses_json=?,interrupts_json=?,updated_at=?"
                " WHERE run_id=? AND status='interrupted' AND revision=?",
                (
                    "running" if ready else "interrupted",
                    0 if ready else 1,
                    # Kept, not cleared. This commits and *then* hands the
                    # responses to the graph; a process that stops in between
                    # used to leave `running` with nothing recorded, so
                    # recovery re-entered with `invoke(None)`, the graph
                    # interrupted at the same nodes, and every human answer
                    # collected across the earlier partial resumes was gone
                    # with no trace. They are cleared in `_settle`, once the
                    # graph has actually consumed them.
                    canonical_json(responses),
                    "[]" if ready else canonical_json(pending_interrupts),
                    self._stamp(), run_id, expected_revision,
                ),
            ).rowcount
            if claimed != 1:
                connection.rollback()
                actual = self.get(run_id)
                raise LangGraphRunConflict(
                    f"cannot resume run at revision {expected_revision}; "
                    f"found {actual.status}@{actual.revision}"
                )
            connection.execute(
                "INSERT INTO langgraph_run_receipts VALUES (?,?,?)",
                (idempotency_key, request_hash, run_id),
            )
            self._append_event(connection, run_id)
            connection.commit()
        if not ready:
            return self.get(run_id)
        return self._execute(
            run_id, self._run_ir(current), resume=responses,
        )

    def recover(self, run_id: str) -> LangGraphRun:
        """Continue a run whose process ended after a durable checkpoint.

        A run this process is executing right now is not that run. `start`
        drives the graph synchronously inside the request thread while the row
        already reads `running`, so an operator's `POST .../recover` — or the
        `recover_langgraph_run` tool — landing in that window used to compile
        and invoke the same thread_id a second time. The attempt journal then
        finds the sibling's `started` row and reports the outcome unknown,
        settling a perfectly healthy run as `unknown`.

        Only this process's own work can be ruled out here. A run left
        `running` by a *different* process is exactly what recovery is for,
        and is indistinguishable from one that machine is still driving.
        """

        with self._in_flight_lock:
            if run_id in self._in_flight:
                return self.get(run_id)
        current = self.get(run_id)
        if current.status == "running":
            with self._connect() as connection:
                firing = connection.execute(
                    "SELECT * FROM langgraph_timers WHERE run_id=?"
                    " AND status='firing' AND purpose='join_deadline'"
                    " ORDER BY due_at,timer_id LIMIT 1", (run_id,),
                ).fetchone()
            if firing is not None:
                return self._fire_join_deadline(
                    run_id, self._run_ir(current), firing["target_id"],
                )
        if current.status in {"waiting", "interrupted"}:
            with self._connect() as connection:
                timer = connection.execute(
                    "SELECT * FROM langgraph_timers WHERE run_id=?"
                    " AND status='scheduled' ORDER BY due_at,timer_id LIMIT 1",
                    (run_id,),
                ).fetchone()
                if timer is None or timer["due_at"] > self._stamp():
                    return current
                if timer["purpose"] == "retry" and current.status != "waiting":
                    return current
                connection.execute("BEGIN IMMEDIATE")
                claimed_run = connection.execute(
                    "UPDATE langgraph_runs SET status='running',revision=revision+1,"
                    "updated_at=? WHERE run_id=? AND revision=?"
                    " AND status IN ('waiting','interrupted')",
                    (self._stamp(), run_id, current.revision),
                ).rowcount
                changed_timer = connection.execute(
                    "UPDATE langgraph_timers SET status=? WHERE timer_id=?"
                    " AND status='scheduled'",
                    (
                        "firing" if timer["purpose"] == "join_deadline"
                        else "fired",
                        timer["timer_id"],
                    ),
                ).rowcount
                if claimed_run != 1 or changed_timer != 1:
                    connection.rollback()
                    return self.get(run_id)
                self._append_event(connection, run_id)
                connection.commit()
            current = self.get(run_id)
            if timer["purpose"] == "join_deadline":
                return self._fire_join_deadline(
                    run_id, self._run_ir(current), timer["target_id"],
                )
        if current.status != "running":
            return current
        # Answers collected before the process stopped. Re-entering with
        # `invoke(None)` would interrupt at the same nodes and lose them, so
        # they are handed back to the graph exactly as `resume` would have.
        with self._connect() as connection:
            pending = connection.execute(
                "SELECT interrupt_responses_json FROM langgraph_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        responses = json.loads(pending["interrupt_responses_json"] or "{}")
        if responses:
            return self._execute(
                run_id, self._run_ir(current), resume=responses,
            )
        config = {"configurable": {"thread_id": run_id}}
        with SqliteSaver.from_conn_string(str(self.checkpoint_db_path)) as saver:
            has_checkpoint = saver.get_tuple(config) is not None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT input_json FROM langgraph_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        inputs = None if has_checkpoint else json.loads(row["input_json"])
        return self._execute(run_id, self._run_ir(current), inputs=inputs)

    def _schedule_join_deadlines(
        self, run_id: str, ir, *,
        available_outputs: Mapping[str, Any],
        pending_nodes: frozenset[str] = frozenset(),
        execution_order: Sequence[str] = (),
    ) -> None:
        """Arm the deadline of every join this run is now waiting at.

        A branch counts as under way when it has produced output *or* is a
        task the graph is currently holding. Requiring output alone meant a
        join fed only by human branches never got a timer at all: at the
        moment it interrupts, neither branch has produced anything, and
        scheduling only runs when an execute ends at an interrupt — which will
        not happen again without the external input the deadline exists to
        stop waiting for. The wait it was meant to bound was the one wait it
        could not bound.
        """

        policies = {policy.id: policy for policy in ir.policies}
        now = self.clock().astimezone(timezone.utc)
        with self._connect() as connection:
            for node in ir.nodes:
                policy = next((
                    policies[item] for item in node.policies
                    if item in policies and policies[item].kind == "join"
                    and policies[item].config.get("mode") == "deadline"
                ), None)
                if policy is None:
                    continue
                if not any(
                    edge.target_node == node.id
                    and (
                        edge.source_node in available_outputs
                        or edge.source_node in pending_nodes
                    )
                    for edge in ir.edges
                ):
                    continue
                due = now + timedelta(
                    seconds=int(policy.config["deadline_seconds"]),
                )
                due_at = due.isoformat(timespec="microseconds").replace(
                    "+00:00", "Z",
                )
                # One deadline per visit. The id and `attempt_number` were
                # fixed for the whole run, and `_finish_timer` marks a fired
                # timer rather than deleting it, so `INSERT OR IGNORE` threw
                # away every later generation's timer: a deadline join inside
                # a loop was bounded on its first visit and unbounded on
                # every one after.
                generation = list(execution_order).count(node.id) + 1
                timer_id = (
                    f"langgraph_timer:{run_id}:join_deadline:{node.id}"
                    f":{generation}"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO langgraph_timers("
                    "timer_id,run_id,node_id,attempt_number,due_at,status,"
                    "purpose,target_id) VALUES (?,?,?,?,?,'scheduled',?,?)",
                    (
                        timer_id, run_id, node.id, generation, due_at,
                        "join_deadline", node.id,
                    ),
                )
            connection.commit()

    def _fire_join_deadline(self, run_id: str, ir, node_id: str) -> LangGraphRun:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT owner_actor FROM langgraph_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        config = {"configurable": {
            "thread_id": run_id, "actor": row["owner_actor"],
        }}
        try:
            with self._executing(run_id), SqliteSaver.from_conn_string(
                str(self.checkpoint_db_path)
            ) as saver:
                workflow = compile_workflow(ir, self.handlers, checkpointer=saver)
                result = workflow.fire_join_deadline(node_id, config=config)
                after = workflow.graph.get_state(config)
            interrupts = tuple(
                {"id": item.id, "value": to_primitive(item.value)}
                for task in after.tasks for item in task.interrupts
            )
            self._finish_timer(run_id, "join_deadline", node_id)
            return self._settle(
                run_id, "interrupted" if interrupts else "completed",
                result=result["result"], interrupts=interrupts,
            )
        except LangGraphJoinDeadlineExceeded as exc:
            self._finish_timer(run_id, "join_deadline", node_id)
            return self._settle(
                run_id, "failed", error=f"{type(exc).__name__}: {exc}",
            )
        except LangGraphUnknownExternalResult as exc:
            # Same rule as `_execute`: an effect whose outcome nobody knows is
            # not a failure to retry, and firing this deadline again would ask
            # the question a second time.
            self._finish_timer(run_id, "join_deadline", node_id)
            return self._settle(
                run_id, "unknown", error=f"{type(exc).__name__}: {exc}",
            )
        except Exception as exc:
            # Anything the fire raised on the way through — a Handler that
            # failed, an output the compiler refused, a completion policy left
            # unsatisfied. Only the deadline's own exception used to be
            # caught, so everything else escaped with the run still `running`
            # and the timer still `firing`. `recover_due` selects exactly that
            # pair and routes it straight back here, and the timer loop
            # isolates the exception — so the run never reached a terminal
            # status and every poll tried again, forever.
            self._finish_timer(run_id, "join_deadline", node_id)
            self._settle(run_id, "failed", error=f"{type(exc).__name__}: {exc}")
            raise

    def cancel(
        self,
        run_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        actor: str | None = None,
    ) -> LangGraphRun:
        """Persist cancellation before signalling any in-flight Handler."""

        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        self.get(run_id, actor=actor)
        request_hash = definition_hash({
            "command": "cancel",
            "run_id": run_id,
            "expected_revision": expected_revision,
        }).value
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt = connection.execute(
                "SELECT request_hash,run_id FROM langgraph_run_receipts"
                " WHERE idempotency_key=?", (idempotency_key,),
            ).fetchone()
            if receipt is not None:
                connection.commit()
                if receipt["request_hash"] != request_hash:
                    raise LangGraphRunConflict(
                        "idempotency key was already used for another request"
                    )
                return self.get(receipt["run_id"])
            changed = connection.execute(
                "UPDATE langgraph_runs SET status='cancelled',revision=revision+1,"
                "interrupts_json='[]',updated_at=? WHERE run_id=? AND revision=?"
                " AND status IN ('running','waiting','interrupted')",
                (self._stamp(), run_id, expected_revision),
            ).rowcount
            if changed != 1:
                connection.rollback()
                actual = self.get(run_id)
                raise LangGraphRunConflict(
                    f"cannot cancel run at revision {expected_revision}; "
                    f"found {actual.status}@{actual.revision}"
                )
            connection.execute(
                "UPDATE langgraph_timers SET status='cancelled'"
                " WHERE run_id=? AND status IN ('scheduled','firing')", (run_id,),
            )
            connection.execute(
                "INSERT INTO langgraph_run_receipts VALUES (?,?,?)",
                (idempotency_key, request_hash, run_id),
            )
            self._append_event(connection, run_id)
            connection.commit()
        self.handlers.cancel(run_id)
        return self.get(run_id)

    def get(self, run_id: str, *, actor: str | None = None) -> LangGraphRun:
        """One run, optionally only if `actor` owns it.

        Scoped exactly as the Artifact store already scopes its reads: a run
        belonging to somebody else is not found, rather than forbidden, so the
        answer does not disclose that the id exists. Callers inside the
        service — recovery, timers, settlement — pass no actor and see every
        run, which is what they are for.
        """

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM langgraph_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None or (actor is not None and row["owner_actor"] != actor):
            raise LookupError(f"LangGraph run not found: {run_id}")
        return self._record(row)

    def _in_background(self, run_id: str, ir, *, inputs) -> None:
        """Execute a run on a thread, and remember it is out there.

        Failures are not raised anywhere a caller could catch them — there is
        no caller left. `_execute` settles the run before it re-raises, so the
        outcome is durable on the run itself, which is where a caller that did
        not wait has to look for it anyway.
        """

        def run() -> None:
            try:
                self._execute(run_id, ir, inputs=inputs)
            except Exception:  # noqa: BLE001 - settled on the run already
                return

        with self._background_lock:
            if self._background is None:
                self._background = ThreadPoolExecutor(
                    max_workers=8, thread_name_prefix="langgraph-run",
                )
            self._background_runs.add(self._background.submit(run))

    def wait_for_background(self, timeout: float = 10.0) -> tuple[str, ...]:
        """Let runs started without a waiter finish; name the ones that did not.

        A shutdown that walks away from them is not a correctness problem —
        they are left `running` and startup recovery re-enters them, the same
        path a killed process takes. It is a politeness problem, and one
        worth a bounded wait: re-entering a run costs a superstep of work
        that letting it finish does not.
        """

        with self._background_lock:
            pending = tuple(self._background_runs)
        done, not_done = wait(pending, timeout=timeout)
        with self._background_lock:
            self._background_runs.difference_update(done)
        return tuple(f"background-run-{index}" for index in range(len(not_done)))

    def _executed_nodes(self, run_id: str) -> tuple[str, ...]:
        """Every node execution this run has recorded, oldest first.

        Read from the newest checkpoint *plus its pending writes*, and the
        second half is not an optimisation. A branch that finished inside a
        superstep another branch interrupted is only in the pending writes:
        the committed checkpoint still says it never ran. Reading the
        checkpoint alone shows a completed parallel branch as not reached for
        as long as its sibling waits for a person.

        No graph is compiled to do it. `get_state` would apply the same writes
        and give the same answer, but compiling wires the Handlers back up,
        and nothing that only reads a run should be able to reach one.

        A node appears once per execution: a loop that ran a node three times
        is three entries, which is what makes "third attempt" sayable.
        """

        config = {"configurable": {"thread_id": run_id}}
        with SqliteSaver.from_conn_string(str(self.checkpoint_db_path)) as saver:
            newest = next(iter(saver.list(config, limit=1)), None)
            if newest is None:
                return ()
            order = list(
                (newest.checkpoint.get("channel_values") or {}).get(
                    "execution_order"
                ) or ()
            )
            # Appended, matching the channel's own reducer.
            for _task, channel, value in (newest.pending_writes or ()):
                if channel != "execution_order":
                    continue
                order.extend(
                    value if isinstance(value, (list, tuple)) else [value]
                )
        return tuple(str(node_id) for node_id in order)

    def _attempt_facts(self, run_id: str) -> dict[str, dict[str, Any]]:
        """Per-node outcome and timing, for the nodes that keep an attempt.

        Only Handlers with a journal are here — an Agent or a Tool. A pure
        node leaves no attempt, so it has no timing and its failure has
        nowhere to be recorded; `steps` says so rather than guessing.
        """

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT node_id, MIN(updated_at) AS first_at,"
                " MAX(updated_at) AS last_at,"
                " SUM(status='failed') AS failed,"
                " SUM(status='unknown') AS unknown"
                " FROM langgraph_handler_attempts WHERE run_id=?"
                " GROUP BY node_id",
                (run_id,),
            ).fetchall()
        return {
            str(row["node_id"]): {
                "first_at": row["first_at"], "last_at": row["last_at"],
                "failed": bool(row["failed"]), "unknown": bool(row["unknown"]),
            }
            for row in rows
        }

    def steps(
        self, run_id: str, *, actor: str | None = None,
    ) -> tuple[Mapping[str, Any], ...]:
        """What this run did, step by step — derived, not recorded.

        There is no step table. This reads the definition for what the run
        *could* do and the checkpoint for what it *did*, and the difference
        between them is the progress. Four statuses come out of that, and each
        means exactly one thing:

        `succeeded`  the node is in the execution order and its attempts, if
                     it keeps any, all settled.
        `failed`     its attempt journal says so. Only a Handler with a
                     journal can say it, so a pure node that raised is not
                     here — the run carries the error, the step does not.
        `waiting`    a person has been asked, and the run is interrupted at
                     this node.
        `not_reached` it is in the definition and has not run. Not "skipped":
                     telling a branch nobody took from one still to come needs
                     the routes walked, and a projection that guessed would be
                     wrong on exactly the runs worth looking at.

        Ordered by the same layout the catalog and the canvas use, so a node
        keeps its place between the picture of the definition and the picture
        of the run.
        """

        run = self.get(run_id, actor=actor)
        ir = self._run_ir(run)
        executed = self._executed_nodes(run_id)
        attempts = self._attempt_facts(run_id)
        counts: dict[str, int] = {}
        for node_id in executed:
            counts[node_id] = counts.get(node_id, 0) + 1
        waiting = {
            str((item.get("value") or {}).get("node_id"))
            for item in run.interrupts
            if isinstance(item.get("value"), Mapping)
        }
        layout = graph_layout(
            [node.id for node in ir.nodes],
            [
                {"from": edge.source_node, "to": edge.target_node,
                 "back_edge": edge.back_edge}
                for edge in ir.edges
            ],
        )
        placed = {
            item["node_id"]: (item["depth"], item["lane"])
            for item in layout["positions"]
        }
        steps: list[Mapping[str, Any]] = []
        for node in sorted(ir.nodes, key=lambda n: placed.get(n.id, (0, 0))):
            fact = attempts.get(node.id, {})
            if node.id in waiting:
                status = "waiting"
            elif fact.get("failed") or fact.get("unknown"):
                status = "failed"
            elif counts.get(node.id):
                status = "succeeded"
            else:
                status = "not_reached"
            steps.append({
                "node_id": node.id,
                "label": node.label or node.id,
                "kind": node.kind,
                "handler": None if node.handler is None else {
                    "name": node.handler.name, "version": node.handler.version,
                },
                "status": status,
                "runs": counts.get(node.id, 0),
                "first_at": fact.get("first_at"),
                "last_at": fact.get("last_at"),
            })
        return tuple(steps)

    def replay(
        self, run_id: str, *, actor: str | None = None, limit: int = 100,
    ) -> tuple[Mapping[str, Any], ...]:
        """The run's state re-derived from what was recorded, step by step.

        Replay has a definition in this codebase and it is not "run it again":
        `workflow/README.md` says a replay may read old state and recorded
        events and nothing else — no clock, no random, no Planner, Handler,
        Tool, HTTP or Artifact writer, and no persistent write of any kind.

        So this reads the checkpointer directly and never compiles the graph.
        Not as an optimisation: with no graph there is no wiring, and with no
        wiring there is no way for a replay to reach a Handler even by
        mistake. What it returns is what was durably recorded at each
        superstep — the same facts a resume would have continued from.

        Ordered oldest first, because a derivation runs forwards. `limit`
        bounds the newest end, which is where a long run's interesting part
        is; a run shorter than the limit is returned whole.
        """

        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        # Scoped and existence-checked exactly as `get` is, and before any
        # checkpoint is read: whose run this is decides whether its recorded
        # state may be looked at.
        self.get(run_id, actor=actor)
        config = {"configurable": {"thread_id": run_id}}
        steps: list[Mapping[str, Any]] = []
        with SqliteSaver.from_conn_string(str(self.checkpoint_db_path)) as saver:
            for entry in saver.list(config, limit=limit):
                values = entry.checkpoint.get("channel_values") or {}
                metadata = entry.metadata or {}
                steps.append({
                    "checkpoint_id": entry.checkpoint.get("id"),
                    "parent_checkpoint_id": (
                        (entry.parent_config or {}).get("configurable", {})
                        .get("checkpoint_id")
                    ),
                    "recorded_at": entry.checkpoint.get("ts"),
                    "step": metadata.get("step"),
                    "source": metadata.get("source"),
                    "execution_order": list(values.get("execution_order") or ()),
                    "node_outputs": to_primitive(values.get("node_outputs") or {}),
                    "node_routes": dict(values.get("node_routes") or {}),
                    "join_deadlines": dict(values.get("join_deadlines") or {}),
                    # Writes the superstep produced but had not committed when
                    # the checkpoint was taken. A branch that finished inside
                    # an interrupted superstep lives here and nowhere else,
                    # which is the state a deadline join fires from.
                    "pending_writes": sorted({
                        channel for _task, channel, _value in
                        (entry.pending_writes or ())
                    }),
                })
        return tuple(reversed(steps))

    def _artifacts_are_here(self) -> bool:
        database = getattr(self.artifacts, "database", None)
        return database is not None and Path(database) == self.run_db_path

    def list_runs(
        self, *, status: str | None = None, limit: int = 100,
        actor: str | None = None, after: tuple[str, str] | None = None,
        query: str = "",
    ) -> tuple[LangGraphRun, ...]:
        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if status is not None and status not in {
            "running", "waiting", "interrupted", "completed", "failed", "unknown",
            "cancelled",
        }:
            raise ValueError("invalid LangGraph run status")
        clauses, params = [], []
        if status is not None:
            clauses.append("status=?"); params.append(status)
        if actor is not None:
            clauses.append("owner_actor=?"); params.append(actor)
        if query.strip():
            # What a person can see on the row: the sentence they typed, the
            # Workflow it ran, and the id they may have copied from elsewhere.
            clauses.append(
                "(goal LIKE ? ESCAPE '\\' OR workflow_id LIKE ? ESCAPE '\\'"
                " OR run_id LIKE ? ESCAPE '\\')"
            )
            pattern = "%" + query.strip().replace(
                "\\", "\\\\"
            ).replace("%", "\\%").replace("_", "\\_") + "%"
            params.extend([pattern, pattern, pattern])
        if after is not None:
            # Keyset, not offset: the list is ordered newest first and a run
            # started while somebody is paging must not shift the page under
            # them into showing one row twice and skipping another.
            clauses.append("(created_at < ? OR (created_at = ? AND run_id > ?))")
            params.extend([after[0], after[0], after[1]])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            # Counted here so a list of fifty is one query rather than fifty,
            # and only when the Artifact store is this same database: it is
            # allowed to be another one, and a subquery would then be reading
            # a table that is not there or not the right one.
            count = (
                "(SELECT COUNT(*) FROM langgraph_artifacts a"
                " WHERE a.run_id = r.run_id AND a.status = 'committed')"
                if self._artifacts_are_here() else "0"
            )
            rows = connection.execute(
                f"SELECT r.*, {count} AS artifact_count FROM langgraph_runs r"
                + where
                + " ORDER BY r.created_at DESC,r.run_id LIMIT ?",
                (*params, limit),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def counts(self) -> dict[str, Any]:
        """Runs and timers by status — what an operator asks the engine for.

        Reported from this adapter's own database rather than the project one:
        the run tables the Ops page used to read belonged to the engine that
        was deleted, and after it went they only ever answered zero.
        """

        with self._connect() as connection:
            runs = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM langgraph_runs"
                    " GROUP BY status"
                )
            }
            timers = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM langgraph_timers"
                    " GROUP BY status"
                )
            }
            handlers = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM langgraph_handler_attempts"
                    " GROUP BY status"
                )
            }
        return {
            "runs_by_status": runs,
            "timers_by_status": timers,
            "handler_attempts_by_status": handlers,
        }

    def handler_attempts(self) -> dict[str, dict[str, Any]]:
        """Per-Handler attempt totals and the latest one, for the Ops page."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT handler_name,
                       COUNT(*) AS total,
                       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
                FROM langgraph_handler_attempts
                WHERE handler_name <> ''
                GROUP BY handler_name
                """
            ).fetchall()
            latest = connection.execute(
                """
                SELECT handler_name, run_id, node_id, attempt_id, status, updated_at
                FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY handler_name
                        ORDER BY updated_at DESC, attempt_id DESC
                    ) AS rank
                    FROM langgraph_handler_attempts WHERE handler_name <> ''
                ) WHERE rank = 1
                """
            ).fetchall()
        recent = {
            str(row["handler_name"]): {
                "run_id": row["run_id"], "node_id": row["node_id"],
                "attempt_id": row["attempt_id"], "status": row["status"],
                "occurred_at": row["updated_at"],
            }
            for row in latest
        }
        return {
            str(row["handler_name"]): {
                "total": int(row["total"]), "failed": int(row["failed"] or 0),
                "recent": recent.get(str(row["handler_name"])),
            }
            for row in rows
        }

    def _append_event(self, connection, run_id: str) -> None:
        """Record that this run changed, in the transaction that changed it.

        What it records is read back rather than computed. The callers know
        what they asked for, but only the row knows what was stored — and one
        of them (a resume that completes the last interrupt) advances the
        status without advancing the revision.
        """

        row = connection.execute(
            "SELECT status,revision,updated_at FROM langgraph_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            return
        append_event(
            connection, run_id, f"langgraph_run.{row['status']}",
            revision=int(row["revision"]), occurred_at=row["updated_at"],
        )

    def events_head(self) -> int:
        """The newest position, which is where a subscriber with no cursor starts."""

        with self._connect() as connection:
            return int(connection.execute(
                "SELECT COALESCE(MAX(position), 0) FROM langgraph_run_events"
            ).fetchone()[0])

    def events_after(
        self, position: int, *, limit: int = 200,
    ) -> tuple[Mapping[str, Any], ...]:
        """Events after `position`, oldest first, bounded.

        `position` is the adapter's own autoincrement, so it is dense enough
        to resume from and never reused: a subscriber that reconnects with the
        last cursor it saw gets exactly what it missed.
        """

        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT position,run_id,event_type,revision,occurred_at,"
                "node_id,attempt_id"
                " FROM langgraph_run_events WHERE position > ?"
                " ORDER BY position LIMIT ?",
                (int(position), limit),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def last_change(self) -> str | None:
        """The most recent run update, as the stored timestamp string."""

        with self._connect() as connection:
            return connection.execute(
                "SELECT MAX(updated_at) FROM langgraph_runs"
            ).fetchone()[0]

    def workflow_usage(self) -> dict[str, dict[str, Any]]:
        """Per-workflow run count and most recent start, for the catalog."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT workflow_id, MAX(created_at) AS last_run_at,"
                " COUNT(*) AS run_count FROM langgraph_runs GROUP BY workflow_id"
            ).fetchall()
        return {
            str(row["workflow_id"]): {
                "last_run_at": row["last_run_at"],
                "run_count": int(row["run_count"]),
            }
            for row in rows
        }

    def recover_running(
        self, *, limit: int = 100, on_error: Callable[[str, Exception], None] | None = None
    ) -> tuple[LangGraphRun, ...]:
        """Recover a bounded snapshot of runs left running by process loss.

        One unrecoverable run must not strand the rest. `_execute` settles a
        run it cannot finish as `failed` *before* it re-raises, so that run's
        terminal state is already durable and skipping it loses nothing;
        carrying the exception out of the batch, on the other hand, leaves
        every run behind it `running` forever. At startup it is worse than
        that — this is called from the ASGI lifespan, so one poisoned run would
        stop the whole Runtime from accepting a single connection, including
        the half of it that has nothing to do with LangGraph.

        `on_error` observes what was skipped. A failure to observe is not
        allowed to undo the isolation this method exists to provide.
        """

        def attempt(run_id: str) -> LangGraphRun | None:
            try:
                return self.recover(run_id)
            except Exception as exc:  # noqa: BLE001 - isolation is the point
                if on_error is not None:
                    try:
                        on_error(run_id, exc)
                    except Exception:  # noqa: BLE001 - observation, never the work
                        pass
                return None

        recovered = [
            run for item in self.list_runs(status="running", limit=limit)
            if (run := attempt(item.run_id)) is not None
        ]
        recovered.extend(
            self.recover_due(
                limit=max(1, limit - len(recovered)), on_error=on_error
            )
        )
        return tuple(recovered)

    def recover_due(
        self, *, limit: int = 100, on_error: Callable[[str, Exception], None] | None = None
    ) -> tuple[LangGraphRun, ...]:
        """Fire every timer that is due, one failure at a time.

        Isolated for the same reason as `recover_running`: this is the timer
        loop's entry point, and a single run whose deadline cannot be fired
        would otherwise stop every other run's timer from firing too.
        """

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT run_id FROM langgraph_timers"
                " WHERE status IN ('scheduled','firing') AND due_at<=?"
                " ORDER BY due_at LIMIT ?",
                (self._stamp(), limit),
            ).fetchall()
        recovered: list[LangGraphRun] = []
        for row in rows:
            try:
                recovered.append(self.recover(row["run_id"]))
            except Exception as exc:  # noqa: BLE001 - isolation is the point
                if on_error is not None:
                    try:
                        on_error(row["run_id"], exc)
                    except Exception:  # noqa: BLE001 - observation, never the work
                        pass
        return tuple(recovered)

    @contextmanager
    def _executing(self, run_id: str):
        """Hold `run_id` for as long as this process is driving its graph."""

        with self._in_flight_lock:
            self._in_flight.add(run_id)
        try:
            yield
        finally:
            with self._in_flight_lock:
                self._in_flight.discard(run_id)

    def _finish_timer(self, run_id: str, purpose: str, target_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE langgraph_timers SET status='fired'"
                " WHERE run_id=? AND purpose=? AND target_id=?"
                " AND status='firing'", (run_id, purpose, target_id),
            )
            connection.commit()

    def _execute(self, run_id: str, ir, *, inputs=..., resume=...) -> LangGraphRun:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT owner_actor FROM langgraph_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise LookupError(f"LangGraph run not found: {run_id}")
        config = {"configurable": {
            "thread_id": run_id, "actor": row["owner_actor"],
        }}
        try:
            with self._executing(run_id), SqliteSaver.from_conn_string(
                str(self.checkpoint_db_path)
            ) as saver:
                workflow = compile_workflow(ir, self.handlers, checkpointer=saver)
                if resume is not ...:
                    result = workflow.resume(resume, config=config)
                else:
                    result = workflow.invoke(None if inputs is None else inputs, config=config)
                snapshot = workflow.graph.get_state(config)
            status = "interrupted" if snapshot.next else "completed"
            if snapshot.next:
                self._schedule_join_deadlines(
                    run_id,
                    ir,
                    available_outputs=snapshot.values.get("node_outputs", {}),
                    # The nodes the graph is holding right now. A human branch
                    # sits here and nowhere else until somebody answers it.
                    pending_nodes=frozenset(
                        task.name for task in snapshot.tasks
                    ),
                    execution_order=snapshot.values.get("execution_order", ()),
                )
            interrupts = tuple(
                {
                    "id": item.id,
                    "value": to_primitive(item.value),
                }
                for task in snapshot.tasks
                for item in task.interrupts
            )
            return self._settle(
                run_id, status, result=result["result"], interrupts=interrupts,
            )
        except LangGraphUnknownExternalResult as exc:
            return self._settle(
                run_id, "unknown", error=f"{type(exc).__name__}: {exc}"
            )
        except LangGraphRetryRequested as exc:
            return self._schedule_retry(run_id, exc)
        except Exception as exc:
            self._settle(run_id, "failed", error=f"{type(exc).__name__}: {exc}")
            raise

    def _schedule_retry(self, run_id: str, request) -> LangGraphRun:
        maximum = int(request.policy.get("max_attempts", 1))
        backoff = tuple(request.policy.get("backoff_seconds") or ())
        with self._connect() as connection:
            # Scoped to the generation that failed. The count used to run
            # over the whole run, so a retry-safe node inside a bounded loop
            # that failed once per generation exhausted `max_attempts` after
            # N generations and failed the run — each generation having had
            # exactly one attempt. The generation rides in `target_id`
            # because `recover` reads that column as a node id only for a
            # join deadline, and because the thing being retried in a loop is
            # a generation's execution rather than the node in the abstract.
            target = f"{request.node_id}#{request.generation}"
            attempt_number = int(connection.execute(
                "SELECT COUNT(*) FROM langgraph_timers"
                " WHERE run_id=? AND purpose='retry' AND target_id=?",
                (run_id, target),
            ).fetchone()[0]) + 1
            if attempt_number >= maximum:
                self._settle(
                    run_id, "failed",
                    error=f"{type(request.cause).__name__}: {request.cause}",
                )
                raise request.cause
            delay = int(backoff[attempt_number - 1]) if attempt_number <= len(backoff) else 0
            due = self.clock().astimezone(timezone.utc) + timedelta(seconds=delay)
            due_at = due.isoformat(timespec="microseconds").replace("+00:00", "Z")
            timer_id = (
                f"langgraph_timer:{run_id}:{request.node_id}"
                f":{request.generation}:{attempt_number}"
            )
            connection.execute(
                "INSERT OR IGNORE INTO langgraph_timers("
                "timer_id,run_id,node_id,attempt_number,due_at,status,purpose,target_id)"
                " VALUES (?,?,?,?,?,'scheduled','retry',?)",
                (
                    timer_id, run_id, request.node_id, attempt_number, due_at,
                    target,
                ),
            )
            connection.commit()
        return self._settle(
            run_id, "waiting",
            interrupts=({
                "type": "retry_timer", "timer_id": timer_id,
                "node_id": request.node_id, "due_at": due_at,
                "attempt_number": attempt_number,
            },),
        )

    def _settle(
        self, run_id: str, status: str, *, result: Any = None,
        interrupts: tuple[Mapping[str, Any], ...] = (), error: str | None = None,
    ) -> LangGraphRun:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                # Reaching here means the execute finished, however it
                # finished, so whatever it was resumed with has been consumed.
                "UPDATE langgraph_runs SET status=?,revision=revision+1,result_json=?,"
                " interrupts_json=?,error=?,interrupt_responses_json='{}',"
                " updated_at=? WHERE run_id=? AND status!='cancelled'",
                (
                    status,
                    None if result is None else canonical_json(result),
                    canonical_json(interrupts),
                    error,
                    self._stamp(),
                    run_id,
                ),
            ).rowcount
            if status in {"completed", "failed", "unknown", "cancelled"}:
                connection.execute(
                    "UPDATE langgraph_timers SET status='cancelled'"
                    " WHERE run_id=? AND status IN ('scheduled','firing')",
                    (run_id,),
                )
            if changed != 1:
                connection.rollback()
                current = self.get(run_id)
                if current.status == "cancelled":
                    return current
                raise LookupError(f"LangGraph run not found: {run_id}")
            self._append_event(connection, run_id)
            connection.commit()
        return self.get(run_id)

    @staticmethod
    def _record(row: sqlite3.Row) -> LangGraphRun:
        return LangGraphRun(
            row["run_id"],
            row["workflow_id"],
            int(row["workflow_version"]),
            row["status"],
            int(row["revision"]),
            None if row["result_json"] is None else json.loads(row["result_json"]),
            tuple(json.loads(row["interrupts_json"])),
            row["error"],
            row["created_at"],
            row["updated_at"],
            row["owner_actor"],
            row["template_id"] if "template_id" in row.keys() else None,
            (row["goal"] or "") if "goal" in row.keys() else "",
            int(row["artifact_count"]) if "artifact_count" in row.keys() else 0,
        )
