"""Application facade for the deterministic Runtime Kernel."""

from __future__ import annotations

from pathlib import Path

from ..domain.envelopes import CommandEnvelope
from ..domain.ids import EntityId
from ..domain.versions import Revision
from ..domain.execution_plan import execution_plan_from_primitive
from ..domain.serialization import to_primitive
from ..domain.states import AttemptStatus, JobStatus, NodeRunStatus, TimerStatus
from ..persistence.uow import SQLiteUnitOfWork
from ..persistence.workflow_versions import SQLiteWorkflowVersionStore
from ..runtime.kernel import RuntimeKernel
from ..runtime.recovery import RuntimeRecovery
from ..runtime.snapshot_coordinator import SnapshotCoordinator


class RuntimeApplicationService:
    def __init__(self, path: Path | str, *, schema_validator=None) -> None:
        self.path = Path(path)
        self.workflow_versions = SQLiteWorkflowVersionStore(self.path)
        self.uow_factory = lambda: SQLiteUnitOfWork(self.path)
        self.snapshots = SnapshotCoordinator(self.uow_factory)
        self.kernel = RuntimeKernel(
            self.uow_factory, self.workflow_versions,
            snapshot_coordinator=self.snapshots,
            schema_validator=schema_validator,
        )
        self.recovery = RuntimeRecovery(self.uow_factory)

    def submit(self, command: CommandEnvelope):
        return self.kernel.handle(command)

    def get_run(self, run_id: EntityId):
        with self.uow_factory() as uow:
            return uow.runs.get(run_id)

    def get_plan(self, run_id: EntityId, version: int = 1):
        with self.uow_factory() as uow:
            return uow.plans.get(run_id, Revision(version))

    def get_timeline(self, run_id: EntityId, *, after: int = 0, limit: int = 1000):
        with self.uow_factory() as uow:
            return uow.events.read_run(run_id, after_global_position=after, limit=limit)

    def list_unfinished(self, *, after_run_id: str = "", limit: int = 100):
        return self.recovery.list_unfinished(after_run_id=after_run_id, limit=limit)

    def get_graph_summary(self, run_id: EntityId, version: int = 1):
        """Return a repository-free diagnostic DTO for UI and Planner context."""
        with self.uow_factory() as uow:
            run = uow.runs.get(run_id)
            record = uow.plans.get(run_id, Revision(version))
            if run is None or record is None:
                return None
            plan = execution_plan_from_primitive(to_primitive(record.plan))
            nodes = uow.node_runs.list_by_run(run_id)
            tokens = uow.tokens.list_by_run(run_id)
            joins = uow.joins.list_by_run(run_id)
            jobs = uow.jobs.list_by_run(run_id)
            timers = uow.timers.list_by_run(run_id)
            attempts = tuple(
                attempt for node in nodes
                for attempt in uow.attempts.list_by_node_run(node.node_run_id)
            )
            waiting_reason = None
            human_waiting = uow.connection.execute(
                """SELECT 1 FROM human_tasks WHERE run_id=?
                   AND status IN ('waiting','claimed') LIMIT 1""",
                (str(run_id),),
            ).fetchone()
            if human_waiting is not None:
                waiting_reason = "human_wait"
            elif any(item.status is AttemptStatus.UNKNOWN_EXTERNAL_RESULT for item in attempts):
                waiting_reason = "unknown_wait"
            elif any(item.status is JobStatus.RETRY_WAIT for item in jobs):
                waiting_reason = "retry_wait"
            elif any(item.status.value == "waiting" for item in joins):
                waiting_reason = "join_wait"
            elif any(item.status is TimerStatus.SCHEDULED for item in timers):
                waiting_reason = "timer_wait"
            elif run.status.value == "running" and not any(
                item.status in {NodeRunStatus.PENDING, NodeRunStatus.READY, NodeRunStatus.RUNNING, NodeRunStatus.WAITING}
                for item in nodes
            ):
                waiting_reason = "stalled"
            return {
                "run_id": str(run_id), "status": run.status.value,
                "plan_version": version,
                "plan_schema_version": plan.schema_version.value,
                "nodes": [
                    {
                        "node_run_id": str(item.node_run_id), "node_id": item.node_id,
                        "generation": item.generation, "status": item.status.value,
                        "activation_key": item.activation_key,
                    }
                    for item in nodes
                ],
                "tokens": [
                    {"token_id": str(item.token_id), "status": item.status.value, "scope": to_primitive(item.scope)}
                    for item in tokens
                ],
                "joins": [
                    {"join_group_id": str(item.join_group_id), "node_id": item.node_id, "status": item.status.value}
                    for item in joins
                ],
                "waiting_reason": waiting_reason,
            }
