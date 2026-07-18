from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from orbit.workflow.application.budget_service import BudgetService
from orbit.workflow.application.human_service import HumanTaskService
from orbit.workflow.application.durable_runtime_service import DurableRuntimeApplicationService
from orbit.workflow.application.runtime_service import RuntimeApplicationService
from orbit.workflow.catalogs import HandlerManifest, InMemoryHandlerCatalog, InMemorySchemaCatalog
from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.envelopes import CommandEnvelope
from orbit.workflow.domain.human import HumanTaskKind, HumanTaskStatus
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.execution_plan import GRAPH_PLAN_SCHEMA_VERSION, GraphExecutionPlan, PlanNode
from orbit.workflow.domain.graph import PlanEdge
from orbit.workflow.domain.handlers import ResourceProfile
from orbit.workflow.domain.plan_patch import (
    PatchOperation, PatchOperationKind, PlanPatch,
)
from orbit.workflow.domain.policy import PolicyEffect, PolicyRule, evaluate_policy
from orbit.workflow.domain.schemas import validate_contract
from orbit.workflow.domain.serialization import definition_hash, to_primitive
from orbit.workflow.domain.stability import CONTRACT_STABILITY, ContractStability
from orbit.workflow.domain.versions import AggregateVersion, Revision
from orbit.workflow.dsl import compile_source
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
from orbit.workflow.handlers import ExecutionRegistry, FakeHandler


NOW = datetime(2026, 7, 18, 2, tzinfo=timezone.utc)


class Step10AgenticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "step10.db"
        source = {
            "dsl_version": "1.2",
            "metadata": {"id": "step10", "name": "Step 10"},
            "nodes": [{"id": "done", "kind": "terminal"}],
            "edges": [], "entry": ["done"], "terminals": ["done"],
        }
        compiled = compile_source(
            json.dumps(source), InMemoryHandlerCatalog([]),
            InMemorySchemaCatalog({}), source_format="json",
        )
        SQLiteWorkflowVersionStore(self.path).publish(
            compiled, expected_latest_version=0, source_format="json",
            source_text=None, actor="test",
        )
        self.run_id = EntityId("run", "step10")
        RuntimeApplicationService(self.path).submit(
            CommandEnvelope(
                EntityId("command", "step10-start"), "start_run",
                self.run_id, self.run_id, AggregateVersion(0), "start",
                "test", NOW,
                {"workflow_id": compiled.ir.workflow_id, "workflow_version": 1,
                 "definition_hash": compiled.definition_hash.value},
            )
        )

    def test_plan_patch_stable_upgrade_has_schema_and_canonical_hash(self):
        patch = PlanPatch(
            EntityId("plan_patch", "stable"), EntityId("proposal", "stable"),
            self.run_id, Revision(1), "remove pending edge",
            (PatchOperation(PatchOperationKind.REMOVE_PENDING_EDGE, "edge"),),
        )
        validate_contract(to_primitive(patch), "plan-patch/1.0")
        self.assertIs(ContractStability.STABLE, CONTRACT_STABILITY["plan_patch"])
        self.assertEqual(patch.content_hash, PlanPatch(
            patch.patch_id, patch.proposal_id, patch.run_id,
            patch.base_plan_version, patch.reason, patch.operations,
        ).content_hash)

    def test_policy_deny_precedence_and_missing_rule_fail_closed(self):
        rules = (
            PolicyRule("allow", "1", "write", PolicyEffect.ALLOW),
            PolicyRule("deny", "1", "write", PolicyEffect.DENY),
        )
        denied = evaluate_policy(
            run_id=self.run_id, patch_id=EntityId("plan_patch", "policy"),
            required_capabilities=("write", "undeclared"), rules=rules,
        )
        self.assertFalse(denied.allowed)
        self.assertIn("explicit deny for write", denied.reasons)
        self.assertIn("missing allow rule for undeclared", denied.reasons)

    def test_budget_add_is_service_level_idempotent(self):
        service = BudgetService(self.path)
        service.open_account(self.run_id, 100, actor="owner", now=NOW)
        first = service.add_budget(
            self.run_id, 50, actor="owner", now=NOW,
            idempotency_key="api-command-1",
        )
        second = service.add_budget(
            self.run_id, 50, actor="owner", now=NOW + timedelta(seconds=10),
            idempotency_key="api-command-1",
        )
        self.assertEqual(first, second)
        self.assertEqual(150, second.total_microunits)
        with connect_workflow_database(self.path, read_only=True) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM budget_ledger_entries WHERE kind='budget_added'"
            ).fetchone()[0]
        self.assertEqual(1, count)
        with self.assertRaisesRegex(ValueError, "different amount"):
            service.add_budget(
                self.run_id, 51, actor="owner", now=NOW,
                idempotency_key="api-command-1",
            )

    def test_concurrent_reservations_do_not_oversell(self):
        service = BudgetService(self.path)
        service.open_account(self.run_id, 100, actor="owner", now=NOW)

        def reserve(name):
            try:
                service.reserve(
                    self.run_id, EntityId("attempt", name), 80,
                    actor="worker", now=NOW,
                )
                return "ok"
            except ValueError:
                return "denied"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = sorted(pool.map(reserve, ("one", "two")))
        self.assertEqual(["denied", "ok"], results)
        with connect_workflow_database(self.path, read_only=True) as connection:
            reserved = connection.execute(
                "SELECT reserved_microunits FROM budget_accounts WHERE run_id=?",
                (str(self.run_id),),
            ).fetchone()[0]
        self.assertEqual(80, reserved)

    def test_usage_sequence_conflict_and_actual_overrun_are_recorded(self):
        service = BudgetService(self.path)
        service.open_account(self.run_id, 100, actor="owner", now=NOW)
        reservation = service.reserve(
            self.run_id, EntityId("attempt", "usage"), 80,
            actor="worker", now=NOW,
        )
        service.report_usage(
            reservation.reservation_id, 1, 40, actor="worker", now=NOW,
        )
        with self.assertRaisesRegex(ValueError, "same usage sequence"):
            service.report_usage(
                reservation.reservation_id, 1, 41, actor="worker", now=NOW,
            )
        account = service.report_usage(
            reservation.reservation_id, 2, 140, actor="worker", now=NOW,
        )
        self.assertTrue(account.exhausted)
        self.assertEqual(140, account.consumed_microunits)

    def test_human_expected_version_allows_only_one_race_winner(self):
        service = HumanTaskService(self.path)
        task_id, token = service.create(
            self.run_id, HumanTaskKind.APPROVAL, {"operation": "deploy"},
            actor="planner", now=NOW,
        )

        def submit(actor):
            try:
                return service.submit(
                    task_id, token, "approve", {}, actor=actor,
                    expected_version=1, now=NOW,
                ).value
            except ValueError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = sorted(pool.map(submit, ("alice", "bob")))
        self.assertEqual(["completed", "conflict"], results)

    def test_external_write_materialization_requires_exact_node_approval(self):
        profile = ResourceProfile(10, 10, 1, 30, 100, "test")
        manifest = HandlerManifest(
            "writer", "1.0.0", ("action",), {}, {}, {"type": "object"},
            ExecutionSafety.UNKNOWN_ON_LEASE_LOSS, profile,
            "schema://result/1.0", ("write:production",), (), True, True,
        )
        registry = ExecutionRegistry()
        registry.register(manifest, FakeHandler(), implementation_id="writer.v1")
        registry.seal()
        service = DurableRuntimeApplicationService(
            self.path, execution_registry=registry,
        )
        action = PlanNode(
            "write", "action", manifest.name, manifest.version,
            manifest.fingerprint, (), (), {"capabilities": ["write:production"]},
        )
        terminal = PlanNode("done", "terminal", None, None, None, (), (), {})
        plan = GraphExecutionPlan(
            GRAPH_PLAN_SCHEMA_VERSION, EntityId("plan", "approval"), self.run_id,
            Revision(1), EntityId("workflow", "step10"), Revision(1),
            definition_hash("workflow"), ("write",), ("done",),
            ("write", "done"), (action, terminal),
            (PlanEdge("finish", "write", "done"),),
            {"write": ("finish",), "done": ()},
            {"write": (), "done": ("finish",)}, {},
        )
        node_run_id = EntityId("node_run", "approval")
        node_run = SimpleNamespace(
            run_id=self.run_id, node_run_id=node_run_id,
            source_plan_version=Revision(1), node_id="write",
        )

        class Plans:
            @staticmethod
            def get(run_id, version):
                return SimpleNamespace(plan=to_primitive(plan))

        with connect_workflow_database(self.path) as connection:
            uow = SimpleNamespace(plans=Plans(), connection=connection)
            with self.assertRaisesRegex(PermissionError, "approval required"):
                service._guard_materialization(uow, node_run, NOW)

        human = HumanTaskService(self.path)
        task_id, token = human.create_node_approval(
            self.run_id, node_run_id, "write:production", 1,
            actor="planner", now=NOW,
        )
        human.submit(
            task_id, token, "approve", {}, actor="approver",
            expected_version=1, now=NOW,
        )
        with connect_workflow_database(self.path) as connection:
            uow = SimpleNamespace(plans=Plans(), connection=connection)
            service._guard_materialization(uow, node_run, NOW)


if __name__ == "__main__":
    unittest.main()
