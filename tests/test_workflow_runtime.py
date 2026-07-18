from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import json
import tempfile
import threading
import unittest

from orbit.workflow.application.runtime_service import RuntimeApplicationService
from orbit.workflow.data.mapping import MappingEvaluationError, evaluate_mapping
from orbit.workflow.domain.definitions import (
    CompiledWorkflow, IREdge, IRHandlerRef, IRNode, IRPort, WorkflowIR,
)
from orbit.workflow.domain.envelopes import CommandEnvelope
from orbit.workflow.domain.execution_plan import execution_plan_from_primitive
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.persistence import UnsupportedEventVersionError
from orbit.workflow.domain.schemas import SchemaValidationError
from orbit.workflow.domain.runtime import CommandResultDisposition
from orbit.workflow.domain.serialization import definition_hash, to_primitive
from orbit.workflow.domain.states import AttemptStatus, NodeRunStatus, WorkflowRunStatus
from orbit.workflow.domain.versions import AggregateVersion, Revision
from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
from orbit.workflow.persistence.memory import MemoryRuntimeDatabase, MemoryUnitOfWork
from orbit.workflow.persistence.snapshots import SnapshotPolicy
from orbit.workflow.persistence.rehydration import rehydrate_aggregate
from orbit.workflow.runtime.events import derived_id
from orbit.workflow.runtime.kernel import RuntimeKernel
from orbit.workflow.runtime.plan_instantiator import UnsupportedPlanShapeError, instantiate_execution_plan
from orbit.workflow.runtime.reducers import (
    reduce_attempt, reduce_node_run, reduce_run_view, reduce_workflow_run,
)
from orbit.workflow.runtime.testing_driver import InMemoryExecutionDriver
from orbit.workflow.runtime.snapshot_coordinator import SnapshotCoordinator
from orbit.workflow.testing import assert_reducer_source_is_pure


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)
GOLDEN = Path(__file__).parent / "fixtures/workflow_runtime/v1/three-node.json"


def linear_ir(*, conditional=False) -> WorkflowIR:
    node_ids = ("collect", "transform", "publish")
    port = IRPort("value", "example://integer/1.0", True, False, None, "")
    nodes = tuple(
        IRNode(
            node_id, "action", (port,), (port,), IRHandlerRef(
                node_id, "1.0.0", "sha256:" + "a" * 64
            ),
            {}, (), None,
        )
        for node_id in node_ids
    ) + (IRNode("done", "terminal", (port,), (), None, {}, (), None),)
    chain = (*node_ids, "done")
    edges = tuple(
        IREdge(
            f"{source}_{target}", source, "value", target, "value", "success",
            {"op": "eq", "left": {"op": "literal", "value": 1}, "right": {"op": "literal", "value": 1}}
            if conditional and index == 0 else {"op": "literal", "value": True},
            {"op": "identity", "schema_id": "example://value/1.0"},
        )
        for index, (source, target) in enumerate(zip(chain, chain[1:]))
    )
    return WorkflowIR(
        "1.1", "workflow:linear", "Linear", "", {}, (), (), nodes, edges,
        ("collect",), ("done",), (), (), {},
    )


class ExecutionPlanTests(unittest.TestCase):
    def test_plan_is_deterministic_round_trips_and_rejects_conditions(self) -> None:
        ir = linear_ir()
        digest = definition_hash(ir)
        arguments = dict(
            run_id=EntityId("run", "r1"), plan_id=EntityId("plan", "p1"),
            workflow_version=Revision(1),
            workflow_definition_hash=digest,
        )
        first = instantiate_execution_plan(ir, **arguments)
        second = instantiate_execution_plan(ir, **arguments)
        self.assertEqual(definition_hash(first), definition_hash(second))
        self.assertEqual(first, execution_plan_from_primitive(to_primitive(first)))
        self.assertEqual(("collect", "transform", "publish", "done"), first.ordered_node_ids)
        with self.assertRaises(UnsupportedPlanShapeError):
            instantiate_execution_plan(linear_ir(conditional=True), **arguments)

        primitive = to_primitive(first)
        primitive["nodes"][0]["outputs"][0]["data_policy"]["transport"] = "file"
        with self.assertRaises((ValueError, SchemaValidationError)):
            execution_plan_from_primitive(primitive)


class RuntimeKernelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.db"
        ir = linear_ir()
        self.digest = definition_hash(ir)
        SQLiteWorkflowVersionStore(self.path).publish(
            CompiledWorkflow(ir, self.digest, "1.0", "sha256:" + "c" * 64),
            expected_latest_version=0, source_format="json", source_text=None,
            actor="test",
        )
        self.service = RuntimeApplicationService(self.path)
        self.run_id = EntityId("run", "r1")
        self.start = CommandEnvelope(
            EntityId("command", "start"), "start_run", self.run_id, self.run_id,
            AggregateVersion(0), "start-r1", "test", NOW,
            {
                "workflow_id": "workflow:linear", "workflow_version": 1,
                "definition_hash": self.digest.value, "input": {"value": 0},
            },
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_three_node_workflow_duplicate_commands_snapshot_and_recovery(self) -> None:
        started = self.service.submit(self.start)
        self.assertEqual(CommandResultDisposition.APPLIED, started.disposition)
        replayed = self.service.submit(self.start)
        self.assertEqual(CommandResultDisposition.REPLAYED, replayed.disposition)
        self.assertEqual(started.event_ids, replayed.event_ids)
        self.assertEqual(started.summary, replayed.summary)
        self.assertEqual((self.run_id,), tuple(item.run_id for item in self.service.list_unfinished()))
        plan_record = self.service.get_plan(self.run_id)
        self.assertIsNotNone(plan_record)
        plan = execution_plan_from_primitive(to_primitive(plan_record.plan))
        self.assertEqual("collect", plan.entry_node_id)

        driver = InMemoryExecutionDriver(
            self.service,
            {
                "collect": lambda value: {"value": value["value"] + 1},
                "transform": lambda value: {"value": value["value"] * 2},
                "publish": lambda value: {"value": value["value"] + 3},
            },
            clock=lambda: NOW,
        )
        driver.run_ready_nodes(self.run_id)
        run = self.service.get_run(self.run_id)
        self.assertEqual(WorkflowRunStatus.SUCCEEDED, run.status)
        with self.service.uow_factory() as uow:
            nodes = uow.node_runs.list_by_run(self.run_id)
            by_node = {item.node_id: item for item in nodes}
            nodes = [by_node[item] for item in ("collect", "transform", "publish")]
            self.assertEqual(["collect", "transform", "publish"], [item.node_id for item in nodes])
            self.assertTrue(all(item.status is NodeRunStatus.SUCCEEDED for item in nodes))
            attempts = [item for node in nodes for item in uow.attempts.list_by_node_run(node.node_run_id)]
            self.assertTrue(all(item.status is AttemptStatus.SUCCEEDED for item in attempts))
            self.assertTrue(all(item.aggregate_version == AggregateVersion(4) for item in attempts))
            snapshots = uow.snapshots.list(self.run_id)
            self.assertEqual(1, len(snapshots))
            outputs = [
                item.envelope.payload["output"]
                for item in uow.events.read_run(self.run_id, limit=1000)
                if item.envelope.event_type == "attempt_output_recorded"
            ]
            timeline = uow.events.read_run(self.run_id, limit=1000)
            replayed_run = rehydrate_aggregate(
                uow.events, self.run_id, None, reduce_workflow_run,
                self.service.recovery.reader,
            )
            replayed_nodes = {
                node.node_id: rehydrate_aggregate(
                    uow.events, node.node_run_id, None, reduce_node_run,
                    self.service.recovery.reader,
                )
                for node in nodes
            }
            replayed_attempts = {
                str(attempt.attempt_id): rehydrate_aggregate(
                    uow.events, attempt.attempt_id, None, reduce_attempt,
                    self.service.recovery.reader,
                )
                for attempt in attempts
            }
        self.assertEqual([{"value": 1}, {"value": 2}, {"value": 5}], outputs)
        self.assertEqual(WorkflowRunStatus.SUCCEEDED, replayed_run)
        self.assertTrue(all(value is NodeRunStatus.SUCCEEDED for value in replayed_nodes.values()))
        self.assertTrue(all(value is AttemptStatus.SUCCEEDED for value in replayed_attempts.values()))
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        timeline_value = [
            {"global_position": item.global_position, "envelope": to_primitive(item.envelope)}
            for item in timeline
        ]
        self.assertEqual(golden["event_count"], len(timeline))
        self.assertEqual(golden["event_types"], [item.envelope.event_type for item in timeline])
        self.assertEqual(golden["timeline_hash"], definition_hash(timeline_value).value)
        self.assertEqual((), self.service.recovery.verify_projection(self.run_id))
        report = self.service.recovery.rehydrate(self.run_id)
        self.assertEqual("succeeded", report.state["run_status"])
        paged_state = {"run_status": None, "nodes": {}, "attempts": {}, "outputs": {}}
        cursor = 0
        while True:
            page = self.service.get_timeline(self.run_id, after=cursor, limit=3)
            if not page:
                break
            for item in page:
                paged_state = reduce_run_view(paged_state, item)
            cursor = page[-1].global_position
        self.assertEqual(report.state, paged_state)
        with self.service.uow_factory() as uow:
            for snapshot in uow.snapshots.list(self.run_id):
                uow.snapshots.delete(snapshot.snapshot_id)
            uow.commit()
        event_only = self.service.recovery.rehydrate(self.run_id)
        self.assertEqual(report.state, event_only.state)

        # Reopening the service preserves receipts, projections, plan, and replay.
        restarted = RuntimeApplicationService(self.path)
        self.assertEqual(WorkflowRunStatus.SUCCEEDED, restarted.get_run(self.run_id).status)
        self.assertEqual(CommandResultDisposition.REPLAYED, restarted.submit(self.start).disposition)
        snapshot_results = []
        threads = [
            threading.Thread(
                target=lambda: snapshot_results.append(restarted.snapshots.consider(self.run_id))
            )
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        with restarted.uow_factory() as uow:
            self.assertEqual(1, len(uow.snapshots.list(self.run_id)))

    def test_failure_stops_downstream_and_cancel_converges(self) -> None:
        self.service.submit(self.start)
        driver = InMemoryExecutionDriver(
            self.service,
            {"collect": lambda value: (_ for _ in ()).throw(RuntimeError("boom"))},
            clock=lambda: NOW,
        )
        driver.run_ready_nodes(self.run_id)
        self.assertEqual(WorkflowRunStatus.FAILED, self.service.get_run(self.run_id).status)
        with self.service.uow_factory() as uow:
            self.assertEqual(1, len(uow.node_runs.list_by_run(self.run_id)))

        second_run = EntityId("run", "r2")
        start2 = CommandEnvelope(
            EntityId("command", "start2"), "start_run", second_run, second_run,
            AggregateVersion(0), "start-r2", "test", NOW,
            {**dict(self.start.payload), "input": {"value": 1}},
        )
        self.service.submit(start2)
        current = self.service.get_run(second_run)
        cancel = CommandEnvelope(
            EntityId("command", "cancel2"), "cancel_run", second_run, second_run,
            current.aggregate_version, "cancel-r2", "test", NOW, {"reason": "user"},
        )
        result = self.service.submit(cancel)
        self.assertEqual(CommandResultDisposition.APPLIED, result.disposition)
        self.assertEqual(WorkflowRunStatus.CANCELLED, self.service.get_run(second_run).status)
        with self.service.uow_factory() as uow:
            self.assertTrue(all(item.status is NodeRunStatus.CANCELLED for item in uow.node_runs.list_by_run(second_run)))

    def test_stale_and_conflicting_idempotency_are_rejected(self) -> None:
        self.service.submit(self.start)
        stale = CommandEnvelope(
            EntityId("command", "stale"), "cancel_run", self.run_id, self.run_id,
            AggregateVersion(0), "stale", "test", NOW, {},
        )
        result = self.service.submit(stale)
        self.assertEqual("CONCURRENCY_CONFLICT", result.diagnostics[0].code)
        changed = CommandEnvelope(
            self.start.command_id, self.start.command_type, self.start.aggregate_id,
            self.start.correlation_id, self.start.expected_version,
            self.start.idempotency_key, self.start.actor, self.start.issued_at,
            {**dict(self.start.payload), "input": {"value": 99}},
        )
        result = self.service.submit(changed)
        self.assertEqual("IDEMPOTENCY_CONFLICT", result.diagnostics[0].code)

    def test_concurrent_cancel_has_one_winner(self) -> None:
        self.service.submit(self.start)
        version = self.service.get_run(self.run_id).aggregate_version
        commands = [
            CommandEnvelope(
                EntityId("command", f"cancel{index}"), "cancel_run", self.run_id,
                self.run_id, version, f"cancel-{index}", "test", NOW,
                {"reason": f"test-{index}"},
            )
            for index in (1, 2)
        ]
        results = []
        threads = [threading.Thread(target=lambda command=item: results.append(self.service.submit(command))) for item in commands]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(
            [CommandResultDisposition.APPLIED, CommandResultDisposition.REJECTED],
            sorted((item.disposition for item in results), key=lambda item: item.value),
        )
        rejected = next(item for item in results if item.disposition is CommandResultDisposition.REJECTED)
        self.assertEqual("CONCURRENCY_CONFLICT", rejected.diagnostics[0].code)

    def test_command_payload_schema_rejects_unknown_fields_without_writes(self) -> None:
        invalid = CommandEnvelope(
            EntityId("command", "invalid"), "start_run", self.run_id, self.run_id,
            AggregateVersion(0), "invalid", "test", NOW,
            {**dict(self.start.payload), "unexpected": True},
        )
        result = self.service.submit(invalid)
        self.assertEqual(CommandResultDisposition.REJECTED, result.disposition)
        self.assertEqual("VALIDATION_FAILED", result.diagnostics[0].code)
        self.assertIsNone(self.service.get_run(self.run_id))

    def test_schedule_node_is_system_only_and_pure_components_pass_source_guard(self) -> None:
        command = CommandEnvelope(
            EntityId("command", "schedule"), "schedule_node",
            EntityId("node_run", "new"), self.run_id, AggregateVersion(0),
            "schedule", "user", NOW,
            {"run_id": str(self.run_id), "node_id": "collect", "input": {}},
        )
        result = self.service.submit(command)
        self.assertEqual("POLICY_REJECTED", result.diagnostics[0].code)
        for function in (
            instantiate_execution_plan, reduce_workflow_run, reduce_node_run,
            reduce_attempt, reduce_run_view,
        ):
            assert_reducer_source_is_pure(function)

    def test_snapshot_replay_uses_prior_snapshot_tail_and_upcasting_reader(self) -> None:
        self.service.submit(self.start)

        class RecordingReader:
            def __init__(self, delegate):
                self.delegate = delegate
                self.positions = []

            def read(self, stored):
                self.positions.append(stored.global_position)
                return self.delegate.read(stored)

        reader = RecordingReader(self.service.recovery.reader)
        coordinator = SnapshotCoordinator(
            self.service.uow_factory,
            policy=SnapshotPolicy(every_n_events=1),
            event_reader=reader,
        )
        self.assertIsNotNone(coordinator.consider(self.run_id))
        with self.service.uow_factory() as uow:
            first_cursor = uow.snapshots.list(self.run_id)[-1].last_global_position
            node = uow.node_runs.list_by_run(self.run_id)[0]
        reader.positions.clear()

        self.service.submit(CommandEnvelope(
            EntityId("command", "tail-attempt"), "start_attempt", node.node_run_id,
            self.run_id, node.aggregate_version, "tail-attempt", "test", NOW, {},
        ))
        self.assertIsNotNone(coordinator.consider(self.run_id))
        self.assertTrue(reader.positions)
        self.assertTrue(all(position > first_cursor for position in reader.positions))

    def test_unknown_event_fails_in_reader_and_run_view_reducer(self) -> None:
        self.service.submit(self.start)
        event = self.service.get_timeline(self.run_id, limit=1)[0]
        unknown = replace(
            event,
            envelope=replace(event.envelope, event_type="future_runtime_fact"),
        )
        initial = {"run_status": None, "nodes": {}, "attempts": {}, "outputs": {}}
        with self.assertRaisesRegex(ValueError, "unknown Runtime event"):
            reduce_run_view(initial, unknown)
        with self.assertRaises(UnsupportedEventVersionError):
            self.service.recovery.reader.read(unknown)

    def test_integrity_and_invalid_mapping_have_stable_diagnostics(self) -> None:
        self.service.submit(self.start)
        node_run_id = derived_id("node_run", self.run_id, 1, "collect", 1)
        duplicate = CommandEnvelope(
            EntityId("command", "duplicate-node"), "schedule_node", node_run_id,
            self.run_id, AggregateVersion(0), "duplicate-node", "system:test", NOW,
            {"run_id": str(self.run_id), "node_id": "collect", "input": {"value": 0}},
        )
        result = self.service.submit(duplicate)
        self.assertEqual("INTEGRITY_VIOLATION", result.diagnostics[0].code)
        with self.assertRaisesRegex(MappingEvaluationError, "array index"):
            evaluate_mapping(
                {"op": "ref", "path": "source.values.bad"},
                {"values": [1]},
            )

    def test_memory_and_sqlite_uow_produce_the_same_kernel_result(self) -> None:
        memory = MemoryRuntimeDatabase()
        memory_kernel = RuntimeKernel(
            lambda: MemoryUnitOfWork(memory), self.service.workflow_versions
        )
        sqlite_start = self.service.submit(self.start)
        memory_start = memory_kernel.handle(self.start)
        self.assertEqual(sqlite_start, memory_start)

        sqlite_node = self.service.get_timeline(self.run_id)[1].envelope.aggregate_id
        memory_node = memory.node_runs.list_by_run(self.run_id)[0]
        self.assertEqual(sqlite_node, memory_node.node_run_id)
        command = CommandEnvelope(
            EntityId("command", "parity-attempt"), "start_attempt",
            memory_node.node_run_id, self.run_id, memory_node.aggregate_version,
            "parity-attempt", "test", NOW, {},
        )
        sqlite_result = self.service.submit(command)
        memory_result = memory_kernel.handle(command)
        self.assertEqual(sqlite_result, memory_result)
        self.assertEqual(
            CommandResultDisposition.REPLAYED,
            memory_kernel.handle(command).disposition,
        )
        self.assertEqual(
            [item.envelope.event_type for item in self.service.get_timeline(self.run_id)],
            [item.envelope.event_type for item in memory.events.read_run(self.run_id)],
        )

    def test_required_input_and_output_ports_are_enforced_atomically(self) -> None:
        invalid_start = CommandEnvelope(
            EntityId("command", "missing-input"), "start_run", self.run_id,
            self.run_id, AggregateVersion(0), "missing-input", "test", NOW,
            {**dict(self.start.payload), "input": {}},
        )
        result = self.service.submit(invalid_start)
        self.assertEqual("VALIDATION_FAILED", result.diagnostics[0].code)
        self.assertIsNone(self.service.get_run(self.run_id))

        self.service.submit(self.start)
        with self.service.uow_factory() as uow:
            node = uow.node_runs.list_by_run(self.run_id)[0]
        started = self.service.submit(CommandEnvelope(
            EntityId("command", "port-start"), "start_attempt", node.node_run_id,
            self.run_id, node.aggregate_version, "port-start", "test", NOW, {},
        ))
        attempt_id = EntityId.parse(started.summary["attempt_id"])
        invalid_complete = CommandEnvelope(
            EntityId("command", "missing-output"), "complete_attempt", attempt_id,
            self.run_id, AggregateVersion(2), "missing-output", "test", NOW,
            {"output": {}},
        )
        result = self.service.submit(invalid_complete)
        self.assertEqual("VALIDATION_FAILED", result.diagnostics[0].code)
        with self.service.uow_factory() as uow:
            self.assertEqual(AttemptStatus.RUNNING, uow.attempts.get(attempt_id).status)

    def test_unsupported_new_workflow_version_cannot_change_existing_run(self) -> None:
        self.service.submit(self.start)
        original_plan = self.service.get_plan(self.run_id)
        changed_ir = linear_ir(conditional=True)
        changed_hash = definition_hash(changed_ir)
        self.service.workflow_versions.publish(
            CompiledWorkflow(changed_ir, changed_hash, "1.0", "sha256:" + "d" * 64),
            expected_latest_version=1, source_format="json", source_text=None,
            actor="test",
        )
        rejected_run = EntityId("run", "unsupported")
        result = self.service.submit(CommandEnvelope(
            EntityId("command", "unsupported"), "start_run", rejected_run,
            rejected_run, AggregateVersion(0), "unsupported", "test", NOW,
            {
                "workflow_id": "workflow:linear", "workflow_version": 2,
                "definition_hash": changed_hash.value, "input": {"value": 0},
            },
        ))
        self.assertEqual("UNSUPPORTED_PLAN_SHAPE", result.diagnostics[0].code)
        self.assertIsNone(self.service.get_run(rejected_run))
        self.assertEqual(original_plan, self.service.get_plan(self.run_id))


if __name__ == "__main__":
    unittest.main()
