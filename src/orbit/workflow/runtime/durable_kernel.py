"""Durable Job/Lease/Timer commands layered on the deterministic Runtime Kernel."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
from typing import Any

from ..domain.concurrency import CommandDisposition
from ..domain.durable_execution import (
    DURABLE_COMMAND_TYPES, DurableTimerRecord, ExecutionSafety, LeaseRecord,
    MAX_JOB_LEASE_TTL, TimerPurpose,
)
from ..domain.envelopes import CommandEnvelope
from ..domain.errors import ErrorCategory, ErrorInfo, LeaseAuthorityError
from ..domain.ids import EntityId
from ..domain.persistence import (
    AttemptRecord, ConcurrencyConflictError, IdempotencyConflictError,
    IntegrityViolationError, PersistenceError, RepositoryAlreadyExistsError,
)
from ..domain.runtime import CommandResult, CommandResultDisposition, KernelDiagnostic
from ..domain.schemas import validate_contract
from ..domain.states import (
    AttemptStatus, JobStatus, LeaseStatus, NodeRunStatus, TimerStatus,
    WorkflowRunStatus,
)
from ..domain.versions import AggregateVersion, Revision, SchemaVersion
from .events import derived_id
from .kernel import RuntimeKernel
from .kernel_families import _EventBuilder, _transition_payload


def token_hash(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


class DurableRuntimeKernel(RuntimeKernel):
    def handle(self, command: CommandEnvelope) -> CommandResult:
        if command.command_type not in DURABLE_COMMAND_TYPES:
            return super().handle(command)
        try:
            validate_contract(
                command.payload,
                f"durable-command/{command.command_type.replace('_', '-')}/1.0",
            )
            with self.uow_factory() as uow:
                prior = uow.receipts.decide(command)
                if prior is not None and prior.disposition is CommandDisposition.REPLAY_PRIOR_RESULT:
                    return CommandResult(
                        CommandResultDisposition.REPLAYED, prior.prior_event_ids,
                        summary=self._durable_summary(command),
                    )
                semantic_replay = self._semantic_timer_replay(uow, command)
                if semantic_replay is not None:
                    return semantic_replay
                builder = _EventBuilder(command)
                event_ids, version, run_id, summary = getattr(
                    self, f"_durable_{command.command_type}"
                )(uow, command, builder)
                uow.receipts.record(run_id, command, tuple(event_ids), command.issued_at)
                uow.commit()
            for task_id, participants, token in builder.human_deliveries:
                try:
                    self.human_task_delivery(task_id, participants, token)
                except Exception as exc:
                    self._log(
                        "human_task_delivery_failed",
                        {"task_id": str(task_id), "error_type": type(exc).__name__},
                    )
            if self.snapshot_coordinator is not None:
                try:
                    self.snapshot_coordinator.consider(run_id)
                except Exception as exc:
                    self._log("runtime_snapshot_failed", {"run_id": str(run_id), "error": type(exc).__name__})
            return CommandResult(
                CommandResultDisposition.APPLIED, tuple(event_ids), version,
                summary=summary,
            )
        except IdempotencyConflictError as exc:
            return self._rejected("IDEMPOTENCY_CONFLICT", str(exc), command)
        except ConcurrencyConflictError as exc:
            return CommandResult(
                CommandResultDisposition.REJECTED,
                diagnostics=(KernelDiagnostic(
                    "CONCURRENCY_CONFLICT", str(exc), command.aggregate_id,
                    exc.expected, exc.actual, True,
                ),),
            )
        except IntegrityViolationError as exc:
            return self._rejected("INTEGRITY_VIOLATION", str(exc), command)
        except PermissionError as exc:
            return self._rejected("POLICY_REJECTED", str(exc), command)
        except RepositoryAlreadyExistsError as exc:
            return self._rejected("ALREADY_EXISTS", str(exc), command)
        except LeaseAuthorityError as exc:
            return self._rejected("STALE_LEASE", str(exc), command)
        except PersistenceError as exc:
            return CommandResult(
                CommandResultDisposition.REJECTED,
                diagnostics=(KernelDiagnostic(
                    "PERSISTENCE_ERROR", str(exc), command.aggregate_id,
                    retryable=True, details={"error_type": type(exc).__name__},
                ),),
            )
        except (ValueError, KeyError) as exc:
            return self._rejected("VALIDATION_FAILED", str(exc), command)
        except Exception as exc:
            self._log("durable_command_internal_error", {"error_type": type(exc).__name__})
            return self._rejected("INTERNAL_ERROR", "durable command failed", command)

    @staticmethod
    def _durable_summary(command):
        if command.command_type == "claim_job":
            return {"job_id": str(command.aggregate_id), "lease_id": command.payload["lease_id"]}
        if command.command_type == "materialize_job":
            return {
                "node_run_id": str(command.aggregate_id),
                "job_id": str(derived_id("job", command.correlation_id, command.aggregate_id, "node_execution")),
            }
        if "timer" in command.command_type:
            return {"timer_id": str(command.aggregate_id)}
        if command.aggregate_id.kind == "lease":
            return {"lease_id": str(command.aggregate_id)}
        return {"job_id": str(command.aggregate_id)}

    @staticmethod
    def _semantic_timer_replay(uow, command):
        """Return the original Timer for a new Command with the same semantic key."""
        if command.command_type != "schedule_timer":
            return None
        purpose = TimerPurpose(str(command.payload["purpose"]))
        prior = uow.timers.get_by_dedupe(
            command.correlation_id, purpose.value,
            str(command.payload["dedupe_key"]),
        )
        if prior is None:
            return None
        if prior.timer_id != command.aggregate_id:
            raise ValueError("Timer ID does not match semantic dedupe identity")
        created = uow.events.read_stream(
            prior.timer_id, to_sequence=1, limit=1
        )
        if len(created) != 1 or created[0].envelope.event_type != "timer_created":
            raise IntegrityViolationError("Timer projection is missing its creation Event")
        return CommandResult(
            CommandResultDisposition.REPLAYED,
            (created[0].envelope.event_id,), prior.aggregate_version,
            summary={"timer_id": str(prior.timer_id)},
        )

    @staticmethod
    def _require_version(record, command):
        if record is None:
            raise ValueError(f"{command.aggregate_id} was not found")
        if record.aggregate_version != command.expected_version:
            raise ConcurrencyConflictError(
                command.aggregate_id, command.expected_version.value,
                record.aggregate_version.value,
            )

    @staticmethod
    def _lease_authority(uow, job, payload, now: datetime):
        lease_id = EntityId.parse(str(payload["lease_id"]))
        lease = uow.leases.get(lease_id)
        if (
            lease is None or lease.job_id != job.job_id
            or lease.status is not LeaseStatus.ACTIVE
            or lease.fencing_token.value != int(payload["fencing_token"])
            or lease.token_hash != token_hash(str(payload["lease_token"]))
            or lease.expires_at <= now
            or job.current_attempt_id != lease.attempt_id
        ):
            raise LeaseAuthorityError("lease credential or fencing token is stale")
        return lease

    def _durable_claim_job(self, uow, command, events):
        job = uow.jobs.get(command.aggregate_id)
        self._require_version(job, command)
        if job.status is not JobStatus.READY or job.available_at > command.issued_at:
            raise ValueError("Job is not claimable")
        run = uow.runs.get(job.run_id)
        node = uow.node_runs.get(job.node_run_id)
        if run is None or run.status is not WorkflowRunStatus.RUNNING:
            raise ValueError("Job Run is not running")
        if node is None or node.status not in {NodeRunStatus.READY, NodeRunStatus.WAITING}:
            raise ValueError("Job NodeRun is not ready or waiting")
        expires_at = datetime.fromisoformat(
            str(command.payload["lease_expires_at"]).replace("Z", "+00:00")
        )
        if not command.issued_at < expires_at <= command.issued_at + MAX_JOB_LEASE_TTL:
            raise ValueError("lease_expires_at exceeds the maximum Job lease TTL")
        number = max(
            (item.attempt_number.value for item in uow.attempts.list_by_node_run(node.node_run_id)),
            default=0,
        ) + 1
        if job.delivery_count >= job.max_delivery_attempts:
            raise ValueError("Job delivery attempts are exhausted")
        attempt_id = derived_id("attempt", node.node_run_id, number)
        attempt = AttemptRecord(
            attempt_id, node.node_run_id, Revision(number), AttemptStatus.CREATED,
            AggregateVersion(0), command.issued_at, command.issued_at,
        )
        uow.attempts.create(attempt)
        attempt_event = events.make(
            attempt_id, 1, "attempt_transitioned",
            _transition_payload(
                "attempt", AttemptStatus.CREATED, AttemptStatus.LEASED,
                run_id=str(run.run_id), node_run_id=str(node.node_run_id),
                attempt_number=number,
            ),
        )
        uow.events.append(run.run_id, attempt_id, AggregateVersion(0), (attempt_event,))
        uow.attempts.update(
            replace(attempt, status=AttemptStatus.LEASED, aggregate_version=AggregateVersion(1)),
            AggregateVersion(0),
        )
        fence = job.delivery_count + 1
        lease_id = EntityId.parse(str(command.payload["lease_id"]))
        lease = LeaseRecord(
            lease_id, job.job_id, attempt_id, str(command.payload["worker_id"]),
            str(command.payload["token_hash"]),
            SchemaVersion(str(command.payload["token_hash_version"])), Revision(fence),
            LeaseStatus.ACTIVE, command.issued_at, expires_at, None,
            AggregateVersion(0), 0,
        )
        uow.leases.create(lease)
        lease_event = events.make(
            lease_id, 1, "lease_created",
            {
                "job_id": str(job.job_id), "attempt_id": str(attempt_id),
                "worker_id": lease.worker_id, "fencing_token": fence,
                "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
            },
        )
        uow.events.append(run.run_id, lease_id, AggregateVersion(0), (lease_event,))
        uow.leases.update(replace(lease, aggregate_version=AggregateVersion(1)), AggregateVersion(0))
        transitioned = events.make(
            job.job_id, job.aggregate_version.value + 1, "job_transitioned",
            _transition_payload("job", JobStatus.READY, JobStatus.LEASED),
        )
        assigned = events.make(
            job.job_id, job.aggregate_version.value + 2, "job_attempt_assigned",
            {
                "attempt_id": str(attempt_id), "attempt_number": number,
                "lease_id": str(lease_id), "fencing_token": fence,
            },
        )
        uow.events.append(run.run_id, job.job_id, job.aggregate_version, (transitioned, assigned))
        new_version = AggregateVersion(job.aggregate_version.value + 2)
        uow.jobs.update(
            replace(
                job, current_attempt_id=attempt_id, status=JobStatus.LEASED,
                delivery_count=fence, aggregate_version=new_version,
                updated_at=command.issued_at,
            ),
            job.aggregate_version,
        )
        ids = [attempt_event.event_id, lease_event.event_id, transitioned.event_id, assigned.event_id]
        return ids, new_version, run.run_id, {
            "job_id": str(job.job_id), "lease_id": str(lease_id),
            "attempt_id": str(attempt_id), "fencing_token": fence,
        }

    def _durable_materialize_job(self, uow, command, events):
        if not command.actor.startswith("system:"):
            raise ValueError("MaterializeJob is system-only")
        node = uow.node_runs.get(command.aggregate_id)
        self._require_version(node, command)
        if node.status not in {NodeRunStatus.READY, NodeRunStatus.WAITING}:
            raise ValueError("MaterializeJob requires ready or waiting NodeRun")
        if any(
            item.node_run_id == node.node_run_id
            and item.status in {JobStatus.READY, JobStatus.LEASED, JobStatus.RUNNING, JobStatus.RETRY_WAIT}
            for item in uow.jobs.list_by_run(node.run_id)
        ):
            raise IntegrityViolationError("NodeRun already has active Job")
        ids = self.work_scheduler.create_for_node(uow, command, events, node)
        job_id = derived_id("job", node.run_id, node.node_run_id, "node_execution")
        return ids, node.aggregate_version, node.run_id, {"job_id": str(job_id), "node_run_id": str(node.node_run_id)}

    def _durable_start_job(self, uow, command, events):
        job = uow.jobs.get(command.aggregate_id)
        self._require_version(job, command)
        if job.status is not JobStatus.LEASED:
            raise ValueError("StartJob requires leased Job")
        lease = self._lease_authority(uow, job, command.payload, command.issued_at)
        attempt = uow.attempts.get(lease.attempt_id)
        node = uow.node_runs.get(job.node_run_id)
        if attempt.status is not AttemptStatus.LEASED or node.status not in {NodeRunStatus.READY, NodeRunStatus.WAITING}:
            raise ValueError("StartJob ownership state is invalid")
        job_event = events.make(job.job_id, job.aggregate_version.value + 1, "job_transitioned", _transition_payload("job", JobStatus.LEASED, JobStatus.RUNNING))
        attempt_event = events.make(attempt.attempt_id, attempt.aggregate_version.value + 1, "attempt_transitioned", _transition_payload("attempt", AttemptStatus.LEASED, AttemptStatus.RUNNING, run_id=str(job.run_id), node_run_id=str(node.node_run_id), attempt_number=attempt.attempt_number.value))
        node_event = events.make(node.node_run_id, node.aggregate_version.value + 1, "node_run_transitioned", _transition_payload("node_run", node.status, NodeRunStatus.RUNNING, run_id=str(job.run_id), node_id=node.node_id))
        uow.events.append(job.run_id, job.job_id, job.aggregate_version, (job_event,))
        uow.events.append(job.run_id, attempt.attempt_id, attempt.aggregate_version, (attempt_event,))
        uow.events.append(job.run_id, node.node_run_id, node.aggregate_version, (node_event,))
        new_version = job.aggregate_version.next()
        uow.jobs.update(replace(job, status=JobStatus.RUNNING, aggregate_version=new_version, updated_at=command.issued_at), job.aggregate_version)
        uow.attempts.update(replace(attempt, status=AttemptStatus.RUNNING, aggregate_version=attempt.aggregate_version.next(), updated_at=command.issued_at), attempt.aggregate_version)
        uow.node_runs.update(replace(node, status=NodeRunStatus.RUNNING, aggregate_version=node.aggregate_version.next(), updated_at=command.issued_at), node.aggregate_version)
        return [job_event.event_id, attempt_event.event_id, node_event.event_id], new_version, job.run_id, {"job_id": str(job.job_id), "status": "running"}

    def _finish_authority(self, uow, command):
        job = uow.jobs.get(command.aggregate_id)
        self._require_version(job, command)
        lease = self._lease_authority(uow, job, command.payload, command.issued_at)
        if job.status is not JobStatus.RUNNING:
            raise ValueError("Job is not running")
        attempt = uow.attempts.get(lease.attempt_id)
        return job, lease, attempt

    def _terminal_lease(
        self, uow, command, events, job, lease, target, *,
        job_event_fields=None, job_record_changes=None,
    ):
        lease_event = events.make(
            lease.lease_id, lease.aggregate_version.value + 1, "lease_transitioned",
            _transition_payload("lease", LeaseStatus.ACTIVE, LeaseStatus.RELEASED),
        )
        uow.events.append(job.run_id, lease.lease_id, lease.aggregate_version, (lease_event,))
        uow.leases.update(
            replace(lease, status=LeaseStatus.RELEASED, released_at=command.issued_at, aggregate_version=lease.aggregate_version.next()),
            lease.aggregate_version,
        )
        job_event = events.make(
            job.job_id, job.aggregate_version.value + 1, "job_transitioned",
            _transition_payload(
                "job", job.status, target, **(job_event_fields or {})
            ),
        )
        uow.events.append(job.run_id, job.job_id, job.aggregate_version, (job_event,))
        new_version = job.aggregate_version.next()
        uow.jobs.update(
            replace(
                job, status=target, aggregate_version=new_version,
                updated_at=command.issued_at, **(job_record_changes or {}),
            ),
            job.aggregate_version,
        )
        return lease_event, job_event, new_version

    def _record_final_usage(self, uow, command, events, attempt_id, run_id, metadata):
        if metadata is None:
            return []
        attempt = uow.attempts.get(attempt_id)
        payload = {
            "usage": metadata.get("usage"),
            "usage_incomplete": bool(metadata["usage_incomplete"]),
            "provider_request_id": metadata.get("provider_request_id"),
            "recorded_at": command.issued_at.isoformat().replace("+00:00", "Z"),
        }
        event = events.make(
            attempt.attempt_id, attempt.aggregate_version.value + 1,
            "attempt_usage_recorded", payload,
        )
        uow.events.append(run_id, attempt.attempt_id, attempt.aggregate_version, (event,))
        uow.attempts.update(
            replace(attempt, aggregate_version=attempt.aggregate_version.next()),
            attempt.aggregate_version,
        )
        return [event.event_id]

    def _durable_complete_job(self, uow, command, events):
        job, lease, attempt = self._finish_authority(uow, command)
        synthetic = CommandEnvelope(
            command.command_id, "complete_attempt", attempt.attempt_id,
            command.correlation_id, attempt.aggregate_version, command.idempotency_key,
            command.actor, command.issued_at, {
                "output": command.payload["output"],
                "artifact_refs": command.payload.get("artifact_refs", []),
            },
        )
        core_ids, _, _, _ = self._complete_attempt(uow, synthetic, events)
        usage_ids = self._record_final_usage(
            uow, command, events, attempt.attempt_id, job.run_id,
            command.payload.get("execution_metadata"),
        )
        lease_event, job_event, version = self._terminal_lease(
            uow, command, events, job, lease, JobStatus.COMPLETED
        )
        return [*core_ids, *usage_ids, lease_event.event_id, job_event.event_id], version, job.run_id, {"job_id": str(job.job_id), "status": "completed"}

    def _durable_fail_job(self, uow, command, events):
        job, lease, attempt = self._finish_authority(uow, command)
        synthetic = CommandEnvelope(
            command.command_id, "fail_attempt", attempt.attempt_id,
            command.correlation_id, attempt.aggregate_version, command.idempotency_key,
            command.actor, command.issued_at, {"error": command.payload["error"]},
        )
        core_ids, _, _, core_summary = self._fail_attempt(uow, synthetic, events)
        usage_ids = self._record_final_usage(
            uow, command, events, attempt.attempt_id, job.run_id,
            command.payload.get("execution_metadata"),
        )
        if core_summary.get("status") == "retry_wait":
            due = command.issued_at + timedelta(seconds=int(core_summary.get("backoff_seconds", 0)))
            lease_event, job_event, version = self._terminal_lease(
                uow, command, events, job, lease, JobStatus.RETRY_WAIT,
                job_event_fields={"available_at": due.isoformat().replace("+00:00", "Z")},
                job_record_changes={"available_at": due},
            )
            timer, timer_ids = self._make_timer(
                uow, command, events, run_id=job.run_id,
                purpose=TimerPurpose.JOB_BACKOFF,
                dedupe_key=f"{job.job_id}:policy:{attempt.attempt_number.value}",
                target_type="job", target_id=job.job_id,
                payload={"job_id": str(job.job_id)}, due_at=due,
            )
            return (
                [*core_ids, *usage_ids, lease_event.event_id, job_event.event_id, *timer_ids],
                version, job.run_id,
                {"job_id": str(job.job_id), "status": "retry_wait", "timer_id": str(timer.timer_id)},
            )
        lease_event, job_event, version = self._terminal_lease(
            uow, command, events, job, lease, JobStatus.FAILED
        )
        return [*core_ids, *usage_ids, lease_event.event_id, job_event.event_id], version, job.run_id, {"job_id": str(job.job_id), "status": "failed"}

    def _durable_report_unknown_job_result(self, uow, command, events):
        job, lease, attempt = self._finish_authority(uow, command)
        node = uow.node_runs.get(job.node_run_id)
        error = dict(command.payload["error"])
        info = ErrorInfo(
            error["code"], ErrorCategory(error["category"]), error["message"],
            error["source"], error["details"], error["cause"],
        )
        if info.category is not ErrorCategory.UNKNOWN_EXTERNAL_RESULT:
            raise ValueError("ReportUnknownJobResult requires unknown_external_result error")
        recorded = events.make(
            attempt.attempt_id, attempt.aggregate_version.value + 1,
            "attempt_failed_recorded",
            {"run_id": str(job.run_id), "node_run_id": str(node.node_run_id), "error": error},
        )
        transitioned = events.make(
            attempt.attempt_id, attempt.aggregate_version.value + 2,
            "attempt_transitioned",
            _transition_payload(
                "attempt", AttemptStatus.RUNNING,
                AttemptStatus.UNKNOWN_EXTERNAL_RESULT, run_id=str(job.run_id),
                node_run_id=str(node.node_run_id),
                attempt_number=attempt.attempt_number.value,
            ),
        )
        uow.events.append(
            job.run_id, attempt.attempt_id, attempt.aggregate_version,
            (recorded, transitioned),
        )
        uow.attempts.update(
            replace(
                attempt, status=AttemptStatus.UNKNOWN_EXTERNAL_RESULT,
                aggregate_version=AggregateVersion(attempt.aggregate_version.value + 2),
                updated_at=command.issued_at,
            ),
            attempt.aggregate_version,
        )
        node_event = events.make(
            node.node_run_id, node.aggregate_version.value + 1,
            "node_run_transitioned",
            _transition_payload(
                "node_run", NodeRunStatus.RUNNING, NodeRunStatus.WAITING,
                run_id=str(job.run_id), node_id=node.node_id,
            ),
        )
        uow.events.append(
            job.run_id, node.node_run_id, node.aggregate_version, (node_event,)
        )
        uow.node_runs.update(
            replace(
                node, status=NodeRunStatus.WAITING,
                aggregate_version=node.aggregate_version.next(),
                updated_at=command.issued_at,
            ),
            node.aggregate_version,
        )
        usage_ids = self._record_final_usage(
            uow, command, events, attempt.attempt_id, job.run_id,
            command.payload["execution_metadata"],
        )
        lease_event, job_event, version = self._terminal_lease(
            uow, command, events, job, lease, JobStatus.FAILED
        )
        return (
            [recorded.event_id, transitioned.event_id, node_event.event_id,
             *usage_ids, lease_event.event_id, job_event.event_id],
            version, job.run_id,
            {"job_id": str(job.job_id), "status": "unknown_external_result"},
        )

    def _durable_release_job(self, uow, command, events):
        job = uow.jobs.get(command.aggregate_id)
        self._require_version(job, command)
        if job.status is not JobStatus.LEASED:
            raise ValueError("ReleaseJob requires leased Job")
        lease = self._lease_authority(uow, job, command.payload, command.issued_at)
        attempt = uow.attempts.get(lease.attempt_id)
        attempt_event = events.make(attempt.attempt_id, attempt.aggregate_version.value + 1, "attempt_transitioned", _transition_payload("attempt", AttemptStatus.LEASED, AttemptStatus.LOST, run_id=str(job.run_id), node_run_id=str(job.node_run_id), attempt_number=attempt.attempt_number.value))
        uow.events.append(job.run_id, attempt.attempt_id, attempt.aggregate_version, (attempt_event,))
        uow.attempts.update(replace(attempt, status=AttemptStatus.LOST, aggregate_version=attempt.aggregate_version.next(), updated_at=command.issued_at), attempt.aggregate_version)
        lease_event, job_event, version = self._terminal_lease(uow, command, events, job, lease, JobStatus.READY)
        return [attempt_event.event_id, lease_event.event_id, job_event.event_id], version, job.run_id, {"job_id": str(job.job_id), "status": "ready"}

    def _make_timer(self, uow, command, events, *, run_id, purpose, dedupe_key, target_type, target_id, payload, due_at):
        prior = uow.timers.get_by_dedupe(run_id, purpose.value, dedupe_key)
        if prior is not None:
            return prior, []
        timer_id = derived_id("timer", run_id, purpose.value, dedupe_key)
        timer = DurableTimerRecord(
            timer_id, run_id, purpose, dedupe_key, target_type, target_id,
            SchemaVersion("1.0"), payload, TimerStatus.SCHEDULED, due_at, None,
            None, None, 0, None, AggregateVersion(0), command.issued_at,
            command.issued_at,
        )
        uow.timers.create(timer)
        event = events.make(timer_id, 1, "timer_created", {
            "run_id": str(run_id), "purpose": purpose.value,
            "dedupe_key": dedupe_key, "target_type": target_type,
            "target_id": str(target_id), "due_at": due_at.isoformat().replace("+00:00", "Z"),
        })
        uow.events.append(run_id, timer_id, AggregateVersion(0), (event,))
        timer = replace(timer, aggregate_version=AggregateVersion(1))
        uow.timers.update(timer, AggregateVersion(0))
        return timer, [event.event_id]

    def _durable_defer_job(self, uow, command, events):
        job, lease, attempt = self._finish_authority(uow, command)
        if job.execution_safety is not ExecutionSafety.REPLAY_SAFE:
            raise ValueError("only replay-safe Job can be deferred")
        due = datetime.fromisoformat(str(command.payload["available_at"]).replace("Z", "+00:00"))
        node = uow.node_runs.get(job.node_run_id)
        attempt_event = events.make(attempt.attempt_id, attempt.aggregate_version.value + 1, "attempt_transitioned", _transition_payload("attempt", AttemptStatus.RUNNING, AttemptStatus.LOST, run_id=str(job.run_id), node_run_id=str(node.node_run_id), attempt_number=attempt.attempt_number.value))
        node_event = events.make(node.node_run_id, node.aggregate_version.value + 1, "node_run_transitioned", _transition_payload("node_run", NodeRunStatus.RUNNING, NodeRunStatus.WAITING, run_id=str(job.run_id), node_id=node.node_id))
        uow.events.append(job.run_id, attempt.attempt_id, attempt.aggregate_version, (attempt_event,))
        uow.events.append(job.run_id, node.node_run_id, node.aggregate_version, (node_event,))
        uow.attempts.update(replace(attempt, status=AttemptStatus.LOST, aggregate_version=attempt.aggregate_version.next()), attempt.aggregate_version)
        uow.node_runs.update(replace(node, status=NodeRunStatus.WAITING, aggregate_version=node.aggregate_version.next()), node.aggregate_version)
        lease_event, job_event, version = self._terminal_lease(
            uow, command, events, job, lease, JobStatus.RETRY_WAIT,
            job_event_fields={
                "available_at": due.isoformat().replace("+00:00", "Z")
            },
            job_record_changes={"available_at": due},
        )
        timer, timer_ids = self._make_timer(
            uow, command, events, run_id=job.run_id, purpose=TimerPurpose.JOB_BACKOFF,
            dedupe_key=f"{job.job_id}:delivery:{job.delivery_count}", target_type="job",
            target_id=job.job_id, payload={"job_id": str(job.job_id)}, due_at=due,
        )
        return [attempt_event.event_id, node_event.event_id, lease_event.event_id, job_event.event_id, *timer_ids], version, job.run_id, {"job_id": str(job.job_id), "timer_id": str(timer.timer_id)}

    def _durable_expire_lease(self, uow, command, events):
        lease = uow.leases.get(command.aggregate_id)
        self._require_version(lease, command)
        if lease.status is not LeaseStatus.ACTIVE or lease.fencing_token.value != int(command.payload["fencing_token"]):
            raise LeaseAuthorityError("lease credential or fencing token is stale")
        observed = datetime.fromisoformat(str(command.payload["observed_at"]).replace("Z", "+00:00"))
        if lease.expires_at > observed:
            raise ValueError("Lease has not expired")
        job = uow.jobs.get(lease.job_id)
        attempt = uow.attempts.get(lease.attempt_id)
        node = uow.node_runs.get(job.node_run_id)
        started = job.status is JobStatus.RUNNING
        attempt_target = (
            AttemptStatus.UNKNOWN_EXTERNAL_RESULT
            if started and job.execution_safety is ExecutionSafety.UNKNOWN_ON_LEASE_LOSS
            else AttemptStatus.LOST
        )
        job_target = JobStatus.READY
        if started:
            job_target = JobStatus.FAILED if attempt_target is AttemptStatus.UNKNOWN_EXTERNAL_RESULT else JobStatus.RETRY_WAIT
        attempt_event = events.make(attempt.attempt_id, attempt.aggregate_version.value + 1, "attempt_transitioned", _transition_payload("attempt", attempt.status, attempt_target, run_id=str(job.run_id), node_run_id=str(node.node_run_id), attempt_number=attempt.attempt_number.value))
        lease_event = events.make(lease.lease_id, lease.aggregate_version.value + 1, "lease_transitioned", _transition_payload("lease", LeaseStatus.ACTIVE, LeaseStatus.EXPIRED))
        job_event = events.make(job.job_id, job.aggregate_version.value + 1, "job_transitioned", _transition_payload("job", job.status, job_target))
        uow.events.append(job.run_id, attempt.attempt_id, attempt.aggregate_version, (attempt_event,))
        uow.events.append(job.run_id, lease.lease_id, lease.aggregate_version, (lease_event,))
        uow.events.append(job.run_id, job.job_id, job.aggregate_version, (job_event,))
        uow.attempts.update(replace(attempt, status=attempt_target, aggregate_version=attempt.aggregate_version.next(), updated_at=observed), attempt.aggregate_version)
        new_lease_version = lease.aggregate_version.next()
        uow.leases.update(replace(lease, status=LeaseStatus.EXPIRED, released_at=observed, aggregate_version=new_lease_version), lease.aggregate_version)
        uow.jobs.update(replace(job, status=job_target, aggregate_version=job.aggregate_version.next(), updated_at=observed), job.aggregate_version)
        ids = [attempt_event.event_id, lease_event.event_id, job_event.event_id]
        if started:
            node_event = events.make(node.node_run_id, node.aggregate_version.value + 1, "node_run_transitioned", _transition_payload("node_run", NodeRunStatus.RUNNING, NodeRunStatus.WAITING, run_id=str(job.run_id), node_id=node.node_id))
            uow.events.append(job.run_id, node.node_run_id, node.aggregate_version, (node_event,))
            uow.node_runs.update(replace(node, status=NodeRunStatus.WAITING, aggregate_version=node.aggregate_version.next(), updated_at=observed), node.aggregate_version)
            ids.append(node_event.event_id)
        if started and job_target is JobStatus.RETRY_WAIT:
            timer, timer_ids = self._make_timer(
                uow, command, events, run_id=job.run_id,
                purpose=TimerPurpose.JOB_BACKOFF,
                dedupe_key=f"{job.job_id}:lease-expiry:{job.delivery_count}",
                target_type="job", target_id=job.job_id,
                payload={"job_id": str(job.job_id)},
                due_at=observed + timedelta(seconds=1),
            )
            ids.extend(timer_ids)
        return ids, new_lease_version, job.run_id, {"lease_id": str(lease.lease_id), "outcome": attempt_target.value}

    def _durable_schedule_timer(self, uow, command, events):
        if command.aggregate_id.kind != "timer" or command.expected_version != AggregateVersion(0):
            raise ValueError("ScheduleTimer requires new timer aggregate")
        run_id = command.correlation_id
        due = datetime.fromisoformat(str(command.payload["due_at"]).replace("Z", "+00:00"))
        timer, ids = self._make_timer(
            uow, command, events, run_id=run_id,
            purpose=TimerPurpose(str(command.payload["purpose"])),
            dedupe_key=str(command.payload["dedupe_key"]),
            target_type=str(command.payload["target_type"]),
            target_id=EntityId.parse(str(command.payload["target_id"])),
            payload=dict(command.payload["payload"]), due_at=due,
        )
        if timer.timer_id != command.aggregate_id:
            raise ValueError("Timer ID does not match deterministic identity")
        return ids, timer.aggregate_version, run_id, {"timer_id": str(timer.timer_id)}

    def _durable_claim_timer(self, uow, command, events):
        timer = uow.timers.get(command.aggregate_id)
        self._require_version(timer, command)
        observed = datetime.fromisoformat(str(command.payload["observed_at"]).replace("Z", "+00:00"))
        if timer.status is not TimerStatus.SCHEDULED or timer.due_at > observed:
            raise ValueError("Timer is not due")
        expires = datetime.fromisoformat(str(command.payload["lease_expires_at"]).replace("Z", "+00:00"))
        event = events.make(timer.timer_id, timer.aggregate_version.value + 1, "timer_transitioned", _transition_payload("timer", TimerStatus.SCHEDULED, TimerStatus.LEASED))
        uow.events.append(timer.run_id, timer.timer_id, timer.aggregate_version, (event,))
        version = timer.aggregate_version.next()
        uow.timers.update(replace(timer, status=TimerStatus.LEASED, lease_owner=str(command.payload["worker_id"]), lease_token_hash=str(command.payload["token_hash"]), lease_fencing_token=timer.lease_fencing_token + 1, lease_expires_at=expires, aggregate_version=version, updated_at=observed), timer.aggregate_version)
        return [event.event_id], version, timer.run_id, {"timer_id": str(timer.timer_id), "fencing_token": timer.lease_fencing_token + 1}

    @staticmethod
    def _timer_authority(timer, payload, now):
        if timer.status is not TimerStatus.LEASED or timer.lease_token_hash != token_hash(str(payload["lease_token"])) or timer.lease_fencing_token != int(payload["fencing_token"]) or timer.lease_expires_at <= now:
            raise LeaseAuthorityError("timer lease credential or fencing token is stale")

    def _durable_fire_timer(self, uow, command, events):
        timer = uow.timers.get(command.aggregate_id)
        self._require_version(timer, command)
        self._timer_authority(timer, command.payload, command.issued_at)
        outcome = "obsolete"
        ids = []
        if timer.purpose is TimerPurpose.JOB_BACKOFF:
            job = uow.jobs.get(timer.target_id)
            if job is not None and job.status is JobStatus.RETRY_WAIT:
                event = events.make(job.job_id, job.aggregate_version.value + 1, "job_transitioned", _transition_payload("job", JobStatus.RETRY_WAIT, JobStatus.READY))
                uow.events.append(timer.run_id, job.job_id, job.aggregate_version, (event,))
                uow.jobs.update(replace(job, status=JobStatus.READY, aggregate_version=job.aggregate_version.next(), updated_at=command.issued_at), job.aggregate_version)
                ids.append(event.event_id)
                outcome = "applied"
        elif timer.purpose is TimerPurpose.NODE_TIMEOUT:
            job = uow.jobs.get(timer.target_id)
            lease = None if job is None else uow.leases.get_active_for_job(job.job_id)
            if job is not None and job.status is JobStatus.RUNNING and lease is not None:
                attempt = uow.attempts.get(lease.attempt_id)
                synthetic = CommandEnvelope(
                    command.command_id, "fail_attempt", attempt.attempt_id,
                    command.correlation_id, attempt.aggregate_version,
                    command.idempotency_key, command.actor, command.issued_at,
                    {"error": {
                        "code": "attempt_timeout", "category": "timeout",
                        "message": "node execution deadline expired",
                        "source": "durable_timer", "details": {}, "cause": None,
                    }},
                )
                core_ids, _, _, core_summary = self._fail_attempt(uow, synthetic, events)
                retrying = core_summary.get("status") == "retry_wait"
                due = command.issued_at + timedelta(seconds=int(core_summary.get("backoff_seconds", 0)))
                lease_event, job_event, _ = self._terminal_lease(
                    uow, command, events, job, lease,
                    JobStatus.RETRY_WAIT if retrying else JobStatus.FAILED,
                    job_event_fields=({"available_at": due.isoformat().replace("+00:00", "Z")} if retrying else None),
                    job_record_changes=({"available_at": due} if retrying else None),
                )
                ids.extend([*core_ids, lease_event.event_id, job_event.event_id])
                if retrying:
                    _, retry_timer_ids = self._make_timer(
                        uow, command, events, run_id=job.run_id,
                        purpose=TimerPurpose.JOB_BACKOFF,
                        dedupe_key=f"{job.job_id}:timeout:{attempt.attempt_number.value}",
                        target_type="job", target_id=job.job_id,
                        payload={"job_id": str(job.job_id)}, due_at=due,
                    )
                    ids.extend(retry_timer_ids)
                outcome = "applied"
        elif timer.purpose is TimerPurpose.LEASE_RECOVERY:
            lease = uow.leases.get(timer.target_id)
            if lease is not None and lease.status is LeaseStatus.ACTIVE and lease.expires_at <= command.issued_at:
                synthetic = CommandEnvelope(
                    command.command_id, "expire_lease", lease.lease_id,
                    command.correlation_id, lease.aggregate_version,
                    command.idempotency_key, command.actor, command.issued_at,
                    {
                        "observed_at": command.issued_at.isoformat().replace("+00:00", "Z"),
                        "fencing_token": lease.fencing_token.value,
                    },
                )
                expiry_ids, _, _, _ = self._durable_expire_lease(
                    uow, synthetic, events
                )
                ids.extend(expiry_ids)
                outcome = "applied"
        elif timer.purpose is TimerPurpose.JOIN_DEADLINE:
            group = uow.joins.get(timer.target_id)
            if group is not None and group.status.value == "waiting":
                plan_record = uow.plans.get(timer.run_id, Revision(1))
                plan = self._load_plan(uow, timer.run_id, plan_record.plan_version.value)
                ids.extend(self._consider_join(
                    uow, command, events, plan, group.node_id,
                    deadline_fired=True,
                ))
                outcome = "applied"
        transitioned = events.make(timer.timer_id, timer.aggregate_version.value + 1, "timer_transitioned", _transition_payload("timer", TimerStatus.LEASED, TimerStatus.FIRED))
        fired = events.make(timer.timer_id, timer.aggregate_version.value + 2, "timer_fired", {"fired_at": command.issued_at.isoformat().replace("+00:00", "Z"), "outcome": outcome})
        uow.events.append(timer.run_id, timer.timer_id, timer.aggregate_version, (transitioned, fired))
        version = AggregateVersion(timer.aggregate_version.value + 2)
        uow.timers.update(replace(timer, status=TimerStatus.FIRED, fired_at=command.issued_at, lease_owner=None, lease_token_hash=None, lease_expires_at=None, aggregate_version=version, updated_at=command.issued_at), timer.aggregate_version)
        return [*ids, transitioned.event_id, fired.event_id], version, timer.run_id, {"timer_id": str(timer.timer_id), "outcome": outcome}

    def _durable_expire_timer_lease(self, uow, command, events):
        timer = uow.timers.get(command.aggregate_id)
        self._require_version(timer, command)
        observed = datetime.fromisoformat(str(command.payload["observed_at"]).replace("Z", "+00:00"))
        if timer.status is not TimerStatus.LEASED or timer.lease_fencing_token != int(command.payload["fencing_token"]) or timer.lease_expires_at > observed:
            raise ValueError("Timer lease is not expired")
        event = events.make(timer.timer_id, timer.aggregate_version.value + 1, "timer_transitioned", _transition_payload("timer", TimerStatus.LEASED, TimerStatus.SCHEDULED))
        uow.events.append(timer.run_id, timer.timer_id, timer.aggregate_version, (event,))
        version = timer.aggregate_version.next()
        uow.timers.update(replace(timer, status=TimerStatus.SCHEDULED, lease_owner=None, lease_token_hash=None, lease_expires_at=None, aggregate_version=version, updated_at=observed), timer.aggregate_version)
        return [event.event_id], version, timer.run_id, {"timer_id": str(timer.timer_id), "status": "scheduled"}

    def _durable_cancel_timer(self, uow, command, events):
        timer = uow.timers.get(command.aggregate_id)
        self._require_version(timer, command)
        if timer.status not in {TimerStatus.SCHEDULED, TimerStatus.LEASED}:
            raise ValueError("Timer is already terminal")
        event = events.make(timer.timer_id, timer.aggregate_version.value + 1, "timer_transitioned", _transition_payload("timer", timer.status, TimerStatus.CANCELLED))
        uow.events.append(timer.run_id, timer.timer_id, timer.aggregate_version, (event,))
        version = timer.aggregate_version.next()
        uow.timers.update(replace(timer, status=TimerStatus.CANCELLED, lease_owner=None, lease_token_hash=None, lease_expires_at=None, aggregate_version=version, updated_at=command.issued_at), timer.aggregate_version)
        return [event.event_id], version, timer.run_id, {"timer_id": str(timer.timer_id), "status": "cancelled"}

    def _durable_cancel_job(self, uow, command, events):
        """Converge a Job after its Attempt was already terminalized elsewhere.

        Run cancellation owns Attempt transitions. This system-level command only
        closes the remaining Job/Lease projection and must not be used as a
        standalone substitute for CancelRun on leased or running work.
        """
        job = uow.jobs.get(command.aggregate_id)
        self._require_version(job, command)
        if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            raise ValueError("Job is already terminal")
        ids = []
        lease = uow.leases.get_active_for_job(job.job_id)
        if lease is not None:
            lease_event = events.make(lease.lease_id, lease.aggregate_version.value + 1, "lease_transitioned", _transition_payload("lease", LeaseStatus.ACTIVE, LeaseStatus.RELEASED))
            uow.events.append(job.run_id, lease.lease_id, lease.aggregate_version, (lease_event,))
            uow.leases.update(replace(lease, status=LeaseStatus.RELEASED, released_at=command.issued_at, aggregate_version=lease.aggregate_version.next()), lease.aggregate_version)
            ids.append(lease_event.event_id)
        event = events.make(job.job_id, job.aggregate_version.value + 1, "job_transitioned", _transition_payload("job", job.status, JobStatus.CANCELLED))
        uow.events.append(job.run_id, job.job_id, job.aggregate_version, (event,))
        version = job.aggregate_version.next()
        uow.jobs.update(replace(job, status=JobStatus.CANCELLED, aggregate_version=version, updated_at=command.issued_at), job.aggregate_version)
        ids.append(event.event_id)
        return ids, version, job.run_id, {"job_id": str(job.job_id), "status": "cancelled"}
