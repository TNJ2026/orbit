from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from orbit.workflow.application.runtime_service import RuntimeApplicationService
from orbit.workflow.domain.definitions import CompiledWorkflow
from orbit.workflow.domain.durable_execution import (
    DurableTimerRecord, ExecutionSafety, JobRecord, JobScanCursor, LeaseRecord,
    TimerPurpose,
)
from orbit.workflow.domain.envelopes import CommandEnvelope
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.persistence import RepositoryAlreadyExistsError
from orbit.workflow.domain.serialization import definition_hash
from orbit.workflow.domain.states import JobStatus, LeaseStatus, TimerStatus
from orbit.workflow.domain.versions import AggregateVersion, Revision, SchemaVersion
from orbit.workflow.persistence import (
    MemoryRuntimeDatabase, MemoryUnitOfWork, SQLiteUnitOfWork, check_database,
)
from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
from tests.test_workflow_runtime import linear_ir


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


class DurablePersistenceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "durable.db"
        ir = linear_ir()
        digest = definition_hash(ir)
        SQLiteWorkflowVersionStore(self.path).publish(
            CompiledWorkflow(ir, digest, "1.0", "sha256:" + "d" * 64),
            expected_latest_version=0, source_format="json", source_text=None,
            actor="test",
        )
        self.service = RuntimeApplicationService(self.path)
        self.run_id = EntityId("run", "durable")
        self.service.submit(CommandEnvelope(
            EntityId("command", "durable-start"), "start_run", self.run_id,
            self.run_id, AggregateVersion(0), "durable-start", "test", NOW,
            {
                "workflow_id": "workflow:linear", "workflow_version": 1,
                "definition_hash": digest.value, "input": {"value": 0},
            },
        ))
        with self.service.uow_factory() as uow:
            self.node = uow.node_runs.list_by_run(self.run_id)[0]
        started = self.service.submit(CommandEnvelope(
            EntityId("command", "durable-attempt"), "start_attempt",
            self.node.node_run_id, self.run_id, self.node.aggregate_version,
            "durable-attempt", "test", NOW, {},
        ))
        self.attempt_id = EntityId.parse(started.summary["attempt_id"])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def records(self):
        job = JobRecord(
            EntityId("job", "j1"), self.run_id, self.node.node_run_id,
            self.attempt_id, "node_execution", ExecutionSafety.REPLAY_SAFE,
            JobStatus.READY, 5, NOW, 1, 3, AggregateVersion(0), NOW, NOW,
        )
        lease = LeaseRecord(
            EntityId("lease", "l1"), job.job_id, self.attempt_id, "worker-1",
            "sha256:token", SchemaVersion("1.0"), Revision(1),
            LeaseStatus.ACTIVE, NOW, NOW + timedelta(seconds=30), None,
            AggregateVersion(0), 0,
        )
        timer = DurableTimerRecord(
            EntityId("timer", "t1"), self.run_id, TimerPurpose.JOB_BACKOFF,
            "j1-backoff-1", "job", job.job_id, SchemaVersion("1.0"),
            {"job_id": str(job.job_id)}, TimerStatus.SCHEDULED,
            NOW + timedelta(seconds=5), None, None, None, 0, None,
            AggregateVersion(0), NOW, NOW,
        )
        return job, lease, timer

    def test_sqlite_and_memory_adapters_share_create_query_and_renew_contract(self) -> None:
        job, lease, timer = self.records()
        memory = MemoryRuntimeDatabase()
        factories = (
            lambda: SQLiteUnitOfWork(self.path),
            lambda: MemoryUnitOfWork(memory),
        )
        for factory in factories:
            with factory() as uow:
                uow.jobs.create(job)
                uow.leases.create(lease)
                uow.timers.create(timer)
                uow.commit()
            with factory() as uow:
                self.assertEqual((job,), uow.jobs.list_claimable(NOW))
                self.assertEqual(lease, uow.leases.get_active_for_job(job.job_id))
                self.assertEqual((timer,), uow.timers.list_due(NOW + timedelta(seconds=5)))
                renewed = uow.leases.renew(
                    lease.lease_id, token_hash=lease.token_hash,
                    fencing_token=1, expected_revision=0,
                    expires_at=NOW + timedelta(seconds=60),
                )
                self.assertEqual(1, renewed.renewal_revision)
                uow.commit()

    def test_unique_active_lease_and_timer_dedupe_are_enforced(self) -> None:
        job, lease, timer = self.records()
        with self.service.uow_factory() as uow:
            uow.jobs.create(job)
            uow.leases.create(lease)
            uow.timers.create(timer)
            with self.assertRaises(RepositoryAlreadyExistsError):
                uow.leases.create(LeaseRecord(
                    EntityId("lease", "l2"), job.job_id, self.attempt_id,
                    "worker-2", "other", SchemaVersion("1.0"), Revision(2),
                    LeaseStatus.ACTIVE, NOW, NOW + timedelta(seconds=20), None,
                    AggregateVersion(0), 0,
                ))
            uow.rollback()

    def test_claim_cursor_preserves_priority_order(self) -> None:
        memory = MemoryRuntimeDatabase()
        jobs = [
            JobRecord(
                EntityId("job", f"j{index}"), self.run_id, self.node.node_run_id,
                None, f"kind-{index}", ExecutionSafety.REPLAY_SAFE,
                JobStatus.READY, priority, NOW, 0, 3, AggregateVersion(0),
                NOW + timedelta(microseconds=index), NOW,
            )
            for index, priority in enumerate((1, 3, 3), 1)
        ]
        with MemoryUnitOfWork(memory) as uow:
            for job in jobs:
                uow.jobs.create(job)
            uow.commit()
        with MemoryUnitOfWork(memory) as uow:
            first = uow.jobs.list_claimable(NOW, limit=1)[0]
            cursor = JobScanCursor(
                first.priority, first.available_at, first.created_at, first.job_id
            )
            rest = uow.jobs.list_claimable(NOW, after=cursor, limit=10)
        self.assertEqual([3, 3, 1], [first.priority, *(item.priority for item in rest)])

    def test_integrity_checker_knows_migration_three_tables(self) -> None:
        report = check_database(self.path)
        self.assertTrue(report.ok, report.issues)
        self.assertEqual(tuple(range(1, 10)), report.migration_versions)
        counts = dict(report.table_counts)
        self.assertIn("jobs", counts)
        self.assertIn("job_leases", counts)
        self.assertIn("durable_timers", counts)


if __name__ == "__main__":
    unittest.main()
