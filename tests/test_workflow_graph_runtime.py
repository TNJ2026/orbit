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
from orbit.workflow.domain.durable_execution import ExecutionSafety
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
from orbit.workflow.runtime.kernel import RuntimeKernel
from orbit.workflow.runtime.plan_instantiator import instantiate_execution_plan
from orbit.workflow.testing import assert_reducer_source_is_pure, side_effect_guard


NOW = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
PORT = lambda name: [{"id": name, "schema_id": "schema://value/1.0"}]


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
