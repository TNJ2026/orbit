"""Application facade for durable Job/Lease/Timer execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import secrets

from ..domain.durable_execution import ExecutionSafety
from ..domain.states import JobStatus, LeaseStatus, TimerStatus
from ..domain.envelopes import CommandEnvelope
from ..domain.execution_plan import execution_plan_from_primitive
from ..domain.handler_context import ExecutorRequest
from ..domain.ids import EntityId, new_id
from ..domain.versions import AggregateVersion
from ..domain.serialization import to_primitive
from ..domain.serialization import definition_hash
from ..persistence.uow import SQLiteUnitOfWork
from ..persistence.workflow_versions import SQLiteWorkflowVersionStore
from ..runtime.durable_kernel import DurableRuntimeKernel, token_hash
from ..runtime.events import derived_id
from ..runtime.recovery import RuntimeRecovery
from ..runtime.durable_recovery import DurableRecoveryScanner
from ..runtime.snapshot_coordinator import SnapshotCoordinator
from ..runtime.work_scheduler import DurableWorkScheduler
from .budget_service import BudgetService


def _time(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class ClaimedWork:
    result: object
    job_id: EntityId
    lease_id: EntityId
    attempt_id: EntityId
    lease_token: str
    fencing_token: int


@dataclass(frozen=True)
class ClaimedTimer:
    result: object
    timer_id: EntityId
    lease_token: str
    fencing_token: int


@dataclass(frozen=True)
class JobDetail:
    job_id: EntityId
    run_id: EntityId
    node_run_id: EntityId
    current_attempt_id: EntityId | None
    status: JobStatus
    execution_safety: ExecutionSafety
    priority: int
    available_at: datetime
    delivery_count: int
    max_delivery_attempts: int
    aggregate_version: AggregateVersion


@dataclass(frozen=True)
class LeaseDetail:
    lease_id: EntityId
    job_id: EntityId
    attempt_id: EntityId
    worker_id: str
    fencing_token: int
    status: LeaseStatus
    acquired_at: datetime
    expires_at: datetime
    released_at: datetime | None
    renewal_revision: int


@dataclass(frozen=True)
class TimerDetail:
    timer_id: EntityId
    run_id: EntityId
    purpose: str
    target_type: str
    target_id: EntityId
    status: TimerStatus
    due_at: datetime
    fired_at: datetime | None
    lease_owner: str | None
    lease_fencing_token: int
    lease_expires_at: datetime | None


@dataclass(frozen=True)
class QueueSummary:
    claimable_jobs: int
    due_timers: int
    active_leases: int
    oldest_ready_age_seconds: float
    oldest_lease_age_seconds: float


def _job_detail(record) -> JobDetail:
    return JobDetail(
        record.job_id, record.run_id, record.node_run_id,
        record.current_attempt_id, record.status, record.execution_safety,
        record.priority, record.available_at, record.delivery_count,
        record.max_delivery_attempts, record.aggregate_version,
    )


def _lease_detail(record) -> LeaseDetail:
    return LeaseDetail(
        record.lease_id, record.job_id, record.attempt_id, record.worker_id,
        record.fencing_token.value, record.status, record.acquired_at,
        record.expires_at, record.released_at, record.renewal_revision,
    )


def _timer_detail(record) -> TimerDetail:
    return TimerDetail(
        record.timer_id, record.run_id, record.purpose.value,
        record.target_type, record.target_id, record.status, record.due_at,
        record.fired_at, record.lease_owner, record.lease_fencing_token,
        record.lease_expires_at,
    )


class DurableRuntimeApplicationService:
    def __init__(
        self, path: Path | str, *, schema_validator=None,
        execution_safety: ExecutionSafety = ExecutionSafety.REPLAY_SAFE,
        execution_registry=None,
        artifact_backend=None,
        uow_factory=None,
        human_task_delivery=None,
        planner_service=None,
        budget_service=None,
    ) -> None:
        self.path = Path(path)
        self.execution_registry = execution_registry
        self.artifact_backend = artifact_backend
        self.workflow_versions = SQLiteWorkflowVersionStore(self.path)
        self.budget_service = budget_service or BudgetService(self.path)
        # Injectable so the memory adapter can be driven through the same
        # service the production one uses. Assigning `service.uow_factory`
        # afterwards does not work — the kernel, scheduler and recovery
        # scanner all capture the factory here — and a parity test that
        # silently kept writing to SQLite would prove nothing.
        self.uow_factory = uow_factory or (lambda: SQLiteUnitOfWork(self.path))
        self.snapshots = SnapshotCoordinator(self.uow_factory)
        self.scheduler = DurableWorkScheduler(
            execution_safety=execution_safety,
            execution_safety_resolver=(
                self._resolve_execution_safety
                if execution_registry is not None else None
            ),
            materialization_guard=(
                self._guard_materialization
                if execution_registry is not None else None
            ),
        )
        self.kernel = DurableRuntimeKernel(
            self.uow_factory, self.workflow_versions,
            snapshot_coordinator=self.snapshots, schema_validator=schema_validator,
            work_scheduler=self.scheduler,
            human_task_delivery=human_task_delivery,
            planner_service=planner_service,
            budget_service=self.budget_service,
        )
        self.recovery = RuntimeRecovery(self.uow_factory)
        self.durable_recovery = DurableRecoveryScanner(self)

    def _resolve_execution_safety(self, uow, node_run):
        plan_record = uow.plans.get(node_run.run_id, node_run.source_plan_version)
        plan = execution_plan_from_primitive(plan_record.plan)
        node = plan.node(node_run.node_id)
        return self.execution_registry.resolve(
            node.handler_name, node.handler_version,
            expected_manifest_fingerprint=node.handler_manifest_fingerprint,
        ).manifest.execution_safety

    def _guard_materialization(self, uow, node_run, now):
        """Block external-write Jobs until a scope-bound Approval Fact exists."""
        plan_record = uow.plans.get(node_run.run_id, node_run.source_plan_version)
        plan = execution_plan_from_primitive(plan_record.plan)
        node = plan.node(node_run.node_id)
        entry = self.execution_registry.resolve(
            node.handler_name, node.handler_version,
            expected_manifest_fingerprint=node.handler_manifest_fingerprint,
        )
        configured = tuple(node.config.get("capabilities", ()))
        capabilities = tuple(sorted(set((*entry.manifest.capabilities, *configured))))
        external = tuple(
            capability for capability in capabilities
            if capability == "external_write"
            or capability.startswith("write:")
            or capability.endswith(".write")
        )
        if node.config.get("external_write") and not external:
            external = ("external_write",)
        for capability in external:
            payload = {
                "node_run_id": str(node_run.node_run_id),
                "capability": capability,
                "plan_version": node_run.source_plan_version.value,
            }
            request_hash = definition_hash({
                "run": str(node_run.run_id), "kind": "approval",
                "payload": payload, "scope": capability,
                "participants": (), "quorum": "any", "count": 1,
            }).value
            approved = uow.connection.execute(
                """SELECT 1 FROM human_tasks
                   WHERE run_id=? AND kind='approval' AND status='completed'
                     AND capability_scope=? AND request_hash=?
                     AND (deadline_at IS NULL OR deadline_at>?)""",
                (
                    str(node_run.run_id), capability, request_hash,
                    now.isoformat(),
                ),
            ).fetchone()
            if approved is None:
                raise PermissionError(
                    f"approval required for {capability} on {node_run.node_run_id}"
                )

    def submit(self, command): return self.kernel.handle(command)

    def submit_human_task(
        self, task_id, run_id, expected_version, *, token, decision, value,
        actor, idempotency_key, now,
    ):
        result = self.submit(CommandEnvelope(
            new_id("command"), "submit_human_task", task_id, run_id,
            AggregateVersion(expected_version), idempotency_key, actor, now,
            {"submission_token": token, "decision": decision, "value": value},
        ))
        if result.disposition.value == "rejected":
            diagnostic = result.diagnostics[0]
            if diagnostic.code == "POLICY_REJECTED":
                raise PermissionError(diagnostic.message)
            raise ValueError(diagnostic.message)
        return dict(result.summary)

    def get_run(self, run_id):
        with self.uow_factory() as uow: return uow.runs.get(run_id)

    def get_plan(self, run_id, version=1):
        from ..domain.versions import Revision
        with self.uow_factory() as uow: return uow.plans.get(run_id, Revision(version))

    def get_timeline(self, run_id, *, after=0, limit=1000):
        with self.uow_factory() as uow:
            return uow.events.read_run(run_id, after_global_position=after, limit=limit)

    def list_jobs(self, run_id):
        with self.uow_factory() as uow:
            return tuple(_job_detail(item) for item in uow.jobs.list_by_run(run_id))

    def get_job(self, job_id):
        with self.uow_factory() as uow:
            record = uow.jobs.get(job_id)
            return None if record is None else _job_detail(record)

    def get_lease(self, lease_id):
        with self.uow_factory() as uow:
            record = uow.leases.get(lease_id)
            return None if record is None else _lease_detail(record)

    def get_timer(self, timer_id):
        with self.uow_factory() as uow:
            record = uow.timers.get(timer_id)
            return None if record is None else _timer_detail(record)

    def build_executor_request(self, claimed: ClaimedWork, now: datetime) -> ExecutorRequest:
        if self.execution_registry is None or not self.execution_registry.sealed:
            raise RuntimeError("a sealed ExecutionRegistry is required")
        with self.uow_factory() as uow:
            job = uow.jobs.get(claimed.job_id)
            lease = uow.leases.get(claimed.lease_id)
            attempt = uow.attempts.get(claimed.attempt_id)
            node = uow.node_runs.get(job.node_run_id)
            plan_record = uow.plans.get(job.run_id, node.source_plan_version)
            plan = execution_plan_from_primitive(plan_record.plan)
            plan_node = plan.node(node.node_id)
            input_value = next(
                item.envelope.payload["input"]
                for item in uow.events.read_stream(node.node_run_id, limit=1000)
                if item.envelope.event_type == "node_input_prepared"
            )
        entry = self.execution_registry.resolve(
            plan_node.handler_name, plan_node.handler_version,
            expected_manifest_fingerprint=plan_node.handler_manifest_fingerprint,
        )
        manifest = entry.manifest
        return ExecutorRequest(
            job.run_id, plan.plan_id, plan.plan_version, node.node_run_id,
            attempt.attempt_id, attempt.attempt_number, job.job_id,
            lease.lease_id, node.node_id, manifest.name, manifest.version,
            manifest.fingerprint, plan_node.config, input_value,
            manifest.inputs, manifest.outputs,
            f"{job.run_id}+{node.node_run_id}+{attempt.attempt_number.value}",
            now + timedelta(seconds=manifest.resource_profile.max_duration_seconds),
            manifest.execution_safety, manifest.resource_profile,
            # The port transports a Handler needs to route its own output; the
            # manifest carries only schemas.
            tuple(plan_node.outputs),
        )

    def build_legacy_executor_input(self, claimed: ClaimedWork):
        """Test-only compatibility DTO for the Step 5 callable executor."""
        with self.uow_factory() as uow:
            job = uow.jobs.get(claimed.job_id)
            node = uow.node_runs.get(job.node_run_id)
            input_value = next(
                item.envelope.payload["input"]
                for item in uow.events.read_stream(node.node_run_id, limit=1000)
                if item.envelope.event_type == "node_input_prepared"
            )
            return node.node_id, dict(input_value)

    def claim_job(self, worker_id: str, now: datetime, *, lease_ttl: timedelta = timedelta(seconds=30)) -> ClaimedWork | None:
        with self.uow_factory() as uow:
            candidates = uow.jobs.list_claimable(now, limit=1)
        if not candidates:
            return None
        job = candidates[0]
        raw_token = secrets.token_urlsafe(32)
        lease_id = new_id("lease")
        command = CommandEnvelope(
            new_id("command"), "claim_job", job.job_id, job.run_id,
            job.aggregate_version, f"claim:{lease_id}", f"worker:{worker_id}", now,
            {
                "worker_id": worker_id, "lease_id": str(lease_id),
                "token_hash": token_hash(raw_token), "token_hash_version": "1.0",
                "lease_expires_at": _time(now + lease_ttl), "observed_at": _time(now),
            },
        )
        result = self.submit(command)
        if result.disposition.value != "applied":
            return None
        return ClaimedWork(
            result, job.job_id, lease_id, EntityId.parse(result.summary["attempt_id"]),
            raw_token, int(result.summary["fencing_token"]),
        )

    def _job_command(self, claimed: ClaimedWork, command_type: str, now: datetime, extra=None):
        expected = claimed.result.primary_version
        if command_type in {
            "complete_job", "fail_job", "defer_job",
            "report_unknown_job_result",
        }:
            expected = expected.next()
        return self.submit(CommandEnvelope(
            derived_id("command", claimed.lease_id, command_type, claimed.fencing_token),
            command_type, claimed.job_id, self._job_run(claimed.job_id), expected,
            f"{command_type}:{claimed.lease_id}:{claimed.fencing_token}",
            "worker:runtime", now,
            {
                "lease_id": str(claimed.lease_id),
                "lease_token": claimed.lease_token,
                "fencing_token": claimed.fencing_token,
                **(extra or {}),
            },
        ))

    def start_job(self, claimed, now): return self._job_command(claimed, "start_job", now)
    def release_job(self, claimed, now): return self._job_command(claimed, "release_job", now)
    @staticmethod
    def _execution_metadata(result):
        return {
            "usage": None if result.usage is None else to_primitive(result.usage),
            "usage_incomplete": result.usage_incomplete,
            "provider_request_id": result.provider_request_id,
        }

    def complete_job(self, claimed, now, output, *, handler_result=None):
        extra = {"output": output}
        if handler_result is not None:
            extra["execution_metadata"] = self._execution_metadata(handler_result)
            extra["artifact_refs"] = [str(item) for item in handler_result.artifact_refs]
        references = () if handler_result is None else handler_result.artifact_refs
        if not references:
            return self._job_command(claimed, "complete_job", now, extra)
        if self.artifact_backend is None:
            raise RuntimeError("Artifact completion requires a configured Blob backend")
        # The shared mutation lock closes the preflight-to-commit deletion gap
        # with GC while keeping filesystem access outside the deterministic Kernel.
        with self.artifact_backend.mutation_lock():
            with self.uow_factory() as uow:
                for artifact_id in references:
                    metadata = uow.artifacts.get(artifact_id)
                    if metadata is None or not self.artifact_backend.verify(
                        metadata.blob_key, metadata.checksum, metadata.size_bytes
                    ):
                        raise ValueError("Artifact Blob failed pre-commit integrity verification")
            return self._job_command(claimed, "complete_job", now, extra)

    def fail_job(self, claimed, now, error, *, handler_result=None):
        extra = {"error": error}
        if handler_result is not None:
            extra["execution_metadata"] = self._execution_metadata(handler_result)
        return self._job_command(claimed, "fail_job", now, extra)

    def report_unknown_job_result(self, claimed, now, result):
        return self._job_command(
            claimed, "report_unknown_job_result", now,
            {
                "error": to_primitive(result.error),
                "execution_metadata": self._execution_metadata(result),
            },
        )
    def defer_job(self, claimed, now, available_at, reason):
        return self._job_command(claimed, "defer_job", now, {"available_at": _time(available_at), "reason": reason})

    def renew_lease(self, claimed, *, expected_revision, expires_at):
        with self.uow_factory() as uow:
            current = uow.leases.get(claimed.lease_id)
            if current is None or expires_at - current.expires_at > timedelta(minutes=5):
                raise ValueError("lease renewal exceeds maximum extension")
            result = uow.leases.renew(
                claimed.lease_id, token_hash=token_hash(claimed.lease_token),
                fencing_token=claimed.fencing_token,
                expected_revision=expected_revision, expires_at=expires_at,
            )
            uow.commit()
            return result

    def expire_lease(self, lease_id, now):
        with self.uow_factory() as uow:
            lease = uow.leases.get(lease_id)
        return self.submit(CommandEnvelope(
            new_id("command"), "expire_lease", lease_id,
            self._job_run(lease.job_id), lease.aggregate_version,
            f"expire:{lease_id}:{lease.fencing_token.value}", "system:lease-reaper", now,
            {"observed_at": _time(now), "fencing_token": lease.fencing_token.value},
        ))

    def _job_run(self, job_id):
        with self.uow_factory() as uow: return uow.jobs.get(job_id).run_id

    def schedule_timer(self, run_id, *, purpose, dedupe_key, target_type, target_id, payload, due_at, now):
        timer_id = derived_id("timer", run_id, purpose, dedupe_key)
        return self.submit(CommandEnvelope(
            new_id("command"), "schedule_timer", timer_id, run_id,
            AggregateVersion(0), f"timer:{purpose}:{dedupe_key}", "system:timer", now,
            {
                "purpose": purpose, "dedupe_key": dedupe_key,
                "target_type": target_type, "target_id": str(target_id),
                "payload_schema_version": "1.0", "payload": payload,
                "due_at": _time(due_at),
            },
        ))

    def claim_timer(self, worker_id, now, *, lease_ttl=timedelta(seconds=15)):
        with self.uow_factory() as uow:
            candidates = uow.timers.list_due(now, limit=1)
        if not candidates: return None
        timer = candidates[0]
        raw = secrets.token_urlsafe(32)
        result = self.submit(CommandEnvelope(
            new_id("command"), "claim_timer", timer.timer_id, timer.run_id,
            timer.aggregate_version, f"claim-timer:{timer.timer_id}:{timer.lease_fencing_token + 1}",
            f"worker:{worker_id}", now,
            {
                "worker_id": worker_id, "token_hash": token_hash(raw),
                "lease_expires_at": _time(now + lease_ttl), "observed_at": _time(now),
            },
        ))
        if result.disposition.value != "applied": return None
        return ClaimedTimer(result, timer.timer_id, raw, int(result.summary["fencing_token"]))

    def fire_timer(self, claimed, now):
        with self.uow_factory() as uow: timer = uow.timers.get(claimed.timer_id)
        return self.submit(CommandEnvelope(
            new_id("command"), "fire_timer", timer.timer_id, timer.run_id,
            timer.aggregate_version, f"fire:{timer.timer_id}:{claimed.fencing_token}",
            "system:timer", now,
            {"lease_token": claimed.lease_token, "fencing_token": claimed.fencing_token},
        ))

    def expire_timer_lease(self, timer_id, now):
        with self.uow_factory() as uow: timer = uow.timers.get(timer_id)
        return self.submit(CommandEnvelope(
            new_id("command"), "expire_timer_lease", timer_id, timer.run_id,
            timer.aggregate_version,
            f"expire-timer:{timer_id}:{timer.lease_fencing_token}",
            "system:timer-recovery", now,
            {"observed_at": _time(now), "fencing_token": timer.lease_fencing_token},
        ))

    def queue_summary(self, now):
        with self.uow_factory() as uow:
            jobs = uow.jobs.list_claimable(now, limit=1000)
            timers = uow.timers.list_due(now, limit=1000)
            runs = uow.runs.list_non_terminal(limit=1000)
            all_jobs = [job for run in runs for job in uow.jobs.list_by_run(run.run_id)]
            leases = [
                lease for job in all_jobs for lease in uow.leases.list_by_job(job.job_id)
                if lease.status.value == "active"
            ]
            return QueueSummary(
                claimable_jobs=len(jobs), due_timers=len(timers),
                active_leases=len(leases),
                oldest_ready_age_seconds=0.0 if not jobs else max(
                    0.0, (now - min(item.available_at for item in jobs)).total_seconds()
                ),
                oldest_lease_age_seconds=0.0 if not leases else max(
                    0.0, (now - min(item.acquired_at for item in leases)).total_seconds()
                ),
            )
