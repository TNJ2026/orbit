"""Safe production wiring for the opt-in LangGraph adapter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from threading import Lock
from types import SimpleNamespace
from typing import Any

from ..domain.handlers import UnknownExternalResultError
from ..domain.serialization import canonical_json
from ..handlers import AgentHandler, TransformHandler
from ..handlers.agent import AgentRequest
from ..persistence.workflow_versions import SQLiteWorkflowVersionStore
from .compiler import (
    BoundHandler, LangGraphHandlerRegistry, LangGraphUnknownExternalResult,
)
from .service import LangGraphWorkflowService


def _transform(inputs: Mapping[str, Any], config: Mapping[str, Any], _context):
    operation = config.get("operation", "identity")
    if operation == "identity":
        return dict(inputs)
    if operation == "select_fields":
        fields = config.get("fields")
        if not isinstance(fields, (list, tuple)):
            raise ValueError("transform fields must be an array")
        return {key: inputs[key] for key in fields if key in inputs}
    if operation == "build_object":
        value = config.get("value")
        if not isinstance(value, Mapping):
            raise ValueError("transform value must be an object")
        return dict(value)
    raise ValueError(f"unsupported transform operation: {operation}")


class _DiscardedOutput:
    def emit(self, _stream: str, _text: str) -> None:
        return None


class _AgentAttemptJournal:
    """Prevent an Agent side effect from being replayed after process loss."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS langgraph_handler_attempts("
                "attempt_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,node_id TEXT NOT NULL,"
                "status TEXT NOT NULL,output_json TEXT,error TEXT,updated_at TEXT NOT NULL)"
            )

    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def claim(self, context) -> Mapping[str, Any] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status,output_json FROM langgraph_handler_attempts"
                " WHERE attempt_id=?", (context.attempt_id,),
            ).fetchone()
            if row is not None:
                connection.commit()
                if row["status"] == "succeeded":
                    return json.loads(row["output_json"])
                raise LangGraphUnknownExternalResult(
                    f"Agent attempt {context.attempt_id} has {row['status']} outcome"
                )
            connection.execute(
                "INSERT INTO langgraph_handler_attempts VALUES (?,?,?,'started',NULL,NULL,?)",
                (context.attempt_id, context.run_id, context.node_id, self._now()),
            )
            connection.commit()
        return None

    def settle(
        self, attempt_id: str, status: str, *, output=None, error: str | None = None
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE langgraph_handler_attempts SET status=?,output_json=?,error=?,"
                "updated_at=? WHERE attempt_id=?",
                (
                    status,
                    None if output is None else canonical_json(output),
                    error,
                    self._now(),
                    attempt_id,
                ),
            )
            connection.commit()


def _agent_adapter(implementation: AgentHandler, manifest, journal):
    active: dict[str, set[str]] = {}
    active_lock = Lock()

    def invoke(inputs, config, context):
        replay = journal.claim(context)
        if replay is not None:
            return replay
        deadline = datetime.now(timezone.utc) + timedelta(
            seconds=manifest.resource_profile.max_duration_seconds
        )
        request_context = SimpleNamespace(
            request=SimpleNamespace(
                attempt_id=context.attempt_id,
                deadline=deadline,
                process_deadline=deadline,
                input=inputs,
                config=config,
                input_ports=(),
                output_ports=(),
            ),
            output=_DiscardedOutput(),
            artifacts=None,
        )
        try:
            with active_lock:
                active.setdefault(context.run_id, set()).add(context.attempt_id)
            try:
                response = implementation.client.execute(
                    AgentRequest(inputs, config, context.attempt_id), request_context
                )
            finally:
                with active_lock:
                    attempts = active.get(context.run_id)
                    if attempts is not None:
                        attempts.discard(context.attempt_id)
                        if not attempts:
                            active.pop(context.run_id, None)
            if not isinstance(response.output, Mapping):
                raise ValueError("Agent output must be an object")
            output = dict(response.output)
            journal.settle(context.attempt_id, "succeeded", output=output)
            return output
        except UnknownExternalResultError as exc:
            journal.settle(context.attempt_id, "unknown", error=str(exc))
            raise LangGraphUnknownExternalResult(str(exc)) from None
        except Exception as exc:
            journal.settle(
                context.attempt_id, "failed", error=f"{type(exc).__name__}: {exc}"
            )
            raise

    def cancel_run(run_id: str) -> bool:
        with active_lock:
            attempts = tuple(active.get(run_id, ()))
        for attempt_id in attempts:
            implementation.client.cancel(f"agent:{attempt_id}")
        return bool(attempts)

    return invoke, cancel_run


def trusted_handlers(
    registrations: Sequence[Any], *, attempt_db_path: Path | str | None = None
) -> LangGraphHandlerRegistry:
    """Expose only reviewed adapters, never arbitrary Orbit NodeHandlers."""

    handlers: list[BoundHandler] = []
    for registration in registrations:
        manifest = registration.manifest
        if isinstance(registration.implementation, TransformHandler):
            handlers.append(BoundHandler(
                manifest.name,
                manifest.version,
                manifest.fingerprint,
                _transform,
            ))
        elif (
            isinstance(registration.implementation, AgentHandler)
            and attempt_db_path is not None
        ):
            journal = _AgentAttemptJournal(attempt_db_path)
            invoke, cancel_run = _agent_adapter(
                registration.implementation, manifest, journal
            )
            handlers.append(BoundHandler(
                manifest.name,
                manifest.version,
                manifest.fingerprint,
                invoke,
                cancel_run,
            ))
    return LangGraphHandlerRegistry(handlers)


def build_service(
    workflow_db_path: Path | str,
    registrations: Sequence[Any],
    *,
    state_directory: Path | str,
) -> LangGraphWorkflowService:
    """Build the isolated service and its two adapter-owned databases."""

    state = Path(state_directory)
    return LangGraphWorkflowService(
        SQLiteWorkflowVersionStore(workflow_db_path),
        trusted_handlers(
            registrations,
            attempt_db_path=state / "langgraph-runs.sqlite3",
        ),
        run_db_path=state / "langgraph-runs.sqlite3",
        checkpoint_db_path=state / "langgraph-checkpoints.sqlite3",
    )
