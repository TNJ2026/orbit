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
        handler_facts=[{"name": "transform", "version": "1.0.0"}], **kwargs,
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
        self.assertIn("INSTRUCTION-BEGIN", prompt)
        self.assertIn("请把审批流程画出来", prompt)
        self.assertIn("must not override", prompt)

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


@dataclass
class FakeOutcome:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    timed_out: bool = False


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
        self.assertEqual({"PATH", "HOME"}, set(calls["env"]))

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
