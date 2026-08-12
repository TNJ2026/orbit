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
    ) -> LangGraphRun:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        current = self.get(run_id)
        request_hash = definition_hash({
            "command": "resume",
            "run_id": run_id,
            "expected_revision": expected_revision,
            "value": value,
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
            claimed = connection.execute(
                "UPDATE langgraph_runs SET status='running',updated_at=?"
                " WHERE run_id=? AND status='interrupted' AND revision=?",
                (self._stamp(), run_id, expected_revision),
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
        record = self._workflow(current.workflow_id, current.workflow_version)
        return self._execute(run_id, record.ir, resume=value)

    def recover(self, run_id: str) -> LangGraphRun:
        """Continue a run whose process ended after a durable checkpoint."""

        current = self.get(run_id)
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
                changed = connection.execute(
                    "UPDATE langgraph_timers SET status='fired' WHERE timer_id=?"
                    " AND status='scheduled'", (timer["timer_id"],),
                ).rowcount
                if changed != 1:
                    connection.rollback()
                    return self.get(run_id)
                connection.execute(
                    "UPDATE langgraph_runs SET status='running',revision=revision+1,"
                    "updated_at=? WHERE run_id=? AND status IN ('waiting','interrupted')",
                    (self._stamp(), run_id),
                )
                connection.commit()
            current = self.get(run_id)
            if timer["purpose"] == "join_deadline":
                record = self._workflow(
                    current.workflow_id, current.workflow_version,
                )
                return self._fire_join_deadline(
                    run_id, record.ir, timer["target_id"],
                )
        if current.status != "running":
            return current
        record = self._workflow(current.workflow_id, current.workflow_version)
        config = {"configurable": {"thread_id": run_id}}
        with SqliteSaver.from_conn_string(str(self.checkpoint_db_path)) as saver:
            has_checkpoint = saver.get_tuple(config) is not None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT input_json FROM langgraph_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        inputs = None if has_checkpoint else json.loads(row["input_json"])
        return self._execute(run_id, record.ir, inputs=inputs)

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
            return self._settle(run_id, "completed", result=result["result"])
        except LangGraphJoinDeadlineExceeded as exc:
            return self._settle(
                run_id, "failed", error=f"{type(exc).__name__}: {exc}",
            )

    def cancel(
        self,
        run_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> LangGraphRun:
        """Persist cancellation before signalling any in-flight Handler."""

        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
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
                " WHERE run_id=? AND status='scheduled'", (run_id,),
            )
            connection.execute(
                "INSERT INTO langgraph_run_receipts VALUES (?,?,?)",
                (idempotency_key, request_hash, run_id),
            )
            connection.commit()
        self.handlers.cancel(run_id)
        return self.get(run_id)

    def get(self, run_id: str) -> LangGraphRun:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM langgraph_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise LookupError(f"LangGraph run not found: {run_id}")
        return self._record(row)

    def list_runs(
        self, *, status: str | None = None, limit: int = 100
    ) -> tuple[LangGraphRun, ...]:
        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if status is not None and status not in {
            "running", "waiting", "interrupted", "completed", "failed", "unknown",
            "cancelled",
        }:
            raise ValueError("invalid LangGraph run status")
        where, params = ("", ()) if status is None else (" WHERE status=?", (status,))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM langgraph_runs" + where
                + " ORDER BY created_at DESC,run_id LIMIT ?",
                (*params, limit),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def recover_running(self, *, limit: int = 100) -> tuple[LangGraphRun, ...]:
        """Recover a bounded snapshot of runs left running by process loss."""

        recovered = [self.recover(item.run_id) for item in self.list_runs(
            status="running", limit=limit,
        )]
        recovered.extend(self.recover_due(limit=max(1, limit - len(recovered))))
        return tuple(recovered)

    def recover_due(self, *, limit: int = 100) -> tuple[LangGraphRun, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT run_id FROM langgraph_timers"
                " WHERE status='scheduled' AND due_at<=? ORDER BY due_at LIMIT ?",
                (self._stamp(), limit),
            ).fetchall()
        return tuple(self.recover(row["run_id"]) for row in rows)

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
                "UPDATE langgraph_runs SET status=?,revision=revision+1,result_json=?,"
                " interrupts_json=?,error=?,updated_at=? WHERE run_id=?"
                " AND status!='cancelled'",
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
                    " WHERE run_id=? AND status='scheduled'", (run_id,),
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
        )
