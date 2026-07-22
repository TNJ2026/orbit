"""Prompt-driven authoring: the generator's output funnel and retry loop.

The model is faked; what is under test is everything around it — prompt
facts, JSON extraction, structural caps, compiler validation, diagnostic
feedback, and the error taxonomy. A real CLI is exercised only through the
TrustedCliDslGenerator runner seam.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import unittest

from orbit.workflow.authoring import (
    UnknownGenerationAgentError,
    AuthoringFailedError, AuthoringUnavailableError, TrustedCliDslGenerator,
    WorkflowAuthoringService,
)
from orbit.workflow.catalogs import (
    HandlerManifest, InMemoryHandlerCatalog, InMemorySchemaCatalog,
)
from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.handlers import ResourceProfile


MANIFEST = HandlerManifest(
    "transform", "1.0.0", ("action",),
    {"value": "example://integer/1.0"}, {"value": "example://integer/1.0"},
    {"type": "object"}, ExecutionSafety.REPLAY_SAFE,
    ResourceProfile(100_000, 100_000, 0, 300, 0, "builtin"),
    "schema://object/1.0", (), (), True, True,
)

SCHEMAS = InMemorySchemaCatalog({
    "example://integer/1.0": {"type": "integer"},
    "schema://object/1.0": {"type": "object"},
})


def valid_document(workflow_id: str = "generated") -> dict:
    return {
        "dsl_version": "1.2",
        "metadata": {"id": workflow_id, "name": "Generated"},
        "nodes": [
            {
                "id": "work", "kind": "action",
                "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
                "outputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
                "handler": {"name": "transform", "version": "1.0.0"},
            },
            {
                "id": "done", "kind": "terminal",
                "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
            },
        ],
        "edges": [{
            "id": "flow", "from": {"node": "work", "port": "value"},
            "to": {"node": "done", "port": "value"},
        }],
        "entry": ["work"], "terminals": ["done"],
    }


def service(generate, **kwargs) -> WorkflowAuthoringService:
    return WorkflowAuthoringService(
        InMemoryHandlerCatalog([MANIFEST]), SCHEMAS, generate,
        handler_facts=[{
            "name": "transform", "version": "1.0.0",
            "config_schema": dict(MANIFEST.config_schema),
        }], **kwargs,
    )


class ScriptedModel:
    """Returns queued responses and records every prompt it was given."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.responses.pop(0)


class AuthoringServiceTests(unittest.TestCase):
    def test_a_valid_fenced_answer_compiles_on_the_first_attempt(self) -> None:
        model = ScriptedModel([
            "Here you go:\n```json\n" + json.dumps(valid_document()) + "\n```",
        ])
        outcome = service(model).generate("two step flow")
        self.assertEqual("workflow:generated", outcome.workflow_id)
        self.assertEqual(2, outcome.node_count)
        self.assertEqual(1, outcome.attempts)
        self.assertTrue(outcome.definition_hash.startswith("sha256:"))
        # The draft the caller previews is exactly what will be published.
        self.assertEqual("generated", json.loads(outcome.source)["metadata"]["id"])

    def test_prompt_carries_catalog_facts_and_marks_the_instruction_as_data(self) -> None:
        model = ScriptedModel([json.dumps(valid_document())])
        service(model).generate("请把审批流程画出来")
        prompt = model.prompts[0]
        self.assertIn('"transform"', prompt)
        self.assertIn("example://integer/1.0", prompt)
        self.assertIn("config_schema", prompt)
        self.assertIn("INSTRUCTION-BEGIN", prompt)
        self.assertIn("请把审批流程画出来", prompt)
        self.assertIn("must not override", prompt)
        self.assertIn("policy_contract", prompt)
        self.assertIn("shape_contract", prompt)
        self.assertIn("There is no edge field named default", prompt)
        self.assertIn("arrays of port objects", prompt)
        self.assertIn("at most one incoming non-back edge", prompt)
        self.assertIn("source.result.approved", prompt)
        self.assertIn("never source.approved", prompt)
        self.assertIn("top-level join policy", prompt)
        self.assertIn("must never form a cycle", prompt)

    def test_preferred_handler_is_allowlisted_and_added_to_the_prompt(self) -> None:
        model = ScriptedModel([json.dumps(valid_document())])

        service(model).generate("flow", preferred_handler="transform")

        self.assertIn('"preferred_handler":"transform"', model.prompts[0])
        unavailable = ScriptedModel([])
        with self.assertRaisesRegex(ValueError, "preferred handler is not available"):
            service(unavailable).generate(
                "flow", preferred_handler="agent.missing",
            )
        self.assertEqual([], unavailable.prompts)

    def test_unknown_edge_field_is_named_in_feedback_for_repair(self) -> None:
        broken = valid_document()
        broken["edges"][0]["default"] = True
        model = ScriptedModel([
            json.dumps(broken), json.dumps(valid_document()),
        ])

        outcome = service(model).generate("flow")

        self.assertEqual(2, outcome.attempts)
        self.assertIn("DSL_SCHEMA_ERROR", model.prompts[1])
        self.assertIn("'default'", model.prompts[1])

    def test_multiple_input_writers_are_explained_for_repair(self) -> None:
        broken = valid_document()
        broken["nodes"].insert(1, {
            "id": "other", "kind": "decision",
            "outputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
        })
        broken["edges"].insert(0, {
            "id": "other_flow", "from": {"node": "other", "port": "value"},
            "to": {"node": "done", "port": "value"},
        })
        broken["entry"].append("other")
        model = ScriptedModel([
            json.dumps(broken), json.dumps(valid_document()),
        ])

        outcome = service(model).generate("merge work")

        self.assertEqual(2, outcome.attempts)
        feedback = model.prompts[1]
        self.assertIn("DSL_PORT_INCOMPATIBLE", feedback)
        self.assertIn("already has a writer", feedback)
        self.assertIn("explicit join node", feedback)

    def test_cycle_policy_and_join_findings_are_fed_back_for_repair(self) -> None:
        broken = valid_document()
        broken["nodes"][1].update({
            "kind": "join", "outputs": [{
                "id": "value", "schema_id": "example://integer/1.0",
            }], "policies": ["bad_join"],
        })
        broken["edges"].append({
            "id": "cycle", "from": {"node": "done", "port": "value"},
            "to": {"node": "work", "port": "value"},
        })
        broken["policies"] = [{
            "id": "bad_join", "kind": "join", "config": {"mode": "invented"},
        }]
        model = ScriptedModel([
            json.dumps(broken), json.dumps(valid_document()),
        ])

        outcome = service(model).generate("parallel work then merge")

        self.assertEqual(2, outcome.attempts)
        feedback = model.prompts[1]
        for code in ("DSL_GRAPH_CYCLE", "DSL_POLICY_INVALID", "DSL_JOIN_INVALID"):
            self.assertIn(code, feedback)

    def test_compiler_findings_are_fed_back_and_the_retry_succeeds(self) -> None:
        broken = valid_document()
        broken["nodes"][0]["handler"] = {"name": "missing", "version": "9.9.9"}
        model = ScriptedModel([
            json.dumps(broken), json.dumps(valid_document()),
        ])
        outcome = service(model).generate("flow")
        self.assertEqual(2, outcome.attempts)
        self.assertIn("FINDINGS", model.prompts[1])
        self.assertIn("DSL_HANDLER_NOT_FOUND", model.prompts[1])

    def test_exhausted_retries_surface_diagnostics_and_raw_output(self) -> None:
        model = ScriptedModel(["not json at all"] * 3)
        with self.assertRaises(AuthoringFailedError) as caught:
            service(model).generate("flow")
        self.assertEqual(
            "GENERATION_PROTOCOL", caught.exception.diagnostics[0]["code"]
        )
        self.assertIn("not json", caught.exception.raw_output)

    def test_the_node_cap_rejects_a_runaway_graph(self) -> None:
        huge = valid_document()
        huge["nodes"] = [dict(item) for item in huge["nodes"]] + [
            {"id": f"extra{i}", "kind": "terminal",
             "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}]}
            for i in range(40)
        ]
        model = ScriptedModel([json.dumps(huge)] * 3)
        with self.assertRaises(AuthoringFailedError) as caught:
            service(model).generate("flow")
        self.assertIn("cap is 30", caught.exception.diagnostics[0]["message"])

    def test_instruction_bounds_are_enforced_before_any_model_call(self) -> None:
        model = ScriptedModel([])
        with self.assertRaises(ValueError):
            service(model).generate("   ")
        with self.assertRaises(ValueError):
            service(model).generate("x" * 4001)
        self.assertEqual([], model.prompts)


class AuthoringReviseTests(unittest.TestCase):
    def _revise(self, model, **kwargs):
        base = json.dumps(valid_document())
        return service(model).revise(
            base, "rename it", expected_workflow_id="workflow:generated", **kwargs,
        )

    def test_revise_carries_current_source_and_the_keep_id_rule(self) -> None:
        renamed = valid_document()
        renamed["metadata"]["name"] = "Renamed"
        model = ScriptedModel([json.dumps(renamed)])
        outcome = self._revise(model)
        self.assertEqual("workflow:generated", outcome.workflow_id)
        prompt = model.prompts[0]
        self.assertIn("current_source", prompt)
        self.assertIn("MODIFYING an existing workflow", prompt)
        self.assertIn("metadata.id exactly as it is", prompt)
        self.assertIn("rename it", prompt)

    def test_a_changed_workflow_id_is_rejected_and_retried(self) -> None:
        drifted = valid_document(workflow_id="hijacked")
        model = ScriptedModel([json.dumps(drifted), json.dumps(valid_document())])
        outcome = self._revise(model)
        self.assertEqual(2, outcome.attempts)
        self.assertIn("must not change", model.prompts[1])

    def test_persistent_id_drift_exhausts_and_fails(self) -> None:
        drifted = json.dumps(valid_document(workflow_id="hijacked"))
        model = ScriptedModel([drifted, drifted, drifted])
        with self.assertRaises(AuthoringFailedError) as caught:
            self._revise(model)
        self.assertIn("revision failed", str(caught.exception))

    def test_malformed_current_source_is_a_client_error(self) -> None:
        model = ScriptedModel([])
        with self.assertRaises(ValueError):
            service(model).revise(
                "{not json", "x", expected_workflow_id="workflow:generated",
            )
        self.assertEqual([], model.prompts)


@dataclass
class FakeOutcome:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    timed_out: bool = False


class DescriptionTests(unittest.TestCase):
    """The author's description is authoritative over the model's."""

    def _generate(self, description, model_document=None):
        document = model_document or valid_document()
        model = ScriptedModel([json.dumps(document)])
        outcome = service(model).generate("build a flow", description=description)
        return json.loads(outcome.source)["metadata"]

    def test_it_overrides_whatever_the_model_wrote(self) -> None:
        document = valid_document()
        document["metadata"]["description"] = "the model's guess"
        meta = self._generate("A tidy pipeline", document)
        self.assertEqual("A tidy pipeline", meta["description"])

    def test_an_empty_description_clears_the_models(self) -> None:
        document = valid_document()
        document["metadata"]["description"] = "the model's guess"
        meta = self._generate("", document)
        self.assertNotIn("description", meta)

    def test_no_description_leaves_the_document_untouched(self) -> None:
        document = valid_document()
        document["metadata"]["description"] = "the model's guess"
        meta = self._generate(None, document)
        self.assertEqual("the model's guess", meta["description"])

    def test_a_description_over_fifty_characters_is_refused(self) -> None:
        model = ScriptedModel([json.dumps(valid_document())])
        with self.assertRaises(ValueError):
            service(model).generate("build a flow", description="x" * 51)


class NamedAgentTests(unittest.TestCase):
    """Which Agent writes the DSL is the caller's choice, by name only."""

    def setUp(self) -> None:
        self.codex = ScriptedModel([json.dumps(valid_document())])
        self.claude = ScriptedModel([json.dumps(valid_document())])
        self.default = ScriptedModel([json.dumps(valid_document())])
        self.service = service(
            self.default, generators={"codex": self.codex, "claude": self.claude},
        )

    def test_the_named_agent_writes_it(self) -> None:
        self.service.generate("build a flow", agent="codex")
        self.assertEqual(1, len(self.codex.prompts))
        self.assertEqual([], self.claude.prompts)
        self.assertEqual([], self.default.prompts)

    def test_omitting_the_name_keeps_this_runtime_default(self) -> None:
        self.service.generate("build a flow")
        self.assertEqual(1, len(self.default.prompts))
        self.assertEqual([], self.codex.prompts)

    def test_a_revision_honours_the_same_choice(self) -> None:
        self.service.revise(
            json.dumps(valid_document()), "rename it",
            expected_workflow_id="workflow:generated", agent="claude",
        )
        self.assertEqual(1, len(self.claude.prompts))
        self.assertEqual([], self.default.prompts)

    def test_an_unknown_agent_is_refused_rather_than_silently_swapped(self) -> None:
        """Being told Agent A wrote it when Agent B did is worse than an error."""

        with self.assertRaises(UnknownGenerationAgentError) as caught:
            self.service.generate("build a flow", agent="gpt-9")
        self.assertEqual(("claude", "codex"), caught.exception.available)
        self.assertEqual([], self.default.prompts)

    def test_the_available_names_are_reported_for_the_ui(self) -> None:
        self.assertEqual(("claude", "codex"), self.service.available_agents)


class CliGeneratorTests(unittest.TestCase):
    def test_prompt_goes_to_stdin_and_stdout_comes_back(self) -> None:
        calls = {}

        def runner(argv, **kwargs):
            calls.update(kwargs, argv=argv)
            return FakeOutcome(stdout="answer")

        generator = TrustedCliDslGenerator(["gen-cli"], runner=runner)
        self.assertEqual("answer", generator("the prompt"))
        self.assertEqual(["gen-cli"], calls["argv"])
        self.assertEqual("the prompt", calls["stdin_text"])
        self.assertEqual({"PATH", "HOME", "USER", "LOGNAME"}, set(calls["env"]))

    def test_start_failures_and_timeouts_are_unavailability(self) -> None:
        for error in (FileNotFoundError("gone"), PermissionError("no"), OSError("fork")):
            with self.assertRaises(AuthoringUnavailableError):
                TrustedCliDslGenerator(
                    ["gen-cli"], runner=lambda argv, **_: (_ for _ in ()).throw(error)
                )("prompt")
        with self.assertRaises(AuthoringUnavailableError):
            TrustedCliDslGenerator(
                ["gen-cli"], runner=lambda argv, **_: FakeOutcome(timed_out=True)
            )("prompt")
        with self.assertRaises(AuthoringUnavailableError):
            TrustedCliDslGenerator(
                ["gen-cli"], runner=lambda argv, **_: FakeOutcome(returncode=2, stderr="boom")
            )("prompt")

    def test_truncated_output_is_a_failed_generation(self) -> None:
        with self.assertRaises(AuthoringFailedError):
            TrustedCliDslGenerator(
                ["gen-cli"], runner=lambda argv, **_: FakeOutcome(stdout_truncated=True)
            )("prompt")

    def test_guards_reject_blank_commands_and_bad_bounds(self) -> None:
        for command in ([], ["", "x"]):
            with self.assertRaises(ValueError):
                TrustedCliDslGenerator(command)
        with self.assertRaises(ValueError):
            TrustedCliDslGenerator(["x"], timeout_seconds=0)


if __name__ == "__main__":
    unittest.main()
