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
from pathlib import Path
from typing import Any, Mapping

from ..api.read_models import ReadModelService
from ..domain.envelopes import CommandEnvelope
from ..domain.ids import EntityId
from ..domain.versions import AggregateVersion
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
        self, path: Path | str, durable_service, *, enforce_single_goal: bool = False
    ) -> None:
        self.path = Path(path)
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

        with connect_workflow_database(self.path, read_only=True) as connection:
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
