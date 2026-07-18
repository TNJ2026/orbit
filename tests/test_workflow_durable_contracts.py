from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from orbit.workflow.domain.durable_execution import (
    DURABLE_COMMAND_TYPES,
    DURABLE_EVENT_VERSIONS,
    DurableTimerRecord,
    ExecutionSafety,
    JobRecord,
    LeaseRecord,
    TimerPurpose,
)
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.schemas import SchemaValidationError, validate_contract
from orbit.workflow.domain.stability import CONTRACT_STABILITY, ContractStability
from orbit.workflow.domain.states import JobStatus, LeaseStatus, TimerStatus
from orbit.workflow.domain.versions import AggregateVersion, Revision, SchemaVersion


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


class DurableExecutionContractTests(unittest.TestCase):
    def test_job_lease_and_timer_records_enforce_identity_and_time(self) -> None:
        job = JobRecord(
            EntityId("job", "j1"), EntityId("run", "r1"),
            EntityId("node_run", "n1"), None, "node_execution",
            ExecutionSafety.REPLAY_SAFE, JobStatus.READY, 0, NOW, 0, 3,
            AggregateVersion(0), NOW, NOW,
        )
        self.assertEqual(JobStatus.READY, job.status)
        lease = LeaseRecord(
            EntityId("lease", "l1"), job.job_id, EntityId("attempt", "a1"),
            "worker-1", "sha256:secret", SchemaVersion("1.0"), Revision(1),
            LeaseStatus.ACTIVE, NOW, NOW + timedelta(seconds=30), None,
            AggregateVersion(0), 0,
        )
        self.assertEqual(1, lease.fencing_token.value)
        timer = DurableTimerRecord(
            EntityId("timer", "t1"), job.run_id, TimerPurpose.JOB_BACKOFF,
            "job:j1:retry:1", "job", job.job_id, SchemaVersion("1.0"),
            {"job_id": str(job.job_id)}, TimerStatus.SCHEDULED,
            NOW + timedelta(seconds=10), None, None, None, 0, None,
            AggregateVersion(0), NOW, NOW,
        )
        with self.assertRaises(TypeError):
            timer.payload["changed"] = True

    def test_invalid_lease_and_timer_metadata_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "terminal lease requires"):
            LeaseRecord(
                EntityId("lease", "l1"), EntityId("job", "j1"),
                EntityId("attempt", "a1"), "worker", "hash",
                SchemaVersion("1.0"), Revision(1), LeaseStatus.EXPIRED,
                NOW, NOW + timedelta(seconds=1), None, AggregateVersion(1), 0,
            )
        with self.assertRaisesRegex(ValueError, "leased timer requires"):
            DurableTimerRecord(
                EntityId("timer", "t1"), EntityId("run", "r1"),
                TimerPurpose.NODE_TIMEOUT, "timeout", "node_run",
                EntityId("node_run", "n1"), SchemaVersion("1.0"), {},
                TimerStatus.LEASED, NOW, None, None, None, 1, None,
                AggregateVersion(1), NOW, NOW,
            )

    def test_command_and_event_catalogs_are_complete_and_stable(self) -> None:
        self.assertEqual(15, len(DURABLE_COMMAND_TYPES))
        self.assertEqual(9, len(DURABLE_EVENT_VERSIONS))
        for name in (
            "durable_execution_records", "durable_execution_ports",
            "durable_commands", "durable_events",
        ):
            self.assertIs(ContractStability.STABLE, CONTRACT_STABILITY[name])

    def test_durable_payload_schema_reports_exact_path(self) -> None:
        payload = {
            "worker_id": "worker-1", "lease_id": "lease:l1",
            "token_hash": "hash", "token_hash_version": "1.0",
            "lease_expires_at": "2026-07-17T00:00:30Z",
            "observed_at": "2026-07-17T00:00:00Z",
        }
        validate_contract(payload, "durable-command/claim-job/1.0")
        with self.assertRaises(SchemaValidationError) as caught:
            validate_contract(
                {**payload, "lease_expires_at": "not-a-date"},
                "durable-command/claim-job/1.0",
            )
        self.assertEqual("$.lease_expires_at", caught.exception.json_path)
        validate_contract(
            {
                "run_id": "run:r1", "node_run_id": "node_run:n1",
                "job_kind": "node_execution", "execution_safety": "replay_safe",
            },
            "durable-event/job-created/1.0",
        )


if __name__ == "__main__":
    unittest.main()
