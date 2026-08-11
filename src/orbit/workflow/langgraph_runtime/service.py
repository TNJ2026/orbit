"""Durable application service for the isolated LangGraph execution adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping
import uuid

from langgraph.checkpoint.sqlite import SqliteSaver

from ..domain.serialization import canonical_json, definition_hash, to_primitive
from .compiler import (
    LangGraphCompileError, LangGraphHandlerRegistry,
    LangGraphUnknownExternalResult, compile_workflow,
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
    ) -> LangGraphRun:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        record = self._workflow(workflow_id, workflow_version)
        request_hash = definition_hash({
            "workflow_id": workflow_id,
            "workflow_version": record.version.value,
            "inputs": inputs,
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
                "result_json,error,created_at,updated_at)"
                " VALUES (?,?,?,'running',0,?,NULL,NULL,?,?)",
                (
                    run_id, workflow_id, record.version.value,
                    canonical_json(inputs), now, now,
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
                " AND status IN ('running','interrupted')",
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
            "running", "interrupted", "completed", "failed", "unknown",
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

        return tuple(self.recover(item.run_id) for item in self.list_runs(
            status="running", limit=limit,
        ))

    def _execute(self, run_id: str, ir, *, inputs=..., resume=...) -> LangGraphRun:
        config = {"configurable": {"thread_id": run_id}}
        try:
            with SqliteSaver.from_conn_string(str(self.checkpoint_db_path)) as saver:
                workflow = compile_workflow(ir, self.handlers, checkpointer=saver)
                if resume is not ...:
                    result = workflow.resume(resume, config=config)
                else:
                    result = workflow.invoke(None if inputs is None else inputs, config=config)
                snapshot = workflow.graph.get_state(config)
            status = "interrupted" if snapshot.next else "completed"
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
        except Exception as exc:
            self._settle(run_id, "failed", error=f"{type(exc).__name__}: {exc}")
            raise

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
        )
