from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import unittest

from orbit.workflow.domain.errors import ERROR_CODE_REGISTRY, ErrorCategory
from orbit.workflow.domain.graph import (
    FAILURE_RESOLUTION_PRECEDENCE,
    CompletionDecision,
    CompletionDisposition,
    EdgeRoute,
    ExhaustionAction,
    FailureResolution,
    JoinDecision,
    JoinDisposition,
    JoinMergeMode,
    JoinMode,
    JoinPolicy,
    LoopPolicy,
    PlanEdge,
    ReworkPolicy,
    RetryPolicy,
    RouteDecision,
    RouteMode,
    TokenScope,
    derive_branch_token_id,
    derive_graph_node_run_id,
    derive_join_group_id,
)
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.schemas import SchemaValidationError, validate_contract
from orbit.workflow.domain.serialization import to_primitive
from orbit.workflow.domain.stability import CONTRACT_STABILITY, ContractStability
from orbit.workflow.domain.states import BranchTokenStatus
from orbit.workflow.domain.versions import DefinitionHash, Revision


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "workflow_graph"
    / "v1"
    / "graph-contracts-1.2.json"
)
HASH_A = DefinitionHash("sha256:" + "a" * 64)
HASH_B = DefinitionHash("sha256:" + "b" * 64)


def contracts():
    node_run_id = EntityId("node_run", "decision-001")
    join_group_id = EntityId("join_group", "join-001")
    return {
        "token_scope": TokenScope(Revision(1), "edge-a", "work", 2, "parallel-1"),
        "plan_edge": PlanEdge(
            "edge-a", "decide", "work", EdgeRoute.SUCCESS, 10,
            "result", "input", {"op": "eq", "left": {"path": "source.ok"}, "right": True},
            {"op": "identity"}, False, None,
        ),
        "retry_policy": RetryPolicy(
            3, (1, 5), (ErrorCategory.TRANSIENT_ERROR, ErrorCategory.TIMEOUT),
        ),
        "rework_policy": ReworkPolicy(2, ExhaustionAction.ERROR_ROUTE),
        "loop_policy": LoopPolicy(4, ExhaustionAction.FAIL),
        "join_policy": JoinPolicy(
            JoinMode.N_OF_M, JoinMergeMode.ARRAY_BY_EDGE, threshold=2,
        ),
        "route_decision": RouteDecision(
            node_run_id, EdgeRoute.SUCCESS, RouteMode.PARALLEL,
            ("edge-a", "edge-b"), ("edge-a",), ("edge-b",), HASH_A,
        ),
        "join_decision": JoinDecision(
            join_group_id, JoinDisposition.OPEN,
            ("edge-a", "edge-b"), ("edge-a", "edge-b"),
            ("edge-a",), ("edge-b",), HASH_B,
        ),
        "completion_decision": CompletionDecision(
            CompletionDisposition.WAIT, "retry timer is pending", (),
            (EntityId("timer", "retry-001"),), "retry_wait",
        ),
    }


class WorkflowGraphContractTests(unittest.TestCase):
    def test_all_contracts_match_schema_and_golden(self):
        values = contracts()
        self.assertEqual(json.loads(FIXTURE.read_text()), to_primitive(values))
        names = {
            "token_scope": "graph-token-scope/1.2",
            "plan_edge": "graph-plan-edge/1.2",
            "retry_policy": "graph-retry-policy/1.2",
            "rework_policy": "graph-rework-policy/1.2",
            "loop_policy": "graph-loop-policy/1.2",
            "join_policy": "graph-join-policy/1.2",
            "route_decision": "graph-route-decision/1.2",
            "join_decision": "graph-join-decision/1.2",
            "completion_decision": "graph-completion-decision/1.2",
        }
        for name, contract in names.items():
            with self.subTest(contract):
                validate_contract(to_primitive(values[name]), contract)

    def test_route_decision_is_a_complete_partition(self):
        with self.assertRaisesRegex(ValueError, "partition"):
            RouteDecision(
                EntityId("node_run", "n1"), EdgeRoute.SUCCESS,
                RouteMode.PARALLEL, ("a", "b"), ("a",), (), HASH_A,
            )
        with self.assertRaisesRegex(ValueError, "at most one"):
            RouteDecision(
                EntityId("node_run", "n1"), EdgeRoute.SUCCESS,
                RouteMode.EXCLUSIVE, ("a", "b"), ("a", "b"), (), HASH_A,
            )

    def test_join_policy_rejects_ambiguous_fields(self):
        with self.assertRaisesRegex(ValueError, "requires threshold"):
            JoinPolicy(JoinMode.N_OF_M, JoinMergeMode.ARRAY_BY_EDGE)
        with self.assertRaisesRegex(ValueError, "only valid for deadline"):
            JoinPolicy(
                JoinMode.ALL, JoinMergeMode.ARRAY_BY_EDGE,
                deadline_seconds=10, min_successful=1,
            )

    def test_all_successful_tolerates_failed_participants_but_all_does_not(self):
        from orbit.workflow.graph.joins import JoinTokenFact, evaluate_join
        facts = (
            JoinTokenFact("ok", 0, BranchTokenStatus.COMPLETED, {"value": 1}),
            JoinTokenFact("bad", 1, BranchTokenStatus.FAILED),
        )
        strict, _ = evaluate_join(
            EntityId("join_group", "strict"),
            JoinPolicy(JoinMode.ALL, JoinMergeMode.ARRAY_BY_EDGE), facts,
        )
        tolerant, merged = evaluate_join(
            EntityId("join_group", "tolerant"),
            JoinPolicy(JoinMode.ALL_SUCCESSFUL, JoinMergeMode.ARRAY_BY_EDGE), facts,
        )
        self.assertIs(JoinDisposition.FAIL, strict.disposition)
        self.assertIs(JoinDisposition.OPEN, tolerant.disposition)
        self.assertEqual(("ok",), tolerant.winner_edge_ids)
        self.assertEqual(({"value": 1},), merged)

    def test_unknown_external_result_cannot_enter_retry_policy(self):
        with self.assertRaisesRegex(ValueError, "cannot be retried"):
            RetryPolicy(2, (0,), (ErrorCategory.UNKNOWN_EXTERNAL_RESULT,))
        self.assertEqual(
            (
                FailureResolution.UNKNOWN_WAIT,
                FailureResolution.RETRY,
                FailureResolution.ROUTE,
                FailureResolution.TERMINATE,
            ),
            FAILURE_RESOLUTION_PRECEDENCE,
        )

    def test_decisions_and_nested_json_are_deeply_immutable(self):
        values = contracts()
        with self.assertRaises(TypeError):
            values["plan_edge"].condition["op"] = "changed"
        with self.assertRaises(FrozenInstanceError):
            values["route_decision"].mode = RouteMode.EXCLUSIVE

    def test_schema_diagnostics_include_exact_json_path(self):
        value = to_primitive(contracts()["plan_edge"])
        value["priority"] = -1
        with self.assertRaises(SchemaValidationError) as caught:
            validate_contract(value, "graph-plan-edge/1.2")
        self.assertEqual("$.priority", caught.exception.json_path)

    def test_stable_id_vectors_are_frozen(self):
        run_id = EntityId("run", "graph-001")
        self.assertEqual(
            "branch_token:4d5ec6b0e091d455f5df4007a865a9ed6c65f244f250be63c0c5c31cf0a0f512",
            str(derive_branch_token_id(run_id, Revision(1), "edge-a", 1, "root")),
        )
        self.assertEqual(
            "join_group:043b52c256c2c9f60c9215c3cce766038d7018c4a50e8cac0f987ea56e9ccc8a",
            str(derive_join_group_id(run_id, Revision(1), "join", 1)),
        )
        self.assertEqual(
            "node_run:7881b9549b93389dffef5f0e9dee55a5d598e661c940d0c7dbb3c2c7ba7a797d",
            str(derive_graph_node_run_id(run_id, Revision(1), "work", 2, "edge-a")),
        )

    def test_stability_and_error_codes_are_registered(self):
        for name in (
            "execution_plan_v1_2", "static_graph_contract_1_2", "graph_policy",
            "graph_decision_facts", "graph_runtime_decisions", "graph_persistence_v5",
        ):
            self.assertIs(ContractStability.STABLE, CONTRACT_STABILITY[name])
        self.assertIs(ErrorCategory.PERMANENT_ERROR, ERROR_CODE_REGISTRY["graph_stalled"])
        self.assertIs(ErrorCategory.TIMEOUT, ERROR_CODE_REGISTRY["join_deadline_exceeded"])


if __name__ == "__main__":
    unittest.main()
