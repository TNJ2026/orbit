"""Durable application service for the isolated LangGraph execution adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping
import uuid

from langgraph.checkpoint.sqlite import SqliteSaver

from ..domain.serialization import canonical_json, definition_hash, to_primitive
from ..domain.ir_schema import workflow_ir_from_primitive
from .compiler import (
    LangGraphCompileError, LangGraphHandlerRegistry,
    LangGraphJoinDeadlineExceeded,
    LangGraphRetryRequested, LangGraphUnknownExternalResult, compile_workflow,
)


class LangGraphRunConflict(ValueError):
    pass


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
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.workflow_versions = workflow_versions
        self.handlers = handlers
        self.run_db_path = Path(run_db_path)
        self.checkpoint_db_path = Path(checkpoint_db_path)
        self.artifacts = artifact_store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
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
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS langgraph_run_receipts (
                    idempotency_key TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    run_id TEXT NOT NULL REFERENCES langgraph_runs(run_id)
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
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(langgraph_runs)")
            }
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

    def _workflow(self, workflow_id: str, version: int | None):
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
    ) -> LangGraphRun:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        if not actor.strip():
            raise ValueError("actor is required")
        record = self._workflow(workflow_id, workflow_version)
        request_hash = definition_hash({
            "workflow_id": workflow_id,
            "workflow_version": record.version.value,
            "inputs": inputs,
            "actor": actor,
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
            run_id = "langgraph_run:" + uuid.uuid4().hex
            now = self._stamp()
            connection.execute(
                "INSERT INTO langgraph_runs("
                "run_id,workflow_id,workflow_version,status,revision,input_json,"
                "result_json,error,created_at,updated_at,owner_actor)"
                " VALUES (?,?,?,'running',0,?,NULL,NULL,?,?,?)",
                (
                    run_id, workflow_id, record.version.value,
                    canonical_json(inputs), now, now, actor,
                ),
            )
            connection.execute(
                "INSERT INTO langgraph_run_receipts VALUES (?,?,?)",
                (idempotency_key, request_hash, run_id),
            )
            connection.commit()
        return self._execute(run_id, record.ir, inputs=inputs)

    def start_snapshot(
        self,
        workflow_id: str,
        ir,
        inputs: Mapping[str, Any],
        *,
        template_id: str,
        idempotency_key: str,
        actor: str = "system:langgraph",
    ) -> LangGraphRun:
        """Start an immutable per-Run graph without publishing a Workflow version."""

        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        if not actor.strip():
            raise ValueError("actor is required")
        snapshot = to_primitive(ir)
        # Fail before recording a Run if the template cannot bind to this Runtime.
        compile_workflow(ir, self.handlers)
        request_hash = definition_hash({
            "template_id": template_id, "graph": snapshot,
            "inputs": inputs, "actor": actor,
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
            run_id = "langgraph_run:" + uuid.uuid4().hex
            now = self._stamp()
            connection.execute(
                "INSERT INTO langgraph_runs("
                "run_id,workflow_id,workflow_version,status,revision,input_json,"
                "result_json,error,created_at,updated_at,owner_actor,template_id,"
                "graph_snapshot_json) VALUES (?,?,0,'running',0,?,NULL,NULL,?,?,?,?,?)",
                (
                    run_id, workflow_id, canonical_json(inputs), now, now, actor,
                    template_id, canonical_json(snapshot),
                ),
            )
            connection.execute(
                "INSERT INTO langgraph_run_receipts VALUES (?,?,?)",
                (idempotency_key, request_hash, run_id),
            )
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
            record = self._workflow(workflow_id, workflow_version)
            compile_workflow(record.ir, self.handlers)
        except LookupError as exc:
            return {"compatible": False, "reason": "workflow_not_found", "detail": str(exc)}
        except (LangGraphCompileError, ValueError, TypeError) as exc:
            return {
                "compatible": False,
                "reason": "unsupported_workflow",
                "detail": str(exc),
            }
        return {
            "compatible": True,
            "workflow_version": record.version.value,
            "engine": "langgraph",
        }

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
            connection.commit()
        if not ready:
            return self.get(run_id)
        return self._execute(
            run_id, self._run_ir(current), resume=responses,
        )

    def recover(self, run_id: str) -> LangGraphRun:
        """Continue a run whose process ended after a durable checkpoint."""

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
        self, run_id: str, ir, *, available_outputs: Mapping[str, Any]
    ) -> None:
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
                    and edge.source_node in available_outputs
                    for edge in ir.edges
                ):
                    continue
                due = now + timedelta(
                    seconds=int(policy.config["deadline_seconds"]),
                )
                due_at = due.isoformat(timespec="microseconds").replace(
                    "+00:00", "Z",
                )
                timer_id = f"langgraph_timer:{run_id}:join_deadline:{node.id}"
                connection.execute(
                    "INSERT OR IGNORE INTO langgraph_timers("
                    "timer_id,run_id,node_id,attempt_number,due_at,status,"
                    "purpose,target_id) VALUES (?,?,?,?,?,'scheduled',?,?)",
                    (
                        timer_id, run_id, node.id, 1, due_at,
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
            with SqliteSaver.from_conn_string(str(self.checkpoint_db_path)) as saver:
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

    def list_runs(
        self, *, status: str | None = None, limit: int = 100,
        actor: str | None = None,
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
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM langgraph_runs" + where
                + " ORDER BY created_at DESC,run_id LIMIT ?",
                (*params, limit),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

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
            with SqliteSaver.from_conn_string(str(self.checkpoint_db_path)) as saver:
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
            attempt_number = int(connection.execute(
                "SELECT COUNT(*) FROM langgraph_timers"
                " WHERE run_id=? AND purpose='retry' AND target_id=?",
                (run_id, request.node_id),
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
            timer_id = f"langgraph_timer:{run_id}:{request.node_id}:{attempt_number}"
            connection.execute(
                "INSERT OR IGNORE INTO langgraph_timers("
                "timer_id,run_id,node_id,attempt_number,due_at,status,purpose,target_id)"
                " VALUES (?,?,?,?,?,'scheduled','retry',?)",
                (
                    timer_id, run_id, request.node_id, attempt_number, due_at,
                    request.node_id,
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
        )
