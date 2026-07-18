from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from orbit.workflow.application.durable_runtime_service import DurableRuntimeApplicationService
from orbit.workflow.domain.definitions import CompiledWorkflow
from orbit.workflow.domain.envelopes import CommandEnvelope
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.serialization import definition_hash
from orbit.workflow.domain.states import AttemptStatus, JobStatus, TimerStatus
from orbit.workflow.domain.versions import AggregateVersion
from orbit.workflow.domain.handlers import ExternalEffect, HandlerResult, HandlerResultStatus
from orbit.workflow.persistence.uow import SQLiteUnitOfWork
from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
from tests.test_workflow_runtime import linear_ir


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


def prepare(path: Path):
    ir = linear_ir()
    digest = definition_hash(ir)
    SQLiteWorkflowVersionStore(path).publish(
        CompiledWorkflow(ir, digest, "1.0", "sha256:" + "f" * 64),
        expected_latest_version=0, source_format="json", source_text=None,
        actor="test",
    )
    service = DurableRuntimeApplicationService(path)
    run_id = EntityId("run", "fault")
    service.submit(CommandEnvelope(
        EntityId("command", "fault-start"), "start_run", run_id, run_id,
        AggregateVersion(0), "fault-start", "test", NOW,
        {
            "workflow_id": "workflow:linear", "workflow_version": 1,
            "definition_hash": digest.value, "input": {"value": 0},
        },
    ))
    return service, run_id


class DurableFaultTests(unittest.TestCase):
    def test_claim_kill_points_leave_no_attempt_or_lease(self):
        for point in (
            "after_attempt_create", "after_event_insert", "after_lease_create",
            "after_job_update", "before_receipt_insert", "before_commit",
        ):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "fault.db"
                service, run_id = prepare(path)
                before = service.get_timeline(run_id)

                def fail(current, expected=point):
                    if current == expected: raise RuntimeError("injected")

                service.kernel.uow_factory = lambda: SQLiteUnitOfWork(path, fault_hook=fail)
                self.assertIsNone(service.claim_job("worker", NOW))
                with service.uow_factory() as uow:
                    job = uow.jobs.list_by_run(run_id)[0]
                    self.assertIs(JobStatus.READY, job.status)
                    self.assertEqual((), uow.attempts.list_by_node_run(job.node_run_id))
                    self.assertIsNone(uow.leases.get_active_for_job(job.job_id))
                    self.assertEqual(before, uow.events.read_run(run_id, limit=1000))

    def test_complete_downstream_job_failure_rolls_back_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fault.db"
            service, run_id = prepare(path)
            claimed = service.claim_job("worker", NOW)
            service.start_job(claimed, NOW)
            before = service.get_timeline(run_id)

            def fail(point):
                if point == "after_job_create": raise RuntimeError("injected")

            service.kernel.uow_factory = lambda: SQLiteUnitOfWork(path, fault_hook=fail)
            result = service.complete_job(claimed, NOW, {"value": 1})
            self.assertEqual("INTERNAL_ERROR", result.diagnostics[0].code)
            with service.uow_factory() as uow:
                job = uow.jobs.get(claimed.job_id)
                attempt = uow.attempts.get(claimed.attempt_id)
                self.assertIs(JobStatus.RUNNING, job.status)
                self.assertIs(AttemptStatus.RUNNING, attempt.status)
                self.assertEqual(before, uow.events.read_run(run_id, limit=1000))

    def test_timer_fire_target_failure_rolls_back_timer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fault.db"
            service, run_id = prepare(path)
            claimed = service.claim_job("worker", NOW)
            service.start_job(claimed, NOW)
            due = NOW + timedelta(seconds=1)
            service.defer_job(claimed, NOW, due, "transport")
            timer = service.claim_timer("timer", due)
            before = service.get_timeline(run_id)

            def fail(point):
                if point == "after_job_update": raise RuntimeError("injected")

            service.kernel.uow_factory = lambda: SQLiteUnitOfWork(path, fault_hook=fail)
            result = service.fire_timer(timer, due)
            self.assertEqual("INTERNAL_ERROR", result.diagnostics[0].code)
            with service.uow_factory() as uow:
                self.assertIs(JobStatus.RETRY_WAIT, uow.jobs.get(claimed.job_id).status)
                self.assertIs(TimerStatus.LEASED, uow.timers.get(timer.timer_id).status)
                self.assertEqual(before, uow.events.read_run(run_id, limit=1000))

    def test_final_usage_and_result_roll_back_together_before_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fault.db"
            service, run_id = prepare(path)
            claimed = service.claim_job("worker", NOW)
            service.start_job(claimed, NOW)
            before = service.get_timeline(run_id)
            result_value = HandlerResult(
                HandlerResultStatus.SUCCEEDED, {"value": 1}, None, None,
                True, ExternalEffect.NONE,
            )

            def fail(point):
                if point == "before_commit": raise RuntimeError("injected")

            service.kernel.uow_factory = lambda: SQLiteUnitOfWork(path, fault_hook=fail)
            result = service.complete_job(
                claimed, NOW, {"value": 1}, handler_result=result_value
            )
            self.assertEqual("INTERNAL_ERROR", result.diagnostics[0].code)
            with service.uow_factory() as uow:
                self.assertIs(JobStatus.RUNNING, uow.jobs.get(claimed.job_id).status)
                self.assertIs(AttemptStatus.RUNNING, uow.attempts.get(claimed.attempt_id).status)
                self.assertEqual(before, uow.events.read_run(run_id, limit=1000))


if __name__ == "__main__": unittest.main()
