from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from orbit.workflow.application.runtime_service import RuntimeApplicationService
from orbit.workflow.domain.definitions import CompiledWorkflow
from orbit.workflow.domain.envelopes import CommandEnvelope
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.runtime import CommandResultDisposition
from orbit.workflow.domain.serialization import definition_hash
from orbit.workflow.domain.states import AttemptStatus, NodeRunStatus
from orbit.workflow.domain.versions import AggregateVersion
from orbit.workflow.persistence import SQLiteReadSession, SQLiteUnitOfWork
from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
from orbit.workflow.runtime.events import derived_id
from orbit.workflow.runtime.kernel import RuntimeKernel
from tests.test_workflow_runtime import linear_ir


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


def prepare(path: Path):
    ir = linear_ir()
    digest = definition_hash(ir)
    versions = SQLiteWorkflowVersionStore(path)
    versions.publish(
        CompiledWorkflow(ir, digest, "1.0", "sha256:" + "c" * 64),
        expected_latest_version=0, source_format="json", source_text=None, actor="test",
    )
    return versions, digest


def start_command(run_id: EntityId, digest, suffix="1"):
    return CommandEnvelope(
        EntityId("command", f"start{suffix}"), "start_run", run_id, run_id,
        AggregateVersion(0), f"start-{suffix}", "test", NOW,
        {"workflow_id": "workflow:linear", "workflow_version": 1, "definition_hash": digest.value, "input": {"value": 0}},
    )


class RuntimeFaultTests(unittest.TestCase):
    def test_start_run_kill_points_never_leave_partial_runtime_state(self) -> None:
        kill_points = (
            "after_run_create", "after_event_insert", "after_plan_append",
            "after_node_run_create", "before_receipt_insert", "before_commit",
        )
        for index, kill_point in enumerate(kill_points):
            with self.subTest(kill_point=kill_point), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "runtime.db"
                versions, digest = prepare(path)

                def fail(point, expected=kill_point):
                    if point == expected:
                        raise RuntimeError("injected")

                kernel = RuntimeKernel(
                    lambda: SQLiteUnitOfWork(path, fault_hook=fail), versions
                )
                result = kernel.handle(
                    start_command(EntityId("run", f"r{index}"), digest, str(index))
                )
                self.assertEqual("INTERNAL_ERROR", result.diagnostics[0].code)
                with SQLiteReadSession(path) as connection:
                    for table in (
                        "workflow_runs", "execution_plans", "node_runs",
                        "node_attempts", "run_events", "command_receipts",
                    ):
                        self.assertEqual(
                            0, connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                            table,
                        )

    def test_downstream_schedule_failure_rolls_back_attempt_and_node_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            versions, digest = prepare(path)
            service = RuntimeApplicationService(path)
            run_id = EntityId("run", "r1")
            service.submit(start_command(run_id, digest))
            with service.uow_factory() as uow:
                node = uow.node_runs.list_by_run(run_id)[0]
            start_attempt = CommandEnvelope(
                EntityId("command", "attempt-start"), "start_attempt",
                node.node_run_id, run_id, node.aggregate_version,
                "attempt-start", "test", NOW, {},
            )
            started = service.submit(start_attempt)
            attempt_id = EntityId.parse(started.summary["attempt_id"])
            with service.uow_factory() as uow:
                before_events = len(uow.events.read_run(run_id, limit=1000))

            def fail(point):
                if point == "after_node_run_create":
                    raise RuntimeError("injected")

            kernel = RuntimeKernel(
                lambda: SQLiteUnitOfWork(path, fault_hook=fail), versions
            )
            complete = CommandEnvelope(
                EntityId("command", "attempt-complete"), "complete_attempt",
                attempt_id, run_id, AggregateVersion(2), "attempt-complete",
                "test", NOW, {"output": {"value": 1}},
            )
            result = kernel.handle(complete)
            self.assertEqual("INTERNAL_ERROR", result.diagnostics[0].code)
            with service.uow_factory() as uow:
                attempt = uow.attempts.get(attempt_id)
                current_node = uow.node_runs.get(node.node_run_id)
                self.assertEqual(AttemptStatus.RUNNING, attempt.status)
                self.assertEqual(AggregateVersion(2), attempt.aggregate_version)
                self.assertEqual(NodeRunStatus.RUNNING, current_node.status)
                self.assertEqual(1, len(uow.node_runs.list_by_run(run_id)))
                self.assertEqual(before_events, len(uow.events.read_run(run_id, limit=1000)))

            applied = service.submit(complete)
            restarted = RuntimeApplicationService(path)
            replayed = restarted.submit(complete)
            self.assertEqual(CommandResultDisposition.APPLIED, applied.disposition)
            self.assertEqual(CommandResultDisposition.REPLAYED, replayed.disposition)
            self.assertEqual(applied.event_ids, replayed.event_ids)
            with restarted.uow_factory() as uow:
                self.assertEqual(2, len(uow.node_runs.list_by_run(run_id)))

    def test_snapshot_failure_does_not_change_successful_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.db"
            versions, digest = prepare(path)

            class BrokenSnapshots:
                def consider(self, run_id):
                    raise RuntimeError("snapshot unavailable")

            kernel = RuntimeKernel(
                lambda: SQLiteUnitOfWork(path), versions,
                snapshot_coordinator=BrokenSnapshots(),
            )
            run_id = EntityId("run", "r1")
            result = kernel.handle(start_command(run_id, digest))
            self.assertEqual(CommandResultDisposition.APPLIED, result.disposition)
            service = RuntimeApplicationService(path)
            self.assertIsNotNone(service.get_run(run_id))

    def test_terminal_command_kill_points_roll_back_every_secondary_reaction(self) -> None:
        cases = {
            "complete_attempt": (
                "after_event_insert", "after_attempt_update",
                "after_node_run_update", "before_receipt_insert", "before_commit",
            ),
            "fail_attempt": (
                "after_event_insert", "after_attempt_update",
                "after_node_run_update", "after_run_update", "before_commit",
            ),
            "cancel_run": (
                "after_event_insert", "after_attempt_update",
                "after_node_run_update", "after_run_update", "before_commit",
            ),
        }
        for command_type, kill_points in cases.items():
            for index, kill_point in enumerate(kill_points):
                with self.subTest(command=command_type, kill_point=kill_point), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "runtime.db"
                    versions, digest = prepare(path)
                    service = RuntimeApplicationService(path)
                    run_id = EntityId("run", f"{command_type}-{index}")
                    service.submit(start_command(run_id, digest, f"{command_type}-{index}"))
                    with service.uow_factory() as uow:
                        node = uow.node_runs.list_by_run(run_id)[0]
                    started = service.submit(CommandEnvelope(
                        EntityId("command", f"begin-{command_type}-{index}"),
                        "start_attempt", node.node_run_id, run_id,
                        node.aggregate_version, f"begin-{command_type}-{index}",
                        "test", NOW, {},
                    ))
                    attempt_id = EntityId.parse(started.summary["attempt_id"])
                    with service.uow_factory() as uow:
                        before_events = tuple(uow.events.read_run(run_id, limit=1000))
                        before_run = uow.runs.get(run_id)
                        before_node = uow.node_runs.get(node.node_run_id)
                        before_attempt = uow.attempts.get(attempt_id)

                    def fail(point, expected=kill_point):
                        if point == expected:
                            raise RuntimeError("injected")

                    payload = {}
                    aggregate_id = run_id
                    expected_version = before_run.aggregate_version
                    if command_type == "complete_attempt":
                        payload = {"output": {"value": 1}}
                        aggregate_id = attempt_id
                        expected_version = before_attempt.aggregate_version
                    elif command_type == "fail_attempt":
                        payload = {"error": {
                            "code": "handler_permanent", "category": "permanent_error",
                            "message": "failed", "source": "test",
                            "details": {}, "cause": None,
                        }}
                        aggregate_id = attempt_id
                        expected_version = before_attempt.aggregate_version
                    command = CommandEnvelope(
                        EntityId("command", f"kill-{command_type}-{index}"),
                        command_type, aggregate_id, run_id, expected_version,
                        f"kill-{command_type}-{index}", "test", NOW, payload,
                    )
                    result = RuntimeKernel(
                        lambda: SQLiteUnitOfWork(path, fault_hook=fail), versions
                    ).handle(command)
                    self.assertEqual("INTERNAL_ERROR", result.diagnostics[0].code)
                    with service.uow_factory() as uow:
                        self.assertEqual(before_events, tuple(uow.events.read_run(run_id, limit=1000)))
                        self.assertEqual(before_run, uow.runs.get(run_id))
                        self.assertEqual(before_node, uow.node_runs.get(node.node_run_id))
                        self.assertEqual(before_attempt, uow.attempts.get(attempt_id))


if __name__ == "__main__":
    unittest.main()
