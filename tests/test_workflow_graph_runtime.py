from __future__ import annotations

from datetime import datetime, timezone
import itertools
import json
from pathlib import Path
import tempfile
import unittest

from orbit.workflow.application.runtime_service import RuntimeApplicationService
from orbit.workflow.catalogs import HandlerManifest, InMemoryHandlerCatalog, InMemorySchemaCatalog
from orbit.workflow.application.durable_runtime_service import DurableRuntimeApplicationService
from orbit.workflow.application.human_delivery import InMemoryHumanTaskDelivery
from orbit.workflow.application.planner_service import PlannerApplicationService
from orbit.workflow.application.plan_service import PlanService
from orbit.workflow.application.budget_service import BudgetService
from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.data import derive_artifact_id
from orbit.workflow.domain.handlers import ResourceProfile
from orbit.workflow.domain.envelopes import CommandEnvelope
from orbit.workflow.domain.execution_plan import GraphExecutionPlan, execution_plan_from_primitive
from orbit.workflow.domain.graph import (
    EdgeRoute, JoinMergeMode, JoinMode, JoinPolicy,
)
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.serialization import definition_hash, to_primitive
from orbit.workflow.domain.states import BranchTokenStatus, WorkflowRunStatus
from orbit.workflow.domain.versions import AggregateVersion, Revision
from orbit.workflow.dsl import DiagnosticError, compile_source
from orbit.workflow.graph.conditions import evaluate_condition
from orbit.workflow.graph.joins import JoinTokenFact, evaluate_join
from orbit.workflow.graph.routing import evaluate_route
from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
from orbit.workflow.persistence.integrity import check_database
from orbit.workflow.persistence.uow import SQLiteUnitOfWork
from orbit.workflow.persistence.memory import MemoryRuntimeDatabase, MemoryUnitOfWork
from orbit.workflow.planner import FakePlannerProvider, PlannerProviderResponse
from orbit.workflow.domain.planner import PlannerUsage
from orbit.workflow.runtime.kernel import RuntimeKernel
from orbit.workflow.runtime.plan_instantiator import instantiate_execution_plan
from orbit.workflow.handlers import ExecutionRegistry, HandlerExecutor, TransformHandler
from orbit.workflow.testing import assert_reducer_source_is_pure, side_effect_guard
from orbit.workflow.worker.runtime import (
    ForeachReconciler, PlannerDispatcher, PlannerProposalReconciler,
    SubflowReconciler, WorkerRuntime,
)


NOW = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
PORT = lambda name: [{"id": name, "schema_id": "schema://value/1.0"}]
ARTIFACT_PORT = lambda name: [{
    "id": name, "schema_id": "schema://value/1.0",
    "transport": "artifact_ref", "max_size_bytes": 4096,
    "content_types": ["application/json"], "visibility": "run",
}]


def compile_graph(value):
    return compile_source(
        json.dumps(value), InMemoryHandlerCatalog([]),
        InMemorySchemaCatalog({"schema://value/1.0": {}}),
        source_format="json",
    )


def decision_graph(*, parallel=False):
    return {
        "dsl_version": "1.2", "metadata": {"id": "decision_graph", "name": "Decision"},
        "nodes": [
            {"id": "choose", "kind": "decision", "inputs": PORT("value"), "outputs": PORT("value"), "route_mode": "parallel" if parallel else "exclusive"},
            {"id": "yes", "kind": "terminal", "inputs": PORT("value")},
            {"id": "no", "kind": "terminal", "inputs": PORT("value")},
        ],
        "edges": [
            {"id": "yes_edge", "from": {"node": "choose", "port": "value"}, "to": {"node": "yes", "port": "value"}, "condition": "source.value == 1", "priority": 0},
            {"id": "no_edge", "from": {"node": "choose", "port": "value"}, "to": {"node": "no", "port": "value"}, "condition": "source.value != 1", "priority": 1},
        ],
        "entry": ["choose"], "terminals": ["yes", "no"],
    }


class PureGraphDecisionTests(unittest.TestCase):
    def test_condition_evaluator_is_pure_and_bounded(self):
        expression = {
            "op": "and", "args": [
                {"op": "eq", "left": {"op": "ref", "path": "source.value"}, "right": {"op": "literal", "value": 3}},
                {"op": "call", "name": "exists", "args": [{"op": "ref", "path": "workflow.inputs.goal"}]},
            ],
        }
        assert_reducer_source_is_pure(evaluate_condition)
        with side_effect_guard():
            self.assertTrue(evaluate_condition(expression, {"value": 3}, workflow_inputs={"goal": "ok"}))

    def test_execution_plan_1_2_round_trips_and_routes_stably(self):
        compiled = compile_graph(decision_graph())
        plan = instantiate_execution_plan(
            compiled.ir, run_id=EntityId("run", "route"),
            plan_id=EntityId("plan", "route"), workflow_version=Revision(1),
            workflow_definition_hash=compiled.definition_hash,
        )
        self.assertIsInstance(plan, GraphExecutionPlan)
        self.assertEqual(plan, execution_plan_from_primitive(to_primitive(plan)))
        decision = evaluate_route(
            plan, EntityId("node_run", "choose"), "choose", EdgeRoute.SUCCESS,
            {"value": 1},
        )
        self.assertEqual(("yes_edge",), decision.selected_edge_ids)
        self.assertEqual(("no_edge",), decision.not_selected_edge_ids)

    def test_exclusive_default_is_validated_and_evaluated_as_fallback(self):
        for default in (None, True, "True", {"op": "literal", "value": True}):
            with self.subTest(default=default):
                value = decision_graph()
                if default is None:
                    value["edges"][1].pop("condition")
                else:
                    value["edges"][1]["condition"] = default
                value["edges"][0]["priority"] = 10
                value["edges"][1]["priority"] = 0
                with self.assertRaises(DiagnosticError) as caught:
                    compile_graph(value)
                self.assertIn(
                    "default edge must sort after every conditional edge",
                    " ".join(item.message for item in caught.exception.diagnostics),
                )

        value = decision_graph()
        value["edges"][1].pop("condition")
        value["edges"][0]["priority"] = 10
        value["edges"][1]["priority"] = 20
        compiled = compile_graph(value)
        plan = instantiate_execution_plan(
            compiled.ir, run_id=EntityId("run", "fallback"),
            plan_id=EntityId("plan", "fallback"), workflow_version=Revision(1),
            workflow_definition_hash=compiled.definition_hash,
        )
        matched = evaluate_route(
            plan, EntityId("node_run", "fallback-yes"), "choose",
            EdgeRoute.SUCCESS, {"value": 1},
        )
        fallback = evaluate_route(
            plan, EntityId("node_run", "fallback-no"), "choose",
            EdgeRoute.SUCCESS, {"value": 2},
        )
        self.assertEqual(("yes_edge",), matched.selected_edge_ids)
        self.assertEqual(("no_edge",), fallback.selected_edge_ids)

    def test_join_winner_and_merge_do_not_depend_on_arrival_order(self):
        facts = (
            JoinTokenFact("slow-high", 0, BranchTokenStatus.COMPLETED, {"v": 1}),
            JoinTokenFact("fast-low", 10, BranchTokenStatus.COMPLETED, {"v": 2}),
        )
        results = set()
        for permutation in itertools.permutations(facts):
            decision, merged = evaluate_join(
                EntityId("join_group", "g"),
                JoinPolicy(JoinMode.ANY, JoinMergeMode.FIRST_BY_PRIORITY),
                permutation,
            )
            results.add((decision.winner_edge_ids, json.dumps(to_primitive(merged), sort_keys=True)))
        self.assertEqual({(("slow-high",), '{"v": 1}')}, results)


class GraphRuntimeE2ETests(unittest.TestCase):
    def test_foreach_materializes_bounded_child_runs_and_aggregates(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "foreach.db"
        manifest = HandlerManifest(
            name="identity", version="1.0.0", node_kinds=("action",),
            inputs={"value": "schema://value/1.0"},
            outputs={"value": "schema://value/1.0"},
            config_schema={"type": "object", "additionalProperties": False},
            execution_safety=ExecutionSafety.REPLAY_SAFE,
            resource_profile=ResourceProfile(0, 0, 0, 60, 0, "free"),
            result_schema_id="schema://value/1.0",
        )
        child = compile_source(
            json.dumps({
                "dsl_version": "1.2", "metadata": {"id": "each_child", "name": "Each child"},
                "nodes": [
                    {"id": "work", "kind": "action", "inputs": PORT("value"),
                     "outputs": PORT("value"),
                     "handler": {"name": "identity", "version": "1.0.0"}},
                    {"id": "done", "kind": "terminal", "inputs": PORT("value")},
                ],
                "edges": [{"id": "done", "from": {"node": "work", "port": "value"},
                           "to": {"node": "done", "port": "value"}}],
                "entry": ["work"], "terminals": ["done"],
            }),
            InMemoryHandlerCatalog([manifest]),
            InMemorySchemaCatalog({"schema://value/1.0": {}}), source_format="json",
        )
        store = SQLiteWorkflowVersionStore(path)
        store.publish(child, expected_latest_version=0, source_format="json", source_text=None, actor="test")
        parent = compile_graph({
            "dsl_version": "1.2", "metadata": {"id": "each_parent", "name": "Each parent"},
            "nodes": [
                {"id": "each", "kind": "foreach", "inputs": PORT("items"),
                 "outputs": PORT("results"), "config": {
                     "workflow_id": str(child.ir.workflow_id), "workflow_version": 1,
                     "definition_hash": child.definition_hash.value,
                     "items_port": "items", "item_port": "value",
                     "result_port": "value", "output_port": "results",
                     "concurrency_limit": 2, "failure_policy": "fail_fast",
                     "item_budget_microunits": 100,
                 }},
                {"id": "done", "kind": "terminal", "inputs": PORT("results")},
            ],
            "edges": [{"id": "done", "from": {"node": "each", "port": "results"},
                       "to": {"node": "done", "port": "results"}}],
            "entry": ["each"], "terminals": ["done"],
        })
        store.publish(parent, expected_latest_version=0, source_format="json", source_text=None, actor="test")
        registry = ExecutionRegistry()
        registry.register(manifest, TransformHandler(), implementation_id="identity.v1")
        registry.seal()
        service = DurableRuntimeApplicationService(path, execution_registry=registry)
        executor = HandlerExecutor(
            registry, InMemorySchemaCatalog({"schema://value/1.0": {}}), clock=lambda: NOW,
        )
        run_id = EntityId("run", "foreach-parent")
        started = service.submit(CommandEnvelope(
            EntityId("command", "foreach-start"), "start_run", run_id, run_id,
            AggregateVersion(0), "foreach-start", "test", NOW,
            {"workflow_id": parent.ir.workflow_id, "workflow_version": 1,
             "definition_hash": parent.definition_hash.value,
             "input": {"items": [3, 1, 2]}},
        ))
        self.assertEqual("applied", started.disposition.value, started.diagnostics)
        reconciler = ForeachReconciler(service, clock=lambda: NOW)
        self.assertTrue(reconciler.run_once())
        with service.uow_factory() as uow:
            counts = dict(uow.connection.execute(
                "SELECT status,COUNT(*) count FROM foreach_items GROUP BY status"
            ).fetchall())
            self.assertEqual({"pending": 1, "running": 2}, counts)
            child_ids = [EntityId.parse(row[0]) for row in uow.connection.execute(
                "SELECT child_run_id FROM foreach_items WHERE status='running' ORDER BY item_index"
            )]
        budget = BudgetService(path)
        parent_account = budget.get_account(run_id)
        self.assertEqual(300, parent_account.total_microunits)
        self.assertEqual(200, parent_account.reserved_microunits)
        child_usage = budget.reserve(
            child_ids[0], EntityId("attempt", "foreach-cost"), 50,
            actor="worker", now=NOW,
        )
        budget.report_usage(
            child_usage.reservation_id, 1, 30, actor="worker", now=NOW,
        )
        budget.settle(child_usage.reservation_id, actor="worker", now=NOW)
        service = DurableRuntimeApplicationService(path, execution_registry=registry)
        reconciler = ForeachReconciler(service, clock=lambda: NOW)
        worker = WorkerRuntime(service, executor, clock=lambda: NOW)
        self.assertTrue(worker.run_once())
        self.assertTrue(worker.run_once())
        self.assertTrue(reconciler.run_once())
        self.assertTrue(worker.run_once())
        self.assertTrue(reconciler.run_once())
        self.assertIs(WorkflowRunStatus.SUCCEEDED, service.get_run(run_id).status)
        with service.uow_factory() as uow:
            group = uow.connection.execute("SELECT * FROM foreach_groups").fetchone()
            aggregate = json.loads(group["aggregate_json"])
            self.assertEqual("completed", group["status"])
            self.assertEqual([3, 1, 2], [item["output"] for item in aggregate["items"]])
        parent_account = budget.get_account(run_id)
        self.assertEqual(30, parent_account.consumed_microunits)
        self.assertEqual(0, parent_account.reserved_microunits)
        replayed = service.recovery.rehydrate(run_id)
        self.assertEqual("completed", next(iter(replayed.state["foreach"].values()))["status"])

        cancelled_run = EntityId("run", "foreach-cancelled")
        service.submit(CommandEnvelope(
            EntityId("command", "foreach-cancel-start"), "start_run",
            cancelled_run, cancelled_run, AggregateVersion(0),
            "foreach-cancel-start", "test", NOW,
            {"workflow_id": parent.ir.workflow_id, "workflow_version": 1,
             "definition_hash": parent.definition_hash.value,
             "input": {"items": [4, 5]}},
        ))
        self.assertTrue(reconciler.run_once())
        waiting = service.get_run(cancelled_run)
        cancelled = service.submit(CommandEnvelope.create(
            command_type="cancel_run", aggregate_id=cancelled_run,
            correlation_id=cancelled_run,
            expected_version=waiting.aggregate_version,
            idempotency_key="foreach-cancel", actor="test", issued_at=NOW,
            payload={"reason": "test"},
        ))
        self.assertEqual("applied", cancelled.disposition.value)
        cancelled_account = budget.get_account(cancelled_run)
        self.assertEqual(0, cancelled_account.reserved_microunits)
        with service.uow_factory() as uow:
            self.assertEqual(
                {"cancelled"},
                {row[0] for row in uow.connection.execute(
                    "SELECT status FROM foreach_items WHERE run_id=?",
                    (str(cancelled_run),),
                )},
            )

        exhausted_run = EntityId("run", "foreach-budget-exhausted")
        service.submit(CommandEnvelope(
            EntityId("command", "foreach-exhausted-start"), "start_run",
            exhausted_run, exhausted_run, AggregateVersion(0),
            "foreach-exhausted-start", "test", NOW,
            {"workflow_id": parent.ir.workflow_id, "workflow_version": 1,
             "definition_hash": parent.definition_hash.value,
             "input": {"items": [6]}, "budget_microunits": 50},
        ))
        self.assertTrue(reconciler.run_once())
        self.assertIs(
            WorkflowRunStatus.BUDGET_EXHAUSTED,
            service.get_run(exhausted_run).status,
        )
        exhausted_account = budget.get_account(exhausted_run)
        self.assertEqual(0, exhausted_account.reserved_microunits)
        budget.add_budget(
            exhausted_run, 100,
            expected_version=exhausted_account.version.value,
            actor="test", now=NOW, idempotency_key="foreach-top-up",
        )
        self.assertIs(WorkflowRunStatus.RUNNING, service.get_run(exhausted_run).status)
        self.assertTrue(reconciler.run_once())
        self.assertEqual(100, budget.get_account(exhausted_run).reserved_microunits)

    def test_published_subflow_starts_links_and_resumes_from_child_terminal(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "subflow.db"
        child_dsl = {
            "dsl_version": "1.2",
            "metadata": {"id": "child", "name": "Child"},
            "nodes": [
                {"id": "child_done", "kind": "terminal", "inputs": PORT("value")},
            ],
            "edges": [], "entry": ["child_done"], "terminals": ["child_done"],
        }
        child = compile_graph(child_dsl)
        store = SQLiteWorkflowVersionStore(path)
        store.publish(
            child, expected_latest_version=0, source_format="json",
            source_text=None, actor="test",
        )
        parent_dsl = {
            "dsl_version": "1.2",
            "metadata": {"id": "parent", "name": "Parent"},
            "nodes": [
                {
                    "id": "call", "kind": "subflow", "inputs": PORT("value"),
                    "outputs": PORT("value"),
                    "config": {
                        "workflow_id": str(child.ir.workflow_id),
                        "workflow_version": 1,
                        "definition_hash": child.definition_hash.value,
                    },
                },
                {"id": "done", "kind": "terminal", "inputs": PORT("value")},
            ],
            "edges": [
                {"id": "child_result", "from": {"node": "call", "port": "value"},
                 "to": {"node": "done", "port": "value"}},
            ],
            "entry": ["call"], "terminals": ["done"],
        }
        parent = compile_graph(parent_dsl)
        store.publish(
            parent, expected_latest_version=0, source_format="json",
            source_text=None, actor="test",
        )
        service = DurableRuntimeApplicationService(path)
        parent_run_id = EntityId("run", "parent-subflow")
        result = service.submit(CommandEnvelope(
            EntityId("command", "parent-subflow-start"), "start_run",
            parent_run_id, parent_run_id, AggregateVersion(0),
            "parent-subflow-start", "test", NOW,
            {"workflow_id": parent.ir.workflow_id, "workflow_version": 1,
             "definition_hash": parent.definition_hash.value,
             "input": {"value": 7}},
        ))
        self.assertEqual("applied", result.disposition.value, result.diagnostics)
        self.assertIs(WorkflowRunStatus.WAITING, service.get_run(parent_run_id).status)
        self.assertTrue(SubflowReconciler(service, clock=lambda: NOW).run_once())
        self.assertIs(WorkflowRunStatus.SUCCEEDED, service.get_run(parent_run_id).status)
        with service.uow_factory() as uow:
            link = uow.connection.execute(
                "SELECT * FROM subflow_links WHERE parent_run_id=?",
                (str(parent_run_id),),
            ).fetchone()
            self.assertEqual("succeeded", link["status"])
            self.assertEqual("node_run", EntityId.parse(link["parent_node_run_id"]).kind)
            self.assertIs(
                WorkflowRunStatus.SUCCEEDED,
                uow.runs.get(EntityId.parse(link["child_run_id"])).status,
            )

    def test_subflow_transfers_artifact_acl_and_returns_artifact_reference(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "subflow-artifact.db"
        child = compile_graph({
            "dsl_version": "1.2",
            "metadata": {"id": "artifact_child", "name": "Artifact child"},
            "nodes": [{
                "id": "child_done", "kind": "terminal",
                "inputs": ARTIFACT_PORT("report"),
            }],
            "edges": [], "entry": ["child_done"], "terminals": ["child_done"],
        })
        store = SQLiteWorkflowVersionStore(path)
        store.publish(
            child, expected_latest_version=0, source_format="json",
            source_text=None, actor="test",
        )
        parent = compile_graph({
            "dsl_version": "1.2",
            "metadata": {"id": "artifact_parent", "name": "Artifact parent"},
            "nodes": [
                {
                    "id": "call", "kind": "subflow",
                    "inputs": ARTIFACT_PORT("report"),
                    "outputs": ARTIFACT_PORT("report"),
                    "config": {
                        "workflow_id": str(child.ir.workflow_id),
                        "workflow_version": 1,
                        "definition_hash": child.definition_hash.value,
                    },
                },
                {"id": "done", "kind": "terminal", "inputs": ARTIFACT_PORT("report")},
            ],
            "edges": [{
                "id": "returned", "from": {"node": "call", "port": "report"},
                "to": {"node": "done", "port": "report"},
            }],
            "entry": ["call"], "terminals": ["done"],
        })
        store.publish(
            parent, expected_latest_version=0, source_format="json",
            source_text=None, actor="test",
        )
        service = DurableRuntimeApplicationService(path)
        run_id = EntityId("run", "subflow-artifact-parent")
        artifact_id = derive_artifact_id(run_id, "report", "report")
        checksum = "sha256:" + "a" * 64
        artifact_ref = {
            "artifact_id": str(artifact_id), "schema_id": "schema://value/1.0",
            "content_type": "application/json", "checksum": checksum,
            "size_bytes": 2,
        }
        started = service.submit(CommandEnvelope(
            EntityId("command", "subflow-artifact-start"), "start_run",
            run_id, run_id, AggregateVersion(0), "subflow-artifact-start",
            "alice", NOW,
            {
                "workflow_id": parent.ir.workflow_id, "workflow_version": 1,
                "definition_hash": parent.definition_hash.value,
                "input": {"report": artifact_ref},
                "artifact_inputs": [{
                    "port_id": "report", **artifact_ref,
                    "blob_key": checksum,
                }],
            },
        ))
        self.assertEqual("applied", started.disposition.value, started.diagnostics)
        reconciler = SubflowReconciler(service, clock=lambda: NOW)
        self.assertTrue(reconciler.run_once())
        self.assertIs(WorkflowRunStatus.SUCCEEDED, service.get_run(run_id).status)
        with service.uow_factory() as uow:
            link = uow.connection.execute("SELECT * FROM subflow_links").fetchone()
            self.assertEqual([str(artifact_id)], json.loads(link["artifact_scope_json"]))
            child_subject = uow.connection.execute(
                "SELECT role FROM run_artifact_subjects WHERE run_id=? AND subject='alice'",
                (link["child_run_id"],),
            ).fetchone()
            self.assertEqual("participant", child_subject["role"])
            self.assertIsNotNone(uow.connection.execute(
                "SELECT 1 FROM artifact_acl WHERE artifact_id=? AND subject='alice' AND permission='read'",
                (str(artifact_id),),
            ).fetchone())
            actions = {
                row[0] for row in uow.connection.execute(
                    "SELECT action FROM audit_records WHERE run_id=?",
                    (str(run_id),),
                )
            }
            self.assertIn("subflow.artifact_acl_transfer", actions)
            self.assertIn("subflow.artifact_acl_return", actions)
        denied_child = EntityId("run", "subflow-artifact-denied")
        denied = service.submit(CommandEnvelope.create(
            command_type="start_run", aggregate_id=denied_child,
            correlation_id=run_id, expected_version=AggregateVersion(0),
            idempotency_key="subflow-artifact-denied", actor="system:subflow",
            issued_at=NOW,
            payload={
                "workflow_id": str(child.ir.workflow_id), "workflow_version": 1,
                "definition_hash": child.definition_hash.value,
                "input": {"report": artifact_ref},
                "artifact_subjects": ["mallory"],
                "artifact_scope": [str(artifact_id)],
            },
        ))
        self.assertEqual("rejected", denied.disposition.value)
        self.assertIn("expand subject authority", denied.diagnostics[0].message)
        self.assertIsNone(service.get_run(denied_child))

    def test_parent_cancel_propagates_to_waiting_subflow_child(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "subflow-cancel.db"
        child = compile_graph({
            "dsl_version": "1.2", "metadata": {"id": "waiting_child", "name": "Waiting child"},
            "nodes": [
                {"id": "approval", "kind": "human", "inputs": PORT("value"),
                 "outputs": PORT("result"),
                 "config": {"task_kind": "approval", "participants": ["reviewer"]}},
                {"id": "done", "kind": "terminal", "inputs": PORT("result")},
            ],
            "edges": [{"id": "approved", "from": {"node": "approval", "port": "result"},
                       "to": {"node": "done", "port": "result"}}],
            "entry": ["approval"], "terminals": ["done"],
        })
        store = SQLiteWorkflowVersionStore(path)
        store.publish(child, expected_latest_version=0, source_format="json", source_text=None, actor="test")
        parent = compile_graph({
            "dsl_version": "1.2", "metadata": {"id": "cancel_parent", "name": "Cancel parent"},
            "nodes": [
                {"id": "call", "kind": "subflow", "inputs": PORT("value"),
                 "outputs": PORT("result"), "config": {
                     "workflow_id": str(child.ir.workflow_id), "workflow_version": 1,
                     "definition_hash": child.definition_hash.value,
                     "parent_cancel_to_child": True,
                 }},
                {"id": "done", "kind": "terminal", "inputs": PORT("result")},
            ],
            "edges": [{"id": "done", "from": {"node": "call", "port": "result"},
                       "to": {"node": "done", "port": "result"}}],
            "entry": ["call"], "terminals": ["done"],
        })
        store.publish(parent, expected_latest_version=0, source_format="json", source_text=None, actor="test")
        service = DurableRuntimeApplicationService(path)
        parent_run_id = EntityId("run", "cancel-parent")
        started = service.submit(CommandEnvelope(
            EntityId("command", "cancel-parent-start"), "start_run",
            parent_run_id, parent_run_id, AggregateVersion(0), "cancel-parent-start",
            "test", NOW, {"workflow_id": parent.ir.workflow_id, "workflow_version": 1,
                          "definition_hash": parent.definition_hash.value,
                          "input": {"value": 1}},
        ))
        self.assertEqual("applied", started.disposition.value, started.diagnostics)
        current = service.get_run(parent_run_id)
        cancelled = service.submit(CommandEnvelope(
            EntityId("command", "cancel-parent-now"), "cancel_run",
            parent_run_id, parent_run_id, current.aggregate_version,
            "cancel-parent-now", "test", NOW, {"reason": "stop"},
        ))
        self.assertEqual("applied", cancelled.disposition.value, cancelled.diagnostics)
        with service.uow_factory() as uow:
            link = uow.connection.execute(
                "SELECT * FROM subflow_links WHERE parent_run_id=?", (str(parent_run_id),)
            ).fetchone()
            self.assertEqual("cancelled", link["status"])
            self.assertIs(
                WorkflowRunStatus.CANCELLED,
                uow.runs.get(EntityId.parse(link["child_run_id"])).status,
            )
            task = uow.connection.execute(
                "SELECT status FROM human_tasks WHERE run_id=?", (link["child_run_id"],)
            ).fetchone()
            self.assertEqual("cancelled", task["status"])

    def test_planner_dispatch_replaces_placeholder_and_worker_executes_plan_v2(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "agentic-dispatch.db"
        dsl = {
            "dsl_version": "1.2",
            "metadata": {"id": "agentic_dispatch", "name": "Agentic dispatch"},
            "nodes": [
                {
                    "id": "plan", "kind": "agentic", "inputs": PORT("value"),
                    "outputs": PORT("value"),
                    "config": {
                        "model_id": "fake", "provider_id": "fake",
                        "capabilities": ["transform"],
                        "remaining_limits": {"decisions": 2, "cost_microunits": 100},
                        "mutable_nodes": ["work"],
                    },
                },
                {
                    "id": "work", "kind": "decision", "inputs": PORT("value"),
                    "outputs": PORT("value"),
                },
                {"id": "done", "kind": "terminal", "inputs": PORT("value")},
            ],
            "edges": [
                {"id": "planned", "from": {"node": "plan", "port": "value"},
                 "to": {"node": "work", "port": "value"}},
                {"id": "finished", "from": {"node": "work", "port": "value"},
                 "to": {"node": "done", "port": "value"}},
            ],
            "entry": ["plan"], "terminals": ["done"],
        }
        compiled = compile_graph(dsl)
        SQLiteWorkflowVersionStore(path).publish(
            compiled, expected_latest_version=0, source_format="json",
            source_text=None, actor="test",
        )
        manifest = HandlerManifest(
            name="transform", version="1.0.0", node_kinds=("action",),
            inputs={"value": "schema://value/1.0"},
            outputs={"value": "schema://value/1.0"},
            config_schema={
                "type": "object", "additionalProperties": False,
                "properties": {"operation": {"const": "identity"}},
            },
            execution_safety=ExecutionSafety.REPLAY_SAFE,
            resource_profile=ResourceProfile(0, 0, 0, 60, 0, "free"),
            result_schema_id="schema://value/1.0", capabilities=("transform",),
        )
        registry = ExecutionRegistry()
        registry.register(
            manifest, TransformHandler(), implementation_id="builtin.transform.v1",
        )
        registry.seal()
        run_id = EntityId("run", "agentic-dispatch")
        raw = json.dumps({
            "schema_version": "1.0", "proposal_id": "proposal:dispatch",
            "run_id": str(run_id), "base_plan_version": 1,
            "action": {"kind": "dispatch", "arguments": {
                "handler": "transform@1.0.0", "inputs": {"value": 2},
                "config": {"operation": "identity"},
            }},
            "reason": "execute the mutable work slot",
        })
        planner = PlannerApplicationService(
            path, provider=FakePlannerProvider([
                PlannerProviderResponse(raw, "request:dispatch", PlannerUsage())
            ]),
        )
        service = DurableRuntimeApplicationService(
            path, execution_registry=registry, planner_service=planner,
        )
        started = service.submit(CommandEnvelope(
            EntityId("command", "agentic-dispatch-start"), "start_run",
            run_id, run_id, AggregateVersion(0), "agentic-dispatch-start",
            "test", NOW,
            {"workflow_id": compiled.ir.workflow_id, "workflow_version": 1,
             "definition_hash": compiled.definition_hash.value,
             "input": {"value": 1}, "goal": "transform the value"},
        ))
        self.assertEqual("applied", started.disposition.value, started.diagnostics)
        self.assertTrue(PlannerDispatcher(planner, clock=lambda: NOW).run_once())
        reconciler = PlannerProposalReconciler(
            planner, service, clock=lambda: NOW, execution_registry=registry,
            plan_service_factory=lambda **options: PlanService(path, **options),
        )
        self.assertTrue(reconciler.run_once())
        with SQLiteUnitOfWork(path) as uow:
            plan_v2 = execution_plan_from_primitive(to_primitive(uow.plans.get(run_id, Revision(2)).plan))
            self.assertEqual("action", plan_v2.node("work").kind)
            work = next(item for item in uow.node_runs.list_by_run(run_id) if item.node_id == "work")
            self.assertEqual(Revision(2), work.source_plan_version)
            policy = uow.connection.execute(
                "SELECT allowed,results_json FROM policy_decisions"
            ).fetchone()
            self.assertEqual(1, policy["allowed"])
            self.assertIn("agentic:plan:transform", policy["results_json"])
        executor = HandlerExecutor(
            registry, InMemorySchemaCatalog({"schema://value/1.0": {}}),
            clock=lambda: NOW,
        )
        self.assertTrue(WorkerRuntime(service, executor, clock=lambda: NOW).run_once())
        self.assertIs(WorkflowRunStatus.SUCCEEDED, service.get_run(run_id).status)
        self.assertEqual("consumed", planner.list_proposals(run_id)[0].status.value)

    def test_published_agentic_node_creates_a_durable_planner_request(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "agentic.db"
        dsl = {
            "dsl_version": "1.2",
            "metadata": {"id": "agentic", "name": "Agentic"},
            "nodes": [
                {"id": "plan", "kind": "agentic", "inputs": PORT("value"),
                 "outputs": PORT("value"),
                 "config": {"model_id": "fake", "provider_id": "fake",
                            "capabilities": ["finish"],
                            "remaining_limits": {"decisions": 2, "cost_microunits": 100}}},
                {"id": "done", "kind": "terminal", "inputs": PORT("value")},
            ],
            "edges": [{"id": "planned", "from": {"node": "plan", "port": "value"},
                       "to": {"node": "done", "port": "value"}}],
            "entry": ["plan"], "terminals": ["done"],
        }
        compiled = compile_graph(dsl)
        SQLiteWorkflowVersionStore(path).publish(
            compiled, expected_latest_version=0, source_format="json",
            source_text=None, actor="test",
        )
        raw = json.dumps({
            "schema_version": "1.0", "proposal_id": "proposal:finish",
            "run_id": str(EntityId("run", "agentic")), "base_plan_version": 1,
            "action": {"kind": "finish", "arguments": {"outputs": {"value": 2}}},
            "reason": "goal complete",
        })
        planner = PlannerApplicationService(
            path, provider=FakePlannerProvider([
                PlannerProviderResponse(raw, "request:finish", PlannerUsage())
            ])
        )
        service = DurableRuntimeApplicationService(path, planner_service=planner)
        run_id = EntityId("run", "agentic")
        result = service.submit(CommandEnvelope(
            EntityId("command", "agentic-start"), "start_run", run_id, run_id,
            AggregateVersion(0), "agentic-start", "test", NOW,
            {"workflow_id": compiled.ir.workflow_id, "workflow_version": 1,
             "definition_hash": compiled.definition_hash.value,
             "input": {"value": 1}, "goal": "finish with a value"},
        ))
        self.assertEqual("applied", result.disposition.value, result.diagnostics)
        attempts = planner.list_attempts(run_id)
        self.assertEqual(1, len(attempts))
        self.assertEqual("requested", attempts[0].status.value)
        self.assertTrue(
            attempts[0].context.graph_summary["waiting_reason"].startswith("planner:node_run:")
        )
        with SQLiteUnitOfWork(path) as uow:
            self.assertEqual(WorkflowRunStatus.WAITING, uow.runs.get(run_id).status)
            node = uow.node_runs.list_by_run(run_id)[0]
            self.assertEqual("waiting", node.status.value)
        self.assertTrue(PlannerDispatcher(planner, clock=lambda: NOW).run_once())
        self.assertTrue(
            PlannerProposalReconciler(planner, service, clock=lambda: NOW).run_once()
        )
        with SQLiteUnitOfWork(path) as uow:
            self.assertEqual(WorkflowRunStatus.SUCCEEDED, uow.runs.get(run_id).status)
        self.assertEqual("consumed", planner.list_proposals(run_id)[0].status.value)

    def run_graph(self, dsl, inputs):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "graph.db"
        compiled = compile_graph(dsl)
        SQLiteWorkflowVersionStore(path).publish(
            compiled, expected_latest_version=0, source_format="json",
            source_text=None, actor="test",
        )
        service = RuntimeApplicationService(path)
        run_id = EntityId("run", "graph")
        result = service.submit(CommandEnvelope(
            EntityId("command", "start"), "start_run", run_id, run_id,
            AggregateVersion(0), "start-graph", "test", NOW,
            {
                "workflow_id": compiled.ir.workflow_id, "workflow_version": 1,
                "definition_hash": compiled.definition_hash.value, "input": inputs,
            },
        ))
        self.assertEqual("applied", result.disposition.value, result.diagnostics)
        return service, run_id

    def test_exclusive_route_records_selected_and_not_selected_tokens(self):
        service, run_id = self.run_graph(decision_graph(), {"value": 1})
        self.assertIs(WorkflowRunStatus.SUCCEEDED, service.get_run(run_id).status)
        summary = service.get_graph_summary(run_id)
        self.assertEqual({"yes", "choose"}, {item["node_id"] for item in summary["nodes"]})
        self.assertEqual({"completed", "not_selected"}, {item["status"] for item in summary["tokens"]})

    def test_static_human_controller_waits_and_resumes_the_graph(self):
        dsl = {
            "dsl_version": "1.2",
            "metadata": {"id": "human_graph", "name": "Human"},
            "nodes": [
                {
                    "id": "approve", "kind": "human",
                    "inputs": PORT("value"), "outputs": PORT("value"),
                    "config": {
                        "task_kind": "approval", "participants": ["operator"],
                        "quorum": "any",
                    },
                },
                {"id": "done", "kind": "terminal", "inputs": PORT("value")},
            ],
            "edges": [{
                "id": "approved",
                "from": {"node": "approve", "port": "value"},
                "to": {"node": "done", "port": "value"},
            }],
            "entry": ["approve"], "terminals": ["done"],
        }
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "human.db"
        compiled = compile_graph(dsl)
        SQLiteWorkflowVersionStore(path).publish(
            compiled, expected_latest_version=0, source_format="json",
            source_text=None, actor="test",
        )
        delivery = InMemoryHumanTaskDelivery()
        service = DurableRuntimeApplicationService(
            path, human_task_delivery=delivery.deliver,
        )
        run_id = EntityId("run", "human")
        started = service.submit(CommandEnvelope(
            EntityId("command", "human-start"), "start_run", run_id, run_id,
            AggregateVersion(0), "human-start", "starter", NOW,
            {
                "workflow_id": compiled.ir.workflow_id,
                "workflow_version": 1,
                "definition_hash": compiled.definition_hash.value,
                "input": {"value": 1},
            },
        ))
        self.assertEqual("applied", started.disposition.value, started.diagnostics)
        self.assertIs(WorkflowRunStatus.WAITING, service.get_run(run_id).status)
        with service.uow_factory() as uow:
            row = uow.connection.execute(
                "SELECT task_id,node_run_id,aggregate_version FROM human_tasks"
                " WHERE run_id=?", (str(run_id),),
            ).fetchone()
        self.assertIsNotNone(row["node_run_id"])
        task_id = EntityId.parse(row["task_id"])
        token = delivery.take(task_id, "operator")
        self.assertIsNotNone(token)

        service.durable_recovery.scan_once(NOW)
        self.assertIs(WorkflowRunStatus.WAITING, service.get_run(run_id).status)
        with self.assertRaises(PermissionError):
            service.submit_human_task(
                task_id, run_id, row["aggregate_version"], token=token,
                decision="approve", value=None, actor="intruder",
                idempotency_key="intruder", now=NOW,
            )

        # The recipient already owns the delivered token; a fresh Runtime
        # instance can resume the durable wait after process restart.
        restarted = DurableRuntimeApplicationService(path)
        submitted = restarted.submit_human_task(
            task_id, run_id, row["aggregate_version"], token=token,
            decision="approve", value={"note": "ship"}, actor="operator",
            idempotency_key="approve-human", now=NOW,
        )

        self.assertEqual("completed", submitted["status"])
        replayed = restarted.submit_human_task(
            task_id, run_id, row["aggregate_version"], token=token,
            decision="approve", value={"note": "ship"}, actor="operator",
            idempotency_key="approve-human", now=NOW,
        )
        self.assertEqual("completed", replayed["status"])
        self.assertIs(WorkflowRunStatus.SUCCEEDED, restarted.get_run(run_id).status)
        summary = RuntimeApplicationService(path).get_graph_summary(run_id)
        self.assertEqual(
            {"approve": "succeeded", "done": "succeeded"},
            {item["node_id"]: item["status"] for item in summary["nodes"]},
        )

    def test_reissued_token_supersedes_the_delivered_one(self):
        """Restart-lost tokens are recovered by rotation, not resignation.

        The delivery adapter is process memory; after a restart the only way
        back in is the reissue path. It must invalidate the original token,
        bump the task version, and leave the kernel submit working with the
        rotated credential.
        """
        from orbit.workflow.application.human_service import HumanTaskService

        dsl = {
            "dsl_version": "1.2",
            "metadata": {"id": "human_rotate", "name": "Human rotate"},
            "nodes": [
                {
                    "id": "approve", "kind": "human",
                    "inputs": PORT("value"), "outputs": PORT("value"),
                    "config": {
                        "task_kind": "approval", "participants": ["operator"],
                        "quorum": "any",
                    },
                },
                {"id": "done", "kind": "terminal", "inputs": PORT("value")},
            ],
            "edges": [{
                "id": "approved",
                "from": {"node": "approve", "port": "value"},
                "to": {"node": "done", "port": "value"},
            }],
            "entry": ["approve"], "terminals": ["done"],
        }
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "rotate.db"
        compiled = compile_graph(dsl)
        SQLiteWorkflowVersionStore(path).publish(
            compiled, expected_latest_version=0, source_format="json",
            source_text=None, actor="test",
        )
        # No delivery adapter at all — the token minted at activation is lost
        # exactly as it would be when the process dies before hand-off.
        service = DurableRuntimeApplicationService(path)
        run_id = EntityId("run", "rotate")
        started = service.submit(CommandEnvelope(
            EntityId("command", "rotate-start"), "start_run", run_id, run_id,
            AggregateVersion(0), "rotate-start", "starter", NOW,
            {
                "workflow_id": compiled.ir.workflow_id,
                "workflow_version": 1,
                "definition_hash": compiled.definition_hash.value,
                "input": {"value": 1},
            },
        ))
        self.assertEqual("applied", started.disposition.value, started.diagnostics)
        with service.uow_factory() as uow:
            row = uow.connection.execute(
                "SELECT task_id,aggregate_version FROM human_tasks WHERE run_id=?",
                (str(run_id),),
            ).fetchone()
        task_id = EntityId.parse(row["task_id"])

        reissued = HumanTaskService(path).reissue_token(
            task_id, actor="operator",
            expected_version=row["aggregate_version"], now=NOW,
        )
        self.assertEqual(
            row["aggregate_version"] + 1, reissued["expected_version"]
        )
        with self.assertRaises(PermissionError):
            HumanTaskService(path).reissue_token(
                task_id, actor="stranger",
                expected_version=reissued["expected_version"], now=NOW,
            )

        submitted = service.submit_human_task(
            task_id, run_id, reissued["expected_version"],
            token=reissued["submission_token"], decision="approve",
            value=None, actor="operator",
            idempotency_key="rotate-approve", now=NOW,
        )
        self.assertEqual("completed", submitted["status"])
        self.assertIs(WorkflowRunStatus.SUCCEEDED, service.get_run(run_id).status)

    def test_parallel_join_opens_once_and_is_recoverable(self):
        dsl = {
            "dsl_version": "1.2", "metadata": {"id": "join_graph", "name": "Join"},
            "nodes": [
                {"id": "fork", "kind": "decision", "inputs": PORT("value"), "outputs": PORT("value"), "route_mode": "parallel"},
                {"id": "left", "kind": "decision", "inputs": PORT("value"), "outputs": PORT("value")},
                {"id": "right", "kind": "decision", "inputs": PORT("value"), "outputs": PORT("value")},
                {"id": "join", "kind": "join", "inputs": PORT("items"), "outputs": PORT("items"), "policies": ["join_all"]},
                {"id": "done", "kind": "terminal", "inputs": PORT("items")},
            ],
            "edges": [
                {"id": "fork_left", "from": {"node": "fork", "port": "value"}, "to": {"node": "left", "port": "value"}},
                {"id": "fork_right", "from": {"node": "fork", "port": "value"}, "to": {"node": "right", "port": "value"}},
                {"id": "left_join", "from": {"node": "left", "port": "value"}, "to": {"node": "join", "port": "items"}},
                {"id": "right_join", "from": {"node": "right", "port": "value"}, "to": {"node": "join", "port": "items"}},
                {"id": "join_done", "from": {"node": "join", "port": "items"}, "to": {"node": "done", "port": "items"}},
            ],
            "entry": ["fork"], "terminals": ["done"],
            "policies": [{"id": "join_all", "kind": "join", "config": {"mode": "all", "merge_mode": "array_by_edge"}}],
        }
        service, run_id = self.run_graph(dsl, {"value": 7})
        summary = service.get_graph_summary(run_id)
        self.assertEqual("succeeded", summary["status"])
        self.assertEqual(["open"], [item["status"] for item in summary["joins"]])
        self.assertEqual(5, len(summary["nodes"]))
        replayed = service.recovery.rehydrate(run_id)
        self.assertEqual("succeeded", replayed.state["run_status"])
        self.assertEqual(1, len(replayed.state["joins"]))
        self.assertEqual(5, len(replayed.state["tokens"]))
        self.assertTrue(check_database(service.path, run_id=run_id).ok)

    def test_parallel_completion_order_and_token_conservation_are_kernel_invariants(self):
        dsl = {
            "dsl_version": "1.2", "metadata": {"id": "order_graph", "name": "Order"},
            "nodes": [
                {"id": "fork", "kind": "decision", "inputs": PORT("value"), "outputs": PORT("value"), "route_mode": "parallel"},
                {"id": "left", "kind": "action", "inputs": PORT("value"), "outputs": PORT("value"), "handler": {"name": "work", "version": "1.0.0"}},
                {"id": "right", "kind": "action", "inputs": PORT("value"), "outputs": PORT("value"), "handler": {"name": "work", "version": "1.0.0"}},
                {"id": "join", "kind": "join", "inputs": PORT("items"), "outputs": PORT("items"), "policies": ["join_all"]},
                {"id": "done", "kind": "terminal", "inputs": PORT("items")},
            ],
            "edges": [
                {"id": "fork_left", "from": {"node": "fork", "port": "value"}, "to": {"node": "left", "port": "value"}},
                {"id": "fork_right", "from": {"node": "fork", "port": "value"}, "to": {"node": "right", "port": "value"}},
                {"id": "left_join", "from": {"node": "left", "port": "value"}, "to": {"node": "join", "port": "items"}, "priority": 0},
                {"id": "right_join", "from": {"node": "right", "port": "value"}, "to": {"node": "join", "port": "items"}, "priority": 1},
                {"id": "join_done", "from": {"node": "join", "port": "items"}, "to": {"node": "done", "port": "items"}},
            ],
            "entry": ["fork"], "terminals": ["done"],
            "policies": [{"id": "join_all", "kind": "join", "config": {"mode": "all", "merge_mode": "array_by_edge"}}],
        }
        handler = HandlerManifest(
            name="work", version="1.0.0", node_kinds=("action",),
            inputs={"value": "schema://value/1.0"}, outputs={"value": "schema://value/1.0"},
            config_schema={"type": "object", "additionalProperties": False},
            execution_safety=ExecutionSafety.REPLAY_SAFE,
            resource_profile=ResourceProfile(0, 0, 0, 60, 0, "free"),
            result_schema_id="schema://value/1.0",
        )
        compiled = compile_source(
            json.dumps(dsl), InMemoryHandlerCatalog([handler]),
            InMemorySchemaCatalog({"schema://value/1.0": {}}), source_format="json",
        )

        def execute(order):
            temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
            path = Path(temp.name) / "order.db"
            SQLiteWorkflowVersionStore(path).publish(
                compiled, expected_latest_version=0, source_format="json",
                source_text=None, actor="test",
            )
            service = DurableRuntimeApplicationService(path)
            run_id = EntityId("run", "order")
            service.submit(CommandEnvelope(
                EntityId("command", "order-start"), "start_run", run_id, run_id,
                AggregateVersion(0), "order-start", "test", NOW,
                {"workflow_id": compiled.ir.workflow_id, "workflow_version": 1,
                 "definition_hash": compiled.definition_hash.value, "input": {"value": 1}},
            ))
            claimed = [service.claim_job(f"worker-{index}", NOW) for index in range(2)]
            self.assertNotIn(None, claimed)
            by_node = {}
            with service.uow_factory() as uow:
                for item in claimed:
                    job = uow.jobs.get(item.job_id)
                    by_node[uow.node_runs.get(job.node_run_id).node_id] = item
            for item in claimed:
                service.start_job(item, NOW)
            for node_id in order:
                service.complete_job(by_node[node_id], NOW, {"value": node_id})
            summary = RuntimeApplicationService(path).get_graph_summary(run_id)
            self.assertEqual("succeeded", summary["status"])
            edge_tokens = [item for item in summary["tokens"] if item["scope"]["edge_id"]]
            self.assertEqual(len(dsl["edges"]), len(edge_tokens))
            self.assertEqual(len(edge_tokens), len({item["token_id"] for item in edge_tokens}))
            self.assertEqual(
                {item["id"] for item in dsl["edges"]},
                {item["scope"]["edge_id"] for item in edge_tokens},
            )
            self.assertTrue(all(item["status"] == "completed" for item in edge_tokens))
            return {
                "status": summary["status"],
                "nodes": sorted((item["node_id"], item["generation"], item["status"]) for item in summary["nodes"]),
                "tokens": sorted((item["scope"]["edge_id"], item["status"]) for item in edge_tokens),
                "joins": sorted((item["node_id"], item["status"]) for item in summary["joins"]),
            }

        self.assertEqual(execute(("left", "right")), execute(("right", "left")))

    def test_bounded_loop_fails_deterministically_and_preserves_generations(self):
        dsl = {
            "dsl_version": "1.2", "metadata": {"id": "loop_graph", "name": "Loop"},
            "nodes": [
                {"id": "loop", "kind": "decision", "inputs": PORT("value"), "outputs": PORT("value")},
                {"id": "done", "kind": "terminal", "inputs": PORT("value")},
            ],
            "edges": [
                {"id": "again", "from": {"node": "loop", "port": "value"}, "to": {"node": "loop", "port": "value"}, "condition": True, "priority": 1, "back_edge": True, "policy": "bounded"},
                {"id": "finish", "from": {"node": "loop", "port": "value"}, "to": {"node": "done", "port": "value"}, "condition": False, "priority": 0},
            ],
            "entry": ["loop"], "terminals": ["done"],
            "policies": [{"id": "bounded", "kind": "loop", "config": {"max_iterations": 2}}],
        }
        service, run_id = self.run_graph(dsl, {"value": 1})
        summary = service.get_graph_summary(run_id)
        self.assertEqual("failed", summary["status"])
        self.assertEqual([1, 2, 3], sorted(item["generation"] for item in summary["nodes"]))

    def test_retry_reuses_node_run_and_creates_a_new_attempt(self):
        dsl = {
            "dsl_version": "1.2", "metadata": {"id": "retry_graph", "name": "Retry"},
            "nodes": [
                {"id": "work", "kind": "action", "inputs": PORT("value"), "outputs": PORT("value"), "handler": {"name": "work", "version": "1.0.0"}, "policies": ["retry"]},
                {"id": "done", "kind": "terminal", "inputs": PORT("value")},
            ],
            "edges": [{"id": "done", "from": {"node": "work", "port": "value"}, "to": {"node": "done", "port": "value"}}],
            "entry": ["work"], "terminals": ["done"],
            "policies": [{"id": "retry", "kind": "retry", "config": {"max_attempts": 2, "backoff_seconds": [0], "categories": ["transient_error"]}}],
        }
        handler = HandlerManifest(
            name="work", version="1.0.0", node_kinds=("action",),
            inputs={"value": "schema://value/1.0"}, outputs={"value": "schema://value/1.0"},
            config_schema={"type": "object", "additionalProperties": False},
            execution_safety=ExecutionSafety.REPLAY_SAFE,
            resource_profile=ResourceProfile(0, 0, 0, 60, 0, "free"),
            result_schema_id="schema://value/1.0",
        )
        compiled = compile_source(
            json.dumps(dsl), InMemoryHandlerCatalog([handler]),
            InMemorySchemaCatalog({"schema://value/1.0": {}}), source_format="json",
        )
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "retry.db"
        SQLiteWorkflowVersionStore(path).publish(compiled, expected_latest_version=0, source_format="json", source_text=None, actor="test")
        service = DurableRuntimeApplicationService(path)
        run_id = EntityId("run", "retry")
        service.submit(CommandEnvelope(
            EntityId("command", "retry-start"), "start_run", run_id, run_id,
            AggregateVersion(0), "retry-start", "test", NOW,
            {"workflow_id": compiled.ir.workflow_id, "workflow_version": 1, "definition_hash": compiled.definition_hash.value, "input": {"value": 1}},
        ))
        first = service.claim_job("worker", NOW); self.assertIsNotNone(first)
        service.start_job(first, NOW)
        failed = service.fail_job(first, NOW, {
            "code": "handler_transient", "category": "transient_error",
            "message": "retry", "source": "test", "details": {}, "cause": None,
        })
        self.assertEqual("retry_wait", failed.summary["status"])
        timer = service.claim_timer("timer", NOW); self.assertIsNotNone(timer)
        service.fire_timer(timer, NOW)
        second = service.claim_job("worker", NOW); self.assertIsNotNone(second)
        service.start_job(second, NOW)
        service.complete_job(second, NOW, {"value": 2})
        self.assertIs(WorkflowRunStatus.SUCCEEDED, service.get_run(run_id).status)
        with service.uow_factory() as uow:
            nodes = uow.node_runs.list_by_run(run_id)
            work = next(item for item in nodes if item.node_id == "work")
            attempts = uow.attempts.list_by_node_run(work.node_run_id)
        self.assertEqual(1, len([item for item in nodes if item.node_id == "work"]))
        self.assertEqual([1, 2], [item.attempt_number.value for item in attempts])

    def test_controller_reaction_limit_yields_a_recoverable_continuation(self):
        count = 140
        nodes = [
            {"id": f"n{index:03d}", "kind": "decision", "inputs": PORT("value"), "outputs": PORT("value")}
            for index in range(count)
        ]
        nodes.append({"id": "done", "kind": "terminal", "inputs": PORT("value")})
        chain = [item["id"] for item in nodes]
        dsl = {
            "dsl_version": "1.2", "metadata": {"id": "continuation", "name": "Continuation"},
            "nodes": nodes,
            "edges": [
                {"id": f"e{index:03d}", "from": {"node": source, "port": "value"}, "to": {"node": target, "port": "value"}}
                for index, (source, target) in enumerate(zip(chain, chain[1:]))
            ],
            "entry": [chain[0]], "terminals": ["done"],
        }
        compiled = compile_graph(dsl)
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "continuation.db"
        SQLiteWorkflowVersionStore(path).publish(compiled, expected_latest_version=0, source_format="json", source_text=None, actor="test")
        service = DurableRuntimeApplicationService(path)
        run_id = EntityId("run", "continuation")
        service.submit(CommandEnvelope(
            EntityId("command", "continuation-start"), "start_run", run_id, run_id,
            AggregateVersion(0), "continuation-start", "test", NOW,
            {"workflow_id": compiled.ir.workflow_id, "workflow_version": 1, "definition_hash": compiled.definition_hash.value, "input": {"value": 1}},
        ))
        self.assertIs(WorkflowRunStatus.RUNNING, service.get_run(run_id).status)
        report = service.durable_recovery.scan_once(NOW, limit=1000)
        self.assertEqual(1, report.graph_advances)
        self.assertIs(WorkflowRunStatus.SUCCEEDED, service.get_run(run_id).status)

    def test_token_fault_rolls_back_the_entire_graph_reaction(self):
        compiled = compile_graph(decision_graph())
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "fault.db"
        versions = SQLiteWorkflowVersionStore(path)
        versions.publish(compiled, expected_latest_version=0, source_format="json", source_text=None, actor="test")

        def fault(point):
            if point == "before_token_create":
                raise RuntimeError("kill")

        kernel = RuntimeKernel(lambda: SQLiteUnitOfWork(path, fault_hook=fault), versions)
        run_id = EntityId("run", "fault")
        result = kernel.handle(CommandEnvelope(
            EntityId("command", "fault-start"), "start_run", run_id, run_id,
            AggregateVersion(0), "fault-start", "test", NOW,
            {"workflow_id": compiled.ir.workflow_id, "workflow_version": 1, "definition_hash": compiled.definition_hash.value, "input": {"value": 1}},
        ))
        self.assertEqual("rejected", result.disposition.value)
        with SQLiteUnitOfWork(path) as uow:
            self.assertIsNone(uow.runs.get(run_id))
            self.assertEqual((), uow.events.read_run(run_id))

    def test_memory_and_sqlite_graph_kernels_have_parity(self):
        compiled = compile_graph(decision_graph())
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "parity.db"
        versions = SQLiteWorkflowVersionStore(path)
        versions.publish(compiled, expected_latest_version=0, source_format="json", source_text=None, actor="test")
        memory = MemoryRuntimeDatabase()
        memory_kernel = RuntimeKernel(lambda: MemoryUnitOfWork(memory), versions)
        sqlite_kernel = RuntimeKernel(lambda: SQLiteUnitOfWork(path), versions)
        run_id = EntityId("run", "parity")
        command = CommandEnvelope(
            EntityId("command", "parity-start"), "start_run", run_id, run_id,
            AggregateVersion(0), "parity-start", "test", NOW,
            {"workflow_id": compiled.ir.workflow_id, "workflow_version": 1, "definition_hash": compiled.definition_hash.value, "input": {"value": 1}},
        )
        left, right = memory_kernel.handle(command), sqlite_kernel.handle(command)
        self.assertEqual(left.disposition, right.disposition)
        self.assertEqual(left.event_ids, right.event_ids)
        self.assertEqual(memory.runs.get(run_id).status, WorkflowRunStatus.SUCCEEDED)
        with SQLiteUnitOfWork(path) as uow:
            self.assertEqual(
                sorted(
                    (str(item.token_id), item.status)
                    for item in memory.tokens.list_by_run(run_id)
                ),
                sorted(
                    (str(item.token_id), item.status)
                    for item in uow.tokens.list_by_run(run_id)
                ),
            )


if __name__ == "__main__":
    unittest.main()
