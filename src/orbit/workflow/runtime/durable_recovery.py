"""Paged recovery coordinator for jobs, leases, timers, and orphan nodes."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.envelopes import CommandEnvelope
from ..domain.ids import new_id
from ..domain.states import AttemptStatus, JobStatus, NodeRunStatus
from ..domain.execution_plan import GraphExecutionPlan, execution_plan_from_primitive
from ..domain.serialization import to_primitive
from ..domain.versions import Revision


@dataclass(frozen=True)
class DurableRecoveryReport:
    expired_leases: int = 0
    expired_timer_leases: int = 0
    materialized_jobs: int = 0
    unknown_attempts: int = 0
    graph_advances: int = 0
    diagnostics: tuple[str, ...] = ()


class DurableRecoveryScanner:
    def __init__(self, service) -> None:
        self.service = service

    def scan_once(self, now, *, limit=100) -> DurableRecoveryReport:
        with self.service.uow_factory() as uow:
            leases = uow.leases.list_expired(now, limit=limit)
            timers = uow.timers.list_expired_leases(now, limit=limit)
            runs = uow.runs.list_non_terminal(limit=limit)
        expired = 0
        timer_expired = 0
        materialized = 0
        unknown = 0
        graph_advances = 0
        diagnostics = []
        for lease in leases:
            result = self.service.expire_lease(lease.lease_id, now)
            if result.disposition.value == "applied": expired += 1
        for timer in timers:
            result = self.service.expire_timer_lease(timer.timer_id, now)
            if result.disposition.value == "applied": timer_expired += 1
        for run in runs:
            with self.service.uow_factory() as uow:
                jobs = uow.jobs.list_by_run(run.run_id)
                active_nodes = {
                    item.node_run_id for item in jobs
                    if item.status in {JobStatus.READY, JobStatus.LEASED, JobStatus.RUNNING, JobStatus.RETRY_WAIT}
                }
                nodes = uow.node_runs.list_by_run(run.run_id)
                plan_record = uow.plans.get(run.run_id, Revision(1))
                graph_controllers = set()
                if plan_record is not None:
                    plan = execution_plan_from_primitive(to_primitive(plan_record.plan))
                    if isinstance(plan, GraphExecutionPlan):
                        graph_controllers = {
                            item.node_id for item in plan.nodes
                            if item.kind in {"decision", "join", "terminal"}
                        }
                unknown_nodes = set()
                for node in nodes:
                    for attempt in uow.attempts.list_by_node_run(node.node_run_id):
                        if attempt.status is AttemptStatus.UNKNOWN_EXTERNAL_RESULT:
                            unknown_nodes.add(node.node_run_id)
                            unknown += 1
                            diagnostics.append(f"UNKNOWN_EXTERNAL_RESULT:{attempt.attempt_id}")
            for node in nodes:
                if (
                    node.status in {NodeRunStatus.READY, NodeRunStatus.WAITING}
                    and node.node_run_id not in active_nodes
                    and node.node_run_id not in unknown_nodes
                    and node.node_id not in graph_controllers
                ):
                    result = self.service.submit(CommandEnvelope(
                        new_id("command"), "materialize_job", node.node_run_id,
                        run.run_id, node.aggregate_version,
                        f"materialize:{node.node_run_id}:{node.aggregate_version.value}",
                        "system:recovery", now, {},
                    ))
                    if result.disposition.value == "applied": materialized += 1
            if run.status.value in {"running", "waiting"}:
                result = self.service.submit(CommandEnvelope(
                    new_id("command"), "advance_graph", run.run_id, run.run_id,
                    run.aggregate_version,
                    f"advance-graph:{run.run_id}:{run.aggregate_version.value}",
                    "system:recovery", now, {"plan_version": 1},
                ))
                if result.disposition.value == "applied":
                    graph_advances += 1
        return DurableRecoveryReport(
            expired, timer_expired, materialized, unknown, graph_advances,
            tuple(diagnostics)
        )
