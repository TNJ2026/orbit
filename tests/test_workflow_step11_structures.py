from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from orbit.workflow.application.foreach_service import ForeachService
from orbit.workflow.application.runtime_service import RuntimeApplicationService
from orbit.workflow.application.subflow_service import SubflowService
from orbit.workflow.catalogs import InMemoryHandlerCatalog, InMemorySchemaCatalog
from orbit.workflow.domain.envelopes import CommandEnvelope
from orbit.workflow.domain.foreach import (
    ForeachFailurePolicy, derive_group_id, derive_item_id,
)
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.plan_patch import (
    AgenticRegion, DynamicDagLimits, PatchOperation, PatchOperationKind,
    PlanPatch,
)
from orbit.workflow.domain.serialization import definition_hash, to_primitive
from orbit.workflow.domain.subflow import MAX_SUBFLOW_DEPTH
from orbit.workflow.domain.versions import AggregateVersion, Revision
from orbit.workflow.dsl import compile_source
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
from orbit.workflow.planner.plan_compiler import PatchValidationError, compile_patch
from orbit.workflow.runtime.plan_instantiator import instantiate_execution_plan


NOW = datetime(2026, 7, 18, 3, tzinfo=timezone.utc)
PORT = [{"id": "value", "schema_id": "schema://value/1.0"}]


class Step11StructureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "step11.db"
        source = {
            "dsl_version": "1.2",
            "metadata": {"id": "step11", "name": "Step 11"},
            "nodes": [{"id": "done", "kind": "terminal"}],
            "edges": [], "entry": ["done"], "terminals": ["done"],
        }
        self.compiled = compile_source(
            json.dumps(source), InMemoryHandlerCatalog([]),
            InMemorySchemaCatalog({"schema://value/1.0": {}}),
            source_format="json",
        )
        SQLiteWorkflowVersionStore(self.path).publish(
            self.compiled, expected_latest_version=0, source_format="json",
            source_text=None, actor="test",
        )
        self.run_id = EntityId("run", "step11")
        self._start(self.run_id, "parent")

    def _start(self, run_id, key):
        RuntimeApplicationService(self.path).submit(
            CommandEnvelope(
                EntityId("command", f"start-{key}"), "start_run", run_id,
                run_id, AggregateVersion(0), f"start-{key}", "test", NOW,
                {"workflow_id": self.compiled.ir.workflow_id,
                 "workflow_version": 1,
                 "definition_hash": self.compiled.definition_hash.value},
            )
        )

    def test_foreach_identity_binds_checksum_key_index_and_plan(self):
        checksum = definition_hash([1, 2]).value
        group = derive_group_id(self.run_id, "each", checksum, Revision(1))
        self.assertEqual(
            group, derive_group_id(self.run_id, "each", checksum, Revision(1))
        )
        first = derive_item_id(group, "a", 0, checksum, Revision(1))
        self.assertNotEqual(
            first, derive_item_id(group, "a", 1, checksum, Revision(1))
        )
        self.assertNotEqual(
            first, derive_item_id(group, "a", 0, checksum, Revision(2))
        )

    def test_duplicate_foreach_keys_fail_before_any_group_write(self):
        service = ForeachService(self.path)
        with self.assertRaisesRegex(ValueError, "unique"):
            service.create_group(
                self.run_id, "each", [1, 2], keys=("x", "x"),
                plan_version=Revision(1), actor="test", now=NOW,
            )
        with connect_workflow_database(self.path, read_only=True) as connection:
            self.assertEqual(
                0, connection.execute("SELECT COUNT(*) FROM foreach_groups").fetchone()[0]
            )

    def test_scheduler_never_exceeds_group_concurrency(self):
        service = ForeachService(self.path)
        group = service.create_group(
            self.run_id, "each", range(5), plan_version=Revision(1),
            concurrency_limit=2, actor="test", now=NOW,
        )
        first = service.claim_ready(group, limit=100, actor="worker", now=NOW)
        second = service.claim_ready(group, limit=100, actor="worker", now=NOW)
        self.assertEqual(2, len(first))
        self.assertEqual((), second)
        service.complete_item(first[0], output=1, actor="worker", now=NOW)
        self.assertEqual(
            1, len(service.claim_ready(group, limit=100, actor="worker", now=NOW))
        )

    def test_fail_fast_preserves_started_items_and_cancels_only_pending(self):
        service = ForeachService(self.path)
        group = service.create_group(
            self.run_id, "each", range(4), plan_version=Revision(1),
            concurrency_limit=2, failure_policy=ForeachFailurePolicy.FAIL_FAST,
            actor="test", now=NOW,
        )
        active = service.claim_ready(group, limit=10, actor="worker", now=NOW)
        service.complete_item(active[0], error={"code": "failed"}, actor="worker", now=NOW)
        with connect_workflow_database(self.path, read_only=True) as connection:
            statuses = [
                row[0] for row in connection.execute(
                    "SELECT status FROM foreach_items WHERE group_id=? ORDER BY item_index",
                    (str(group),),
                )
            ]
        self.assertEqual(["failed", "running", "cancelled", "cancelled"], statuses)

    def test_subflow_rejects_version_drift_and_recursion_overflow(self):
        child = EntityId("run", "child")
        self._start(child, "child")
        service = SubflowService(self.path)
        with self.assertRaisesRegex(ValueError, "WorkflowVersion"):
            service.link(
                self.run_id, child,
                workflow_id=EntityId.parse(self.compiled.ir.workflow_id),
                workflow_version=Revision(2), input_mapping={}, output_mapping={},
                actor="test", now=NOW,
            )
        with self.assertRaisesRegex(ValueError, "recursion"):
            service.link(
                self.run_id, child,
                workflow_id=EntityId.parse(self.compiled.ir.workflow_id),
                workflow_version=Revision(1), input_mapping={}, output_mapping={},
                recursion_depth=MAX_SUBFLOW_DEPTH + 1, actor="test", now=NOW,
            )

    def test_dynamic_dag_cycle_and_width_limits_fail_before_plan_creation(self):
        source = {
            "dsl_version": "1.2",
            "metadata": {"id": "graph", "name": "Graph"},
            "nodes": [
                {"id": "choose", "kind": "decision", "inputs": PORT, "outputs": PORT},
                {"id": "done", "kind": "terminal", "inputs": PORT},
            ],
            "edges": [{"id": "finish", "from": {"node": "choose", "port": "value"},
                       "to": {"node": "done", "port": "value"}}],
            "entry": ["choose"], "terminals": ["done"],
        }
        compiled = compile_source(
            json.dumps(source), InMemoryHandlerCatalog([]),
            InMemorySchemaCatalog({"schema://value/1.0": {}}), source_format="json",
        )
        base = instantiate_execution_plan(
            compiled.ir, run_id=EntityId("run", "dag"),
            plan_id=EntityId("plan", "dag"), workflow_version=Revision(1),
            workflow_definition_hash=compiled.definition_hash,
        )
        edge = lambda edge_id, source_id, target_id: {
            "edge_id": edge_id, "source_node_id": source_id,
            "target_node_id": target_id, "route": "success", "priority": 0,
            "source_port": "value", "target_port": "value", "condition": None,
            "mapping": None, "back_edge": False, "policy_ref": None,
        }
        patch = PlanPatch(
            EntityId("plan_patch", "cycle"), EntityId("proposal", "cycle"),
            base.run_id, Revision(1), "cycle",
            (
                PatchOperation(PatchOperationKind.REMOVE_PENDING_EDGE, "finish"),
                PatchOperation(PatchOperationKind.ADD_EDGE, "forward", edge("forward", "choose", "done")),
                PatchOperation(PatchOperationKind.ADD_EDGE, "cycle", edge("cycle", "done", "choose")),
            ),
        )
        with self.assertRaisesRegex(PatchValidationError, "dag"):
            compile_patch(
                base, patch, AgenticRegion("r", ("choose", "done")),
                {"choose": "pending", "done": "pending"},
            )


if __name__ == "__main__":
    unittest.main()
