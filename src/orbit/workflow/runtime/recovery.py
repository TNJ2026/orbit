"""Runtime run-view replay and paged unfinished-run discovery."""

from __future__ import annotations

from ..domain.ids import EntityId
from ..persistence.rehydration import rehydrate_run_view
from .event_reader import runtime_event_reader
from .reducers import reduce_run_view
from .snapshot_coordinator import RUNTIME_REDUCER_VERSION, SNAPSHOT_SCHEMA_VERSION


class RuntimeRecovery:
    def __init__(self, uow_factory) -> None:
        self.uow_factory = uow_factory
        self.reader = runtime_event_reader()

    def rehydrate(self, run_id: EntityId):
        with self.uow_factory() as uow:
            return rehydrate_run_view(
                uow.events, uow.snapshots, run_id,
                {"run_status": None, "nodes": {}, "attempts": {}, "outputs": {}, "jobs": {}, "leases": {}, "timers": {}, "usage": {}},
                reduce_run_view, self.reader,
                snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
                reducer_version=RUNTIME_REDUCER_VERSION,
            )

    def list_unfinished(self, *, after_run_id: str = "", limit: int = 100):
        with self.uow_factory() as uow:
            return uow.runs.list_non_terminal(after_run_id=after_run_id, limit=limit)

    def verify_projection(self, run_id: EntityId) -> tuple[str, ...]:
        report = self.rehydrate(run_id)
        issues = []
        with self.uow_factory() as uow:
            run = uow.runs.get(run_id)
            if run is None:
                return ("WorkflowRun projection is missing",)
            if report.state["run_status"] != run.status.value:
                issues.append(
                    f"Run projection {run.status.value} != replay {report.state['run_status']}"
                )
            for node in uow.node_runs.list_by_run(run_id):
                replayed = report.state["nodes"].get(str(node.node_run_id), {}).get("status")
                if replayed != node.status.value:
                    issues.append(
                        f"NodeRun {node.node_run_id} projection {node.status.value} != replay {replayed}"
                    )
                for attempt in uow.attempts.list_by_node_run(node.node_run_id):
                    replayed_attempt = report.state["attempts"].get(
                        str(attempt.attempt_id), {}
                    ).get("status")
                    if replayed_attempt != attempt.status.value:
                        issues.append(
                            f"Attempt {attempt.attempt_id} projection {attempt.status.value} != replay {replayed_attempt}"
                        )
            for job in uow.jobs.list_by_run(run_id):
                replayed_job = report.state["jobs"].get(
                    str(job.job_id), {}
                ).get("status")
                if replayed_job != job.status.value:
                    issues.append(
                        f"Job {job.job_id} projection {job.status.value} != replay {replayed_job}"
                    )
                for lease in uow.leases.list_by_job(job.job_id):
                    replayed_lease = report.state["leases"].get(
                        str(lease.lease_id), {}
                    ).get("status")
                    if replayed_lease != lease.status.value:
                        issues.append(
                            f"Lease {lease.lease_id} projection {lease.status.value} != replay {replayed_lease}"
                        )
            for timer in uow.timers.list_by_run(run_id):
                replayed_timer = report.state["timers"].get(
                    str(timer.timer_id), {}
                ).get("status")
                if replayed_timer != timer.status.value:
                    issues.append(
                        f"Timer {timer.timer_id} projection {timer.status.value} != replay {replayed_timer}"
                    )
        return tuple(issues)
