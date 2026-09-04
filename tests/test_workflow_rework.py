"""A person rejects with a reason, and the Agent works again on that reason.

The whole loop, through the real DSL compiler and the real LangGraph runtime:
`tests/fixtures/workflow_dsl/v1/human-rework.json` is the worked example, and
these tests are what keeps it from rotting into a document that describes a
workflow the Runtime would no longer accept.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from langgraph.checkpoint.memory import InMemorySaver

from orbit.workflow.application import load_catalogs
from orbit.workflow.dsl import DiagnosticError, compile_source
from orbit.workflow.langgraph_runtime import (
    BoundHandler,
    LangGraphHandlerRegistry,
    compile_workflow,
)


FIXTURES = Path(__file__).parent / "fixtures" / "workflow_dsl" / "v1"


class HumanReworkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalogs = load_catalogs(FIXTURES / "human-rework-catalog.json")
        self.briefs: list[str] = []

    # --- the example, and the machinery to run it -------------------------

    def document(self) -> dict:
        return json.loads(
            (FIXTURES / "human-rework.json").read_text(encoding="utf-8")
        )

    def edge(self, document: dict, edge_id: str) -> dict:
        return next(item for item in document["edges"] if item["id"] == edge_id)

    def compile(self, document: dict):
        return compile_source(
            json.dumps(document),
            self.catalogs.handlers,
            self.catalogs.schemas,
            source_format="json",
            extensions=self.catalogs.extensions,
        )

    def started(self, document: dict | None = None, brief: str = "起草发布说明"):
        """Compile the example and run it up to its first review."""

        ir = self.compile(document or self.document()).ir

        def agent(values, config, context):
            self.briefs.append(values["brief"])
            return {"draft": f"draft {len(self.briefs)}"}

        def render(values, config, context):
            return {"document": f"<{values['draft']}>"}

        implementations = {"agent_task": agent, "render": render}
        references = {
            node.handler.name: node.handler
            for node in ir.nodes
            if node.handler is not None
        }
        # Bound to the fingerprint the IR itself resolved, so the fixture
        # catalog stays the single statement of what these Handlers promise.
        registry = LangGraphHandlerRegistry([
            BoundHandler(
                reference.name,
                reference.version,
                reference.manifest_fingerprint,
                implementations[name],
            )
            for name, reference in references.items()
        ])
        graph = compile_workflow(ir, registry, checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "rework"}}
        graph.invoke({"brief": brief}, config=config)
        return graph, config

    def submit(self, graph, config, decision: str, value=None):
        return graph.resume(
            {"submission": {"decision": decision, "value": value}}, config=config
        )

    def order(self, graph, config) -> list[str]:
        return list(graph.graph.get_state(config).values["execution_order"])

    def asked(self, graph, config):
        """What the pending review is being shown, or None if none pends."""

        return next(
            (
                item.value
                for task in graph.graph.get_state(config).tasks
                for item in task.interrupts
            ),
            None,
        )

    # --- the loop ---------------------------------------------------------

    def test_a_rejection_carries_its_reason_back_to_the_agent(self) -> None:
        """The reason is the next generation's brief.

        A back edge supersedes the original ingress on the port it writes, so
        what the reviewer typed arrives on the Agent's own input rather than
        having to be threaded through every node in between.
        """

        graph, config = self.started(brief="起草发布说明")

        self.submit(graph, config, "reject", "缺少版本号和日期")

        self.assertEqual("起草发布说明", self.briefs[0])
        self.assertEqual(
            {
                "original_request": "起草发布说明",
                "previous_document": "<draft 1>",
                "rework_reason": "缺少版本号和日期",
            },
            self.briefs[1],
        )

    def test_the_reviewer_is_asked_again_with_what_the_rework_produced(self) -> None:
        """A rework is re-reviewed, not silently accepted."""

        graph, config = self.started()
        first = self.asked(graph, config)

        self.submit(graph, config, "reject", "再写一版")
        second = self.asked(graph, config)

        self.assertEqual({"document": "<draft 1>"}, first["input"])
        self.assertEqual({"document": "<draft 2>"}, second["input"])

    def test_the_step_between_the_agent_and_the_review_runs_again_too(self) -> None:
        """Everything below the back edge's target repeats, not just its target.

        `render` sits between the Agent and the reviewer and never asked to be
        part of a rework; it is re-run anyway, because the graph resumes from
        the node the back edge names. A step with side effects belongs outside
        this stretch, and that is a design decision the author has to make
        knowingly.
        """

        graph, config = self.started()

        self.submit(graph, config, "reject", "再写一版")

        self.assertEqual(
            [
                "draft", "render", "review", "review_context", "route_review",
                "draft", "render",
            ],
            self.order(graph, config),
        )

    def test_an_approval_finishes_the_run_at_its_terminal(self) -> None:
        graph, config = self.started()

        self.submit(graph, config, "reject", "再写一版")
        self.submit(graph, config, "approve")

        self.assertEqual("done", self.order(graph, config)[-1])
        self.assertEqual((), graph.graph.get_state(config).next)
        self.assertEqual("approved", graph.resume({}, config=config)["result"]["status"])

    # --- the bound --------------------------------------------------------

    def test_rejecting_past_the_bound_lands_on_the_terminal_that_says_so(self) -> None:
        """`max_generations` is how many reworks are allowed, not how many
        reviews happen: two reworks, then the third rejection is routed to the
        terminal the author declared for it instead of failing the run.
        """

        graph, config = self.started()

        for attempt in range(1, 4):
            self.submit(graph, config, "reject", f"第 {attempt} 次驳回")

        self.assertEqual("done", self.order(graph, config)[-1])
        self.assertEqual((), graph.graph.get_state(config).next)
        self.assertEqual(3, len(self.briefs))
        self.assertEqual(
            "rework_exhausted", graph.resume({}, config=config)["result"]["status"],
        )

    def test_without_an_error_route_the_spent_bound_fails_the_run(self) -> None:
        """`exhaustion: fail` is a run failure, which is why the example does
        not use it: a review that could not be agreed on is an outcome, and
        recording it as a Runtime error loses that distinction.
        """

        document = self.document()
        document["policies"][0]["config"]["exhaustion"] = "fail"
        document["edges"].remove(self.edge(document, "out_of_generations"))
        document["edges"].remove(self.edge(document, "abandoned_finalize"))
        document["edges"].remove(self.edge(document, "approved_finalize"))
        document["edges"].remove(self.edge(document, "finalize_done"))
        document["edges"].append({
            "id": "approved_done",
            "from": {"node": "approved_result", "port": "result"},
            "to": {"node": "done", "port": "result"},
        })
        document["nodes"] = [
            node for node in document["nodes"]
            if node["id"] not in {"abandoned_result", "finalize"}
        ]
        document["policies"] = [
            policy for policy in document["policies"]
            if policy["id"] != "final_outcome_any"
        ]
        document["result"] = {"node": "approved_result", "port": "result"}
        graph, config = self.started(document)

        self.submit(graph, config, "reject", "第 1 次驳回")
        self.submit(graph, config, "reject", "第 2 次驳回")

        with self.assertRaisesRegex(ValueError, "exceeded max_generations"):
            self.submit(graph, config, "reject", "第 3 次驳回")

    def test_a_back_edge_without_a_bound_is_refused(self) -> None:
        """There is no unbounded rework to write by accident."""

        document = self.document()
        del self.edge(document, "rejected")["policy"]

        with self.assertRaises(DiagnosticError) as raised:
            self.compile(document)

        self.assertIn(
            "loop or rework policy",
            " ".join(item.message for item in raised.exception.diagnostics),
        )

    # --- branching on the reason -----------------------------------------

    def test_a_review_edge_may_only_branch_on_the_decision(self) -> None:
        """An edge out of a review reads `decision` and nothing else.

        So "send it back to whichever step the reason blames" cannot be
        written on the review's own edges; the next test shows where it goes
        instead.
        """

        document = self.document()
        self.edge(document, "review_context")["condition"] = (
            "source.submission.value == 'content'"
        )

        with self.assertRaises(DiagnosticError) as raised:
            self.compile(document)

        self.assertIn(
            "DSL_GRAPH_CONDITION_INVALID",
            [item.code for item in raised.exception.diagnostics],
        )

    def test_a_decision_node_after_the_review_routes_by_the_reason(self) -> None:
        """One hop past the review, the reason is ordinary data again.

        A decision node carries the submission through untouched, and its own
        edges are under no such restriction — which is how a rejection reaches
        the step it actually blames.
        """

        document = self.document()
        route = next(node for node in document["nodes"] if node["id"] == "route_review")
        self.assertEqual("decision", route["kind"])
        self.assertEqual(
            "source.context.submission.decision == 'reject'",
            self.edge(document, "rejected")["condition"],
        )
        self.compile(document)


if __name__ == "__main__":
    unittest.main()
