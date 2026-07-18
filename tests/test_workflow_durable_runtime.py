from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import tempfile
import threading
import unittest

from orbit.workflow.application.durable_runtime_service import DurableRuntimeApplicationService
from orbit.workflow.application.runtime_service import RuntimeApplicationService
from orbit.workflow.domain.definitions import CompiledWorkflow
from orbit.workflow.domain.envelopes import CommandEnvelope
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.runtime import CommandResultDisposition
from orbit.workflow.domain.serialization import definition_hash
from orbit.workflow.domain.states import JobStatus, LeaseStatus, TimerStatus, WorkflowRunStatus
from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.versions import AggregateVersion
from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
from orbit.workflow.persistence.memory import MemoryRuntimeDatabase, MemoryUnitOfWork
from orbit.workflow.runtime.durable_kernel import DurableRuntimeKernel, token_hash
from orbit.workflow.runtime.reducers import reduce_run_view
from orbit.workflow.runtime.work_scheduler import DurableWorkScheduler
from orbit.workflow.worker.runtime import TimerDispatcher, WorkerRuntime
from tests.test_workflow_runtime import linear_ir


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


class DurableRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "durable-runtime.db"
        ir = linear_ir()
        self.digest = definition_hash(ir)
        SQLiteWorkflowVersionStore(self.path).publish(
            CompiledWorkflow(ir, self.digest, "1.0", "sha256:" + "e" * 64),
            expected_latest_version=0, source_format="json", source_text=None,
            actor="test",
        )
        self.service = DurableRuntimeApplicationService(self.path)
        self.run_id = EntityId("run", "durable-e2e")
        self.start = CommandEnvelope(
            EntityId("command", "durable-e2e-start"), "start_run", self.run_id,
            self.run_id, AggregateVersion(0), "durable-e2e-start", "test", NOW,
            {
                "workflow_id": "workflow:linear", "workflow_version": 1,
                "definition_hash": self.digest.value, "input": {"value": 0},
            },
        )

    def tearDown(self): self.temp.cleanup()

    def test_worker_executes_three_node_durable_timeline(self):
        result = self.service.submit(self.start)
        self.assertEqual(CommandResultDisposition.APPLIED, result.disposition)
        self.assertEqual(1, len(self.service.list_jobs(self.run_id)))
        worker = WorkerRuntime(
            self.service,
            lambda node, value, token: {
                "value": value["value"] + 1 if node == "collect"
                else value["value"] * 2 if node == "transform"
                else value["value"] + 3
            },
            clock=lambda: NOW,
        )
        for _ in range(3): self.assertTrue(worker.run_once())
        self.assertEqual(WorkflowRunStatus.SUCCEEDED, self.service.get_run(self.run_id).status)
        jobs = self.service.list_jobs(self.run_id)
        self.assertEqual(3, len(jobs))
        self.assertTrue(all(item.status is JobStatus.COMPLETED for item in jobs))
        with self.service.uow_factory() as uow:
            leases = [uow.leases.get_active_for_job(job.job_id) for job in jobs]
            self.assertEqual([None, None, None], leases)
        report = self.service.recovery.rehydrate(self.run_id)
        self.assertEqual(3, len(report.state["jobs"]))
        self.assertEqual(3, len(report.state["leases"]))
        self.assertEqual((), self.service.recovery.verify_projection(self.run_id))
        golden = json.loads(
            (Path(__file__).parent / "fixtures/workflow_durable/v1/three-node.json").read_text()
        )
        timeline = self.service.get_timeline(self.run_id)
        self.assertEqual(golden["event_count"], len(timeline))
        self.assertEqual(golden["event_types"], [item.envelope.event_type for item in timeline])

    def test_stale_result_is_rejected_after_release(self):
        self.service.submit(self.start)
        claimed = self.service.claim_job("w1", NOW)
        self.assertIsNotNone(claimed)
        self.assertEqual("applied", self.service.release_job(claimed, NOW).disposition.value)
        result = self.service.complete_job(claimed, NOW, {"value": 1})
        self.assertEqual("STALE_LEASE", result.diagnostics[0].code)
        with self.service.uow_factory() as uow:
            lease = uow.leases.get(claimed.lease_id)
            self.assertIs(LeaseStatus.RELEASED, lease.status)

    def test_defer_uses_durable_timer_and_requeues_job(self):
        self.service.submit(self.start)
        claimed = self.service.claim_job("w1", NOW)
        self.service.start_job(claimed, NOW)
        due = NOW + timedelta(seconds=10)
        result = self.service.defer_job(claimed, NOW, due, "transport")
        self.assertEqual("applied", result.disposition.value)
        job = self.service.list_jobs(self.run_id)[0]
        self.assertIs(JobStatus.RETRY_WAIT, job.status)
        self.assertEqual(due, job.available_at)
        timeline = self.service.get_timeline(self.run_id)
        deferred = next(
            item for item in timeline
            if item.envelope.event_type == "job_transitioned"
            and item.envelope.payload["to"] == JobStatus.RETRY_WAIT.value
        )
        self.assertEqual("2026-07-17T00:00:10Z", deferred.envelope.payload["available_at"])
        replayed = {}
        for item in timeline:
            replayed = reduce_run_view(replayed, item)
        self.assertEqual(
            "2026-07-17T00:00:10Z",
            replayed["jobs"][str(job.job_id)]["available_at"],
        )
        with self.service.uow_factory() as uow:
            timer = uow.timers.list_by_run(self.run_id)[0]
            self.assertIs(TimerStatus.SCHEDULED, timer.status)
        dispatcher = TimerDispatcher(self.service, clock=lambda: due)
        self.assertTrue(dispatcher.run_once())
        self.assertIs(JobStatus.READY, self.service.list_jobs(self.run_id)[0].status)

    def test_cancel_run_cancels_job_and_timer(self):
        self.service.submit(self.start)
        job = self.service.list_jobs(self.run_id)[0]
        self.service.schedule_timer(
            self.run_id, purpose="node_timeout", dedupe_key="timeout-1",
            target_type="job", target_id=job.job_id, payload={},
            due_at=NOW + timedelta(seconds=20), now=NOW,
        )
        run = self.service.get_run(self.run_id)
        result = self.service.submit(CommandEnvelope(
            EntityId("command", "cancel-durable"), "cancel_run", self.run_id,
            self.run_id, run.aggregate_version, "cancel-durable", "test", NOW,
            {"reason": "test"},
        ))
        self.assertEqual("applied", result.disposition.value)
        self.assertIs(JobStatus.CANCELLED, self.service.list_jobs(self.run_id)[0].status)
        with self.service.uow_factory() as uow:
            self.assertIs(TimerStatus.CANCELLED, uow.timers.list_by_run(self.run_id)[0].status)

    def test_schedule_timer_semantic_dedupe_returns_original_timer(self):
        self.service.submit(self.start)
        job = self.service.list_jobs(self.run_id)[0]
        first = self.service.schedule_timer(
            self.run_id, purpose="node_timeout", dedupe_key="semantic-once",
            target_type="job", target_id=job.job_id, payload={"source": 1},
            due_at=NOW + timedelta(seconds=20), now=NOW,
        )
        timer_id = EntityId.parse(first.summary["timer_id"])
        second = self.service.submit(CommandEnvelope(
            EntityId("command", "semantic-timer-second"), "schedule_timer",
            timer_id, self.run_id, AggregateVersion(0),
            "different-idempotency-key", "system:timer", NOW,
            {
                "purpose": "node_timeout", "dedupe_key": "semantic-once",
                "target_type": "job", "target_id": str(job.job_id),
                "payload_schema_version": "1.0", "payload": {"source": 2},
                "due_at": (NOW + timedelta(seconds=99)).isoformat().replace("+00:00", "Z"),
            },
        ))
        self.assertIs(CommandResultDisposition.REPLAYED, second.disposition)
        self.assertEqual(str(timer_id), second.summary["timer_id"])
        self.assertEqual(first.event_ids, second.event_ids)
        with self.service.uow_factory() as uow:
            self.assertEqual(1, len(uow.timers.list_by_run(self.run_id)))
            self.assertIsNone(uow.receipts.get(timer_id, "different-idempotency-key"))

    def test_running_lease_loss_is_unknown_and_never_retried(self):
        service = DurableRuntimeApplicationService(
            self.path, execution_safety=ExecutionSafety.UNKNOWN_ON_LEASE_LOSS
        )
        service.submit(self.start)
        claimed = service.claim_job("w1", NOW)
        service.start_job(claimed, NOW)
        report = service.expire_lease(claimed.lease_id, NOW + timedelta(seconds=31))
        self.assertEqual("applied", report.disposition.value)
        self.assertIs(JobStatus.FAILED, service.list_jobs(self.run_id)[0].status)
        submitted = []
        original_submit = service.submit

        def track_submit(command):
            submitted.append(command.command_type)
            return original_submit(command)

        service.submit = track_submit
        recovery = service.durable_recovery.scan_once(NOW + timedelta(seconds=31))
        self.assertEqual(1, recovery.unknown_attempts)
        self.assertTrue(recovery.diagnostics[0].startswith("UNKNOWN_EXTERNAL_RESULT:"))
        self.assertNotIn("materialize_job", submitted)
        self.assertIsNone(service.claim_job("w2", NOW + timedelta(seconds=32)))

    def test_claim_rejects_lease_beyond_kernel_maximum_ttl(self):
        self.service.submit(self.start)
        self.assertIsNone(
            self.service.claim_job(
                "w1", NOW, lease_ttl=timedelta(days=365)
            )
        )
        self.assertIsNotNone(self.service.claim_job("w2", NOW))

    def test_expiry_before_start_is_safe_to_reclaim(self):
        self.service.submit(self.start)
        first = self.service.claim_job("w1", NOW)
        self.service.expire_lease(first.lease_id, NOW + timedelta(seconds=31))
        job = self.service.list_jobs(self.run_id)[0]
        self.assertIs(JobStatus.READY, job.status)
        second = self.service.claim_job("w2", NOW + timedelta(seconds=32))
        self.assertIsNotNone(second)
        self.assertGreater(second.fencing_token, first.fencing_token)

    def test_recovery_requeues_expired_timer_lease(self):
        self.service.submit(self.start)
        job = self.service.list_jobs(self.run_id)[0]
        self.service.schedule_timer(
            self.run_id, purpose="node_timeout", dedupe_key="expired-timer",
            target_type="job", target_id=job.job_id, payload={}, due_at=NOW, now=NOW,
        )
        claimed = self.service.claim_timer("timer", NOW)
        recovery = self.service.durable_recovery.scan_once(NOW + timedelta(seconds=16))
        self.assertEqual(1, recovery.expired_timer_leases)
        with self.service.uow_factory() as uow:
            self.assertIs(TimerStatus.SCHEDULED, uow.timers.get(claimed.timer_id).status)

    def test_concurrent_claim_has_exactly_one_winner(self):
        self.service.submit(self.start)
        results = []
        threads = [
            threading.Thread(
                target=lambda worker=f"w{index}": results.append(
                    self.service.claim_job(worker, NOW)
                )
            )
            for index in range(8)
        ]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        self.assertEqual(1, sum(item is not None for item in results))
        job = self.service.list_jobs(self.run_id)[0]
        with self.service.uow_factory() as uow:
            self.assertIsNotNone(uow.leases.get_active_for_job(job.job_id))

    def test_memory_and_sqlite_durable_kernel_are_equivalent(self):
        memory = MemoryRuntimeDatabase()
        kernel = DurableRuntimeKernel(
            lambda: MemoryUnitOfWork(memory), self.service.workflow_versions,
            work_scheduler=DurableWorkScheduler(execution_safety=ExecutionSafety.REPLAY_SAFE),
        )
        sqlite_start = self.service.submit(self.start)
        memory_start = kernel.handle(self.start)
        self.assertEqual(sqlite_start.event_ids, memory_start.event_ids)
        sqlite_job = self.service.list_jobs(self.run_id)[0]
        memory_job = memory.jobs.list_by_run(self.run_id)[0]
        self.assertEqual(
            (sqlite_job.job_id, sqlite_job.status, sqlite_job.aggregate_version),
            (memory_job.job_id, memory_job.status, memory_job.aggregate_version),
        )
        raw = "fixed-lease-token"
        command = CommandEnvelope(
            EntityId("command", "parity-claim"), "claim_job", sqlite_job.job_id,
            self.run_id, sqlite_job.aggregate_version, "parity-claim", "worker:test", NOW,
            {
                "worker_id": "test", "lease_id": "lease:parity",
                "token_hash": token_hash(raw), "token_hash_version": "1.0",
                "lease_expires_at": "2026-07-17T00:00:30Z",
                "observed_at": "2026-07-17T00:00:00Z",
            },
        )
        sqlite_claim = self.service.submit(command)
        memory_claim = kernel.handle(command)
        self.assertEqual(sqlite_claim, memory_claim)

    def test_bearer_token_never_enters_event_snapshot_or_metrics(self):
        self.service.submit(self.start)
        claimed = self.service.claim_job("secret-worker", NOW)
        timeline_text = repr(self.service.get_timeline(self.run_id))
        report_text = repr(self.service.recovery.rehydrate(self.run_id).state)
        self.assertNotIn(claimed.lease_token, timeline_text)
        self.assertNotIn(claimed.lease_token, report_text)

    def test_node_timeout_timer_atomically_fails_running_work(self):
        self.service.submit(self.start)
        claimed = self.service.claim_job("timeout-worker", NOW)
        self.service.start_job(claimed, NOW)
        due = NOW + timedelta(seconds=5)
        self.service.schedule_timer(
            self.run_id, purpose="node_timeout", dedupe_key="node-timeout-1",
            target_type="job", target_id=claimed.job_id, payload={},
            due_at=due, now=NOW,
        )
        self.assertTrue(TimerDispatcher(self.service, clock=lambda: due).run_once())
        self.assertIs(JobStatus.FAILED, self.service.list_jobs(self.run_id)[0].status)
        self.assertIs(WorkflowRunStatus.FAILED, self.service.get_run(self.run_id).status)

    def test_metrics_failure_cannot_change_worker_result(self):
        class BrokenMetrics:
            def increment(self, *args, **kwargs): raise RuntimeError("metrics down")
        self.service.submit(self.start)
        worker = WorkerRuntime(
            self.service, lambda node, value, token: {"value": 1},
            clock=lambda: NOW, metrics=BrokenMetrics(),
        )
        self.assertTrue(worker.run_once())
        self.assertIn(
            JobStatus.COMPLETED,
            {item.status for item in self.service.list_jobs(self.run_id)},
        )

    def test_recovery_materializes_job_for_step4_ready_node(self):
        legacy = RuntimeApplicationService(self.path)
        legacy.submit(self.start)
        durable = DurableRuntimeApplicationService(self.path)
        self.assertEqual((), durable.list_jobs(self.run_id))
        report = durable.durable_recovery.scan_once(NOW)
        self.assertEqual(1, report.materialized_jobs)
        self.assertEqual(1, len(durable.list_jobs(self.run_id)))

    def test_cancel_run_is_observed_by_worker_cancellation_token(self):
        self.service.submit(self.start)
        entered = threading.Event()
        observed = threading.Event()

        def executor(node, value, token):
            entered.set()
            self.assertTrue(observed.wait(2))
            token.raise_if_cancelled()

        worker = WorkerRuntime(self.service, executor, clock=lambda: NOW)
        thread = threading.Thread(target=worker.run_once)
        thread.start()
        self.assertTrue(entered.wait(2))
        run = self.service.get_run(self.run_id)
        self.service.submit(CommandEnvelope(
            EntityId("command", "cancel-during-execute"), "cancel_run",
            self.run_id, self.run_id, run.aggregate_version,
            "cancel-during-execute", "test", NOW, {"reason": "test"},
        ))
        observed.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIs(WorkflowRunStatus.CANCELLED, self.service.get_run(self.run_id).status)
        self.assertIs(JobStatus.CANCELLED, self.service.list_jobs(self.run_id)[0].status)


if __name__ == "__main__": unittest.main()
