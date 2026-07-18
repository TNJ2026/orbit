from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from orbit.workflow.application.runtime_service import RuntimeApplicationService
from orbit.workflow.domain.data import DataOwnerKind
from orbit.workflow.domain.definitions import CompiledWorkflow
from orbit.workflow.domain.envelopes import CommandEnvelope
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.serialization import definition_hash
from orbit.workflow.domain.versions import AggregateVersion
from orbit.workflow.persistence import SQLiteUnitOfWork
from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
from orbit.workflow.runtime.testing_driver import InMemoryExecutionDriver
from tests.test_workflow_runtime import linear_ir


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


class WorkflowDataPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.path = Path(self.temp.name) / "data.db"
        ir = linear_ir(); self.digest = definition_hash(ir)
        SQLiteWorkflowVersionStore(self.path).publish(
            CompiledWorkflow(ir, self.digest, "1.0", "sha256:" + "d" * 64),
            expected_latest_version=0, source_format="json", source_text=None, actor="test",
        )
        self.service = RuntimeApplicationService(self.path); self.run_id = EntityId("run", "data")
        self.start = CommandEnvelope(
            EntityId("command", "start-data"), "start_run", self.run_id, self.run_id,
            AggregateVersion(0), "start-data", "test", NOW,
            {"workflow_id": "workflow:linear", "workflow_version": 1,
             "definition_hash": self.digest.value, "input": {"value": 2}},
        )

    def tearDown(self): self.temp.cleanup()

    def test_migration_four_and_runtime_value_projection(self):
        self.service.submit(self.start)
        with SQLiteUnitOfWork(self.path) as uow:
            tables = {row[0] for row in uow.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"values", "value_links", "artifacts", "artifact_links"} <= tables)
            entry = uow.node_runs.list_by_run(self.run_id)[0]
            value = uow.values.get_by_owner_port(DataOwnerKind.NODE_INPUT, entry.node_run_id, "value")
            self.assertEqual(2, value.data)
            run_value = uow.values.get_by_owner_port(DataOwnerKind.RUN_INPUT, self.run_id, "value")
            self.assertEqual(2, run_value.data)

    def test_outputs_inputs_and_mapping_lineage_commit_together(self):
        self.service.submit(self.start)
        driver = InMemoryExecutionDriver(self.service, {
            "collect": lambda value: {"value": value["value"] + 1},
            "transform": lambda value: {"value": value["value"]},
            "publish": lambda value: {"value": value["value"]},
        }, clock=lambda: NOW)
        driver.run_ready_nodes(self.run_id)
        with SQLiteUnitOfWork(self.path) as uow:
            nodes = {item.node_id: item for item in uow.node_runs.list_by_run(self.run_id)}
            attempt = uow.attempts.list_by_node_run(nodes["collect"].node_run_id)[0]
            output = uow.values.get_by_owner_port(DataOwnerKind.ATTEMPT_OUTPUT, attempt.attempt_id, "value")
            target = uow.values.get_by_owner_port(DataOwnerKind.NODE_INPUT, nodes["transform"].node_run_id, "value")
            self.assertEqual(3, output.data); self.assertEqual(3, target.data)
            links = uow.value_links.list_for_value(output.value_id, direction="downstream")
            self.assertEqual(target.value_id, links[0].target_value_id)

    def test_value_projection_rolls_back_with_uow(self):
        self.service.submit(self.start)
        with SQLiteUnitOfWork(self.path) as uow:
            value = uow.values.list_by_owner(DataOwnerKind.NODE_INPUT, uow.node_runs.list_by_run(self.run_id)[0].node_run_id)[0]
            with self.assertRaises(Exception): uow.values.insert(replace(value, value_id=EntityId("value", "other")))
