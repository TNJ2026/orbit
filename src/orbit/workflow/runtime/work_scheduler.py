"""Atomic NodeRun-to-Job materialization for the durable production path."""

from __future__ import annotations

from dataclasses import replace

from ..domain.durable_execution import ExecutionSafety, JobRecord
from ..domain.states import JobStatus
from ..domain.versions import AggregateVersion
from .events import derived_id


class DurableWorkScheduler:
    def __init__(
        self, *, execution_safety: ExecutionSafety = ExecutionSafety.UNKNOWN_ON_LEASE_LOSS,
        max_delivery_attempts: int = 3, execution_safety_resolver=None,
        materialization_guard=None,
    ) -> None:
        self.execution_safety = execution_safety
        self.max_delivery_attempts = max_delivery_attempts
        self.execution_safety_resolver = execution_safety_resolver
        self.materialization_guard = materialization_guard

    def create_for_node(self, uow, command, events, node_run):
        if self.materialization_guard is not None:
            self.materialization_guard(uow, node_run, command.issued_at)
        job_id = derived_id("job", node_run.run_id, node_run.node_run_id, "node_execution")
        safety = (
            self.execution_safety_resolver(uow, node_run)
            if self.execution_safety_resolver is not None
            else self.execution_safety
        )
        job = JobRecord(
            job_id, node_run.run_id, node_run.node_run_id, None, "node_execution",
            safety, JobStatus.READY, 0, command.issued_at, 0,
            self.max_delivery_attempts, AggregateVersion(0), command.issued_at,
            command.issued_at,
        )
        uow.jobs.create(job)
        event = events.make(
            job_id, 1, "job_created",
            {
                "run_id": str(node_run.run_id),
                "node_run_id": str(node_run.node_run_id),
                "job_kind": job.job_kind,
                "execution_safety": job.execution_safety.value,
            },
        )
        uow.events.append(node_run.run_id, job_id, AggregateVersion(0), (event,))
        uow.jobs.update(replace(job, aggregate_version=AggregateVersion(1)), AggregateVersion(0))
        return [event.event_id]
