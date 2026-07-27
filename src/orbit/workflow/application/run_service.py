"""Run lifecycle use cases shared by HTTP, CLI and MCP.

All three adapters call these methods; none of them build commands or touch the
database themselves. That is what keeps `orbit run start`, `POST /api/v1/runs`
and the MCP `start_run` tool from drifting into three different validations of
the same thing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import contextmanager
import fcntl
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from ..api.read_models import ReadModelService
from ..domain.envelopes import CommandEnvelope
from ..domain.ids import EntityId
from ..domain.versions import AggregateVersion
from ..handlers.agent import AGENT_COMPLETION_MARKER, AGENT_RESULT_PORT
from ..persistence.database import connect_workflow_database


class RunStartError(ValueError):
    """The run could not be started; the message is safe to show a caller."""


class ActiveGoalExistsError(RunStartError):
    """A local workspace already has a foreground Goal in progress."""

    def __init__(self, active_goal: Mapping[str, Any]) -> None:
        self.active_goal = dict(active_goal)
        super().__init__(
            f"active goal already exists: {self.active_goal.get('run_id', 'unknown')}"
        )


@dataclass(frozen=True)
class StartedRun:
    run_id: str
    workflow_id: str
    workflow_version: int
    plan_id: str | None
    disposition: str
    replayed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "workflow_version": self.workflow_version,
            "plan_id": self.plan_id,
            "disposition": self.disposition,
            "replayed": self.replayed,
        }


def derive_run_id(workflow_id: str, version: int, idempotency_key: str) -> EntityId:
    """Deterministic run id.

    Deriving it from the caller's idempotency key means a retried start finds
    the same run through the kernel's receipt, instead of creating a second one
    because the client generated a fresh uuid.
    """

    seed = f"{workflow_id}|{version}|{idempotency_key}"
    return EntityId("run", hashlib.sha256(seed.encode("utf-8")).hexdigest())


class RunApplicationService:
    """Start runs and answer "what is this run doing" for every adapter."""

    def __init__(
        self, path: Path | str, durable_service, *, enforce_single_goal: bool = False,
        workflow_db_path: Path | str | None = None,
    ) -> None:
        self.path = Path(path)
        self.workflow_path = Path(workflow_db_path or path)
        self.service = durable_service
        self.reads = ReadModelService(self.path)
        self.enforce_single_goal = enforce_single_goal

    @contextmanager
    def _start_guard(self):
        """Serialize foreground starts across local server and CLI processes."""

        if not self.enforce_single_goal:
            yield
            return
        lock_path = self.path.with_suffix(self.path.suffix + ".goal.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    # -- start ------------------------------------------------------------

    def active_goal(self) -> dict[str, Any] | None:
        """Return the one user-started root Run that owns the local workspace."""

        with connect_workflow_database(self.path, read_only=True) as connection:
            row = connection.execute(
                "SELECT run_id,display_name,goal,workflow_id,workflow_version,"
                " status,aggregate_version,created_at,updated_at"
                " FROM workflow_runs WHERE run_id=correlation_id"
                " AND status IN ('created','running','waiting','waiting_for_budget',"
                " 'budget_exhausted') ORDER BY updated_at DESC,run_id LIMIT 1"
            ).fetchone()
        return None if row is None else dict(row)

    def resolve_workflow(
        self, workflow_id: str, version: int | None
    ) -> tuple[int, str]:
        """Latest published version and its hash, or the exact one requested."""

        with connect_workflow_database(self.workflow_path, read_only=True) as connection:
            if version is None:
                row = connection.execute(
                    "SELECT version, definition_hash FROM workflow_versions"
                    " WHERE workflow_id = ? ORDER BY version DESC LIMIT 1",
                    (workflow_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT version, definition_hash FROM workflow_versions"
                    " WHERE workflow_id = ? AND version = ?",
                    (workflow_id, version),
                ).fetchone()
        if row is None:
            raise RunStartError(
                f"workflow version not found: {workflow_id}"
                + (f"@{version}" if version is not None else " (no published version)")
            )
        return int(row["version"]), row["definition_hash"]

    def _materialize_workflow(self, workflow_id: str, version: int) -> None:
        """Mirror one public immutable version into the project's FK boundary.

        Runs remain project-local and the schema deliberately references a
        WorkflowVersion. The mirror is immutable execution evidence, not a
        second authoring authority; catalog and publishing continue to use the
        public library.
        """

        if self.workflow_path == self.path:
            return
        with connect_workflow_database(self.workflow_path, read_only=True) as source:
            row = source.execute(
                "SELECT * FROM workflow_versions WHERE workflow_id=? AND version=?",
                (workflow_id, version),
            ).fetchone()
            definition = source.execute(
                "SELECT * FROM workflow_definitions WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
        if row is None or definition is None:
            raise RunStartError(f"workflow version not found: {workflow_id}@{version}")
        columns = (
            "workflow_id", "version", "definition_hash", "dsl_version", "ir_version",
            "compiler_version", "canonical_ir_json", "source_format", "source_text",
            "catalog_fingerprint", "created_at", "created_by",
        )
        with connect_workflow_database(self.path) as target:
            target.execute("BEGIN IMMEDIATE")
            target.execute(
                "INSERT OR IGNORE INTO workflow_definitions"
                "(workflow_id,name,created_at,created_by) VALUES (?,?,?,?)",
                tuple(definition[key] for key in (
                    "workflow_id", "name", "created_at", "created_by",
                )),
            )
            existing = target.execute(
                "SELECT definition_hash FROM workflow_versions"
                " WHERE workflow_id=? AND version=?",
                (workflow_id, version),
            ).fetchone()
            if existing is not None and existing["definition_hash"] != row["definition_hash"]:
                target.rollback()
                raise RunStartError(
                    f"project WorkflowVersion conflicts with public library:"
                    f" {workflow_id}@{version}"
                )
            if existing is None:
                target.execute(
                    f"INSERT INTO workflow_versions({','.join(columns)})"
                    f" VALUES ({','.join('?' for _ in columns)})",
                    tuple(row[key] for key in columns),
                )
            target.commit()

    def _ensure_handlers_available(self, workflow_id: str, version: int) -> None:
        registry = getattr(self.service, "execution_registry", None)
        versions = getattr(self.service, "workflow_versions", None)
        if registry is None or versions is None:
            return
        record = versions.get(workflow_id, version)
        if record is None:
            raise RunStartError(f"workflow version not found: {workflow_id}@{version}")
        for node in record.ir.nodes:
            if node.handler is None:
                continue
            try:
                registry.resolve(
                    node.handler.name, node.handler.version,
                    expected_manifest_fingerprint=node.handler.manifest_fingerprint,
                )
            except (LookupError, ValueError) as exc:
                raise RunStartError(f"HANDLER_UNAVAILABLE: {exc}") from None

    def start_run(
        self,
        *,
        workflow_id: str,
        version: int | None = None,
        inputs: Mapping[str, Any] | None = None,
        goal: str = "",
        budget_microunits: int | None = None,
        actor: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> StartedRun:
        if not actor.strip():
            raise RunStartError("actor is required")
        if not idempotency_key.strip():
            raise RunStartError("idempotency_key is required")
        if budget_microunits is not None and budget_microunits < 0:
            raise RunStartError("budget_microunits must not be negative")

        resolved_version, digest = self.resolve_workflow(workflow_id, version)
        self._ensure_handlers_available(workflow_id, resolved_version)
        self._materialize_workflow(workflow_id, resolved_version)
        run_id = derive_run_id(workflow_id, resolved_version, idempotency_key)
        issued_at = now or datetime.now(timezone.utc)

        payload: dict[str, Any] = {
            "workflow_id": workflow_id,
            "workflow_version": resolved_version,
            "definition_hash": digest,
            "input": dict(inputs or {}),
        }
        if goal:
            payload["goal"] = goal
        if budget_microunits is not None:
            payload["budget_microunits"] = int(budget_microunits)

        command = CommandEnvelope(
            EntityId("command", hashlib.sha256(
                f"start|{run_id}|{idempotency_key}".encode("utf-8")
            ).hexdigest()),
            "start_run", run_id, run_id, AggregateVersion(0),
            f"start_run:{idempotency_key}", actor, issued_at, payload,
        )
        with self._start_guard():
            active = self.active_goal() if self.enforce_single_goal else None
            if active is not None and active["run_id"] != str(run_id):
                raise ActiveGoalExistsError(active)
            result = self.service.submit(command)
        disposition = result.disposition.value
        if disposition not in {"applied", "replayed"}:
            reasons = "; ".join(
                f"{item.code}: {item.message}" for item in result.diagnostics
            ) or "command rejected"
            raise RunStartError(reasons)

        summary = dict(result.summary or {})
        return StartedRun(
            run_id=str(run_id),
            workflow_id=workflow_id,
            workflow_version=resolved_version,
            plan_id=summary.get("plan_id"),
            disposition=disposition,
            replayed=disposition == "replayed",
        )

    def cancel_run(
        self,
        run_id: str,
        expected_version: int,
        *,
        actor: str,
        idempotency_key: str,
        reason: str = "cancelled by operator",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        identifier = EntityId.parse(run_id)
        command = CommandEnvelope(
            EntityId("command", hashlib.sha256(
                f"cancel|{run_id}|{idempotency_key}".encode("utf-8")
            ).hexdigest()),
            "cancel_run", identifier, identifier,
            AggregateVersion(int(expected_version)),
            f"cancel_run:{idempotency_key}", actor,
            now or datetime.now(timezone.utc), {"reason": reason},
        )
        result = self.service.submit(command)
        if result.disposition.value not in {"applied", "replayed"}:
            reasons = "; ".join(
                f"{item.code}: {item.message}" for item in result.diagnostics
            ) or "cancel rejected"
            raise RunStartError(reasons)
        return {"run_id": run_id, "disposition": result.disposition.value}

    def retry_node_run(
        self,
        run_id: str,
        node_run_id: str,
        expected_version: int,
        *,
        actor: str,
        idempotency_key: str,
        reason: str = "retried by operator",
        agent: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Run a NodeRun parked on an unknown external result once more.

        The operator, not the Runtime, decides that re-running is safe: only
        they can know whether the Agent already acted on the outside world.
        """

        identifier = EntityId.parse(node_run_id)
        payload: dict[str, Any] = {"reason": reason}
        if agent:
            payload["handler_override"] = self.service.resolve_retry_handler(agent)
        command = CommandEnvelope(
            EntityId("command", hashlib.sha256(
                f"retry|{node_run_id}|{idempotency_key}".encode("utf-8")
            ).hexdigest()),
            "retry_node_run", identifier, EntityId.parse(run_id),
            AggregateVersion(int(expected_version)),
            f"retry_node_run:{idempotency_key}", actor,
            now or datetime.now(timezone.utc), payload,
        )
        result = self.service.submit(command)
        if result.disposition.value not in {"applied", "replayed"}:
            reasons = "; ".join(
                f"{item.code}: {item.message}" for item in result.diagnostics
            ) or "retry rejected"
            raise RunStartError(reasons)
        summary = result.summary or {}
        return {
            "node_run_id": node_run_id,
            "node_id": summary.get("node_id"),
            "generation": summary.get("generation"),
            "disposition": result.disposition.value,
        }

    def accept_unknown_result(
        self, run_id: str, node_run_id: str, expected_version: int, *,
        attempt_id: str, actor: str, idempotency_key: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        with connect_workflow_database(self.path, read_only=True) as db:
            rows = db.execute(
                "SELECT stream,text FROM attempt_output WHERE attempt_id=?"
                " AND run_id=? ORDER BY chunk_id",
                (attempt_id, run_id),
            ).fetchall()
        stdout = "".join(row["text"] for row in rows if row["stream"] == "stdout")
        lines = stdout.rstrip().splitlines()
        # The marker and the port id come from the Agent handler rather than
        # being spelled again here: this reads output written to that handler's
        # protocol, so a change to the protocol has to reach this code.
        if not lines or lines[-1].strip() != AGENT_COMPLETION_MARKER:
            raise RunStartError("captured stdout has no terminal completion marker")
        body = "\n".join(lines[:-1]).strip()
        matches = re.findall(r"```json\s*(\{.*?\})\s*```", body, re.S)
        if not matches:
            raise RunStartError("captured stdout has no JSON result")
        try:
            value = json.loads(matches[-1])
        except json.JSONDecodeError as exc:
            raise RunStartError("captured JSON result is invalid") from exc
        if not isinstance(value, dict):
            raise RunStartError("captured result must be an object")
        identifier = EntityId.parse(node_run_id)
        command = CommandEnvelope(
            EntityId("command", hashlib.sha256(
                f"accept|{attempt_id}|{idempotency_key}".encode()
            ).hexdigest()),
            "accept_unknown_result", identifier, EntityId.parse(run_id),
            AggregateVersion(int(expected_version)),
            f"accept_unknown_result:{idempotency_key}", actor,
            now or datetime.now(timezone.utc),
            {"attempt_id": attempt_id, "output": {AGENT_RESULT_PORT: value},
             "reason": "accepted captured completed output"},
        )
        result = self.service.submit(command)
        if result.disposition.value not in {"applied", "replayed"}:
            reasons = "; ".join(
                f"{item.code}: {item.message}" for item in result.diagnostics
            ) or "accept rejected"
            raise RunStartError(reasons)
        return {"node_run_id": node_run_id, "attempt_id": attempt_id,
                "disposition": result.disposition.value}

    # -- inspect ----------------------------------------------------------

    def inspect(self, run_id: str) -> dict[str, Any]:
        """Everything an operator needs to answer "why is this run here".

        The projection is built server-side on purpose: a CLI that folds events
        itself becomes a second, silently diverging state machine.
        """

        identifier = EntityId.parse(run_id)
        summary = self.reads.run_summary(identifier)
        responsibilities = self.reads.responsibilities(identifier)
        errors, _ = self.reads.errors(identifier, limit=10)
        return {
            "summary": summary,
            "responsibilities": responsibilities,
            "recent_errors": errors,
        }
