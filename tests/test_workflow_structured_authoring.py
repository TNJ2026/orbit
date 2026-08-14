from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pydantic import ValidationError

from orbit.workflow.authoring.generator import (
    CHANGE_KINDS,
    MAX_CHANGE_ENTRIES,
    AuthoringFailedError,
    AuthoringUnavailableError,
    AuthoringUnknownResultError,
    CancelScope,
    cancellable,
)
from orbit.workflow.authoring import (
    UnknownGenerationAgentError,
    WorkflowAuthoringService,
)
from orbit.workflow.authoring.structured import (
    ChangeEntry,
    GeneratedWorkflow,
    StructuredDslGenerator,
    structured_generators,
)
from orbit.workflow.catalogs import (
    HandlerManifest, InMemoryHandlerCatalog, InMemorySchemaCatalog,
)
from orbit.web.api_v1 import READ_SCOPE, Authorizer
from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.handlers import ResourceProfile
from orbit.workflow.dsl import AuthoredWorkflow
from tests.test_web_composition import AsgiHarness


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


WORKFLOW = {
    "dsl_version": "1.3",
    "metadata": {"id": "summarize_flow", "name": "Summarize flow"},
    "nodes": [
        {
            "id": "draft",
            "kind": "action",
            "handler": {"name": "collect", "version": "^1.0"},
            "outputs": [{"id": "request", "schema_id": "example://request/1.0"}],
        },
        {
            "id": "done",
            "kind": "terminal",
            "inputs": [{"id": "request", "schema_id": "example://request/1.0"}],
        },
    ],
    "edges": [
        {
            "id": "draft_done",
            "from": {"node": "draft", "port": "request"},
            "to": {"node": "done", "port": "request"},
        }
    ],
    "entry": ["draft"],
    "terminals": ["done"],
    "result": {"node": "draft", "port": "request"},
}


class _Result:
    def __init__(self, output) -> None:
        self.output = output


class _StubAgent:
    """The `run_sync(prompt).output` shape, without the optional dependency."""

    def __init__(self, output=None, error: Exception | None = None) -> None:
        self.output = output
        self.error = error
        self.prompts: list[str] = []
        self.settings: list[object] = []

    def run_sync(self, prompt: str, **kwargs):
        self.prompts.append(prompt)
        self.settings.append(kwargs.get("model_settings"))
        if self.error is not None:
            raise self.error
        return _Result(self.output)


def _generator(agent: _StubAgent, **kwargs) -> StructuredDslGenerator:
    return StructuredDslGenerator(
        "stub:model", agent_factory=lambda model, instructions: agent, **kwargs
    )


def _generated(**overrides) -> GeneratedWorkflow:
    payload = {"workflow": json.loads(json.dumps(WORKFLOW)), **overrides}
    return GeneratedWorkflow.model_validate(payload)


class EnvelopeTests(unittest.TestCase):
    def test_a_plain_generation_emits_a_bare_dsl_document(self) -> None:
        response = _generated().to_response()
        self.assertEqual("1.3", response["dsl_version"])
        self.assertNotIn("workflow", response)
        self.assertNotIn("change_summary", response)

    def test_a_summary_produces_the_revision_envelope(self) -> None:
        response = _generated(
            change_summary=[
                {"kind": "added", "node_id": "draft", "label": "Draft the summary"}
            ]
        ).to_response()
        self.assertIn("workflow", response)
        self.assertNotIn("dsl_version", response)
        self.assertEqual("1.3", response["workflow"]["dsl_version"])
        self.assertEqual("added", response["change_summary"][0]["kind"])

    def test_the_envelope_is_distinguishable_the_way_the_service_splits_it(
        self,
    ) -> None:
        """`_unwrap` keys off `dsl_version` being absent at the top level."""

        bare = _generated().to_response()
        enveloped = _generated(
            change_summary=[{"kind": "changed", "node_id": "draft", "label": "x"}]
        ).to_response()
        self.assertIn("dsl_version", bare)
        self.assertNotIn("dsl_version", enveloped)

    def test_an_absent_detail_is_dropped_rather_than_null(self) -> None:
        entry = _generated(
            change_summary=[{"kind": "removed", "node_id": "gone", "label": "Gone"}]
        ).to_response()["change_summary"][0]
        self.assertNotIn("detail", entry)

    def test_change_kinds_stay_in_step_with_the_service_filter(self) -> None:
        """A kind this type admits but the service drops is silently lost."""

        self.assertEqual(
            set(CHANGE_KINDS), set(ChangeEntry.model_fields["kind"].annotation.__args__)
        )

    def test_a_summary_longer_than_the_service_keeps_is_rejected(self) -> None:
        entries = [
            {"kind": "added", "node_id": f"n{index}", "label": "x"}
            for index in range(MAX_CHANGE_ENTRIES + 1)
        ]
        with self.assertRaises(ValidationError):
            _generated(change_summary=entries)

    def test_an_unsupported_node_kind_never_reaches_the_envelope(self) -> None:
        payload = json.loads(json.dumps(WORKFLOW))
        payload["nodes"][0]["kind"] = "agentic"
        with self.assertRaises(ValidationError):
            GeneratedWorkflow.model_validate({"workflow": payload})


class GeneratorContractTests(unittest.TestCase):
    def test_it_satisfies_the_same_prompt_to_text_contract_as_the_cli(self) -> None:
        agent = _StubAgent(_generated())
        text = _generator(agent)("write me a flow")
        self.assertEqual(["write me a flow"], agent.prompts)
        metadata = json.loads(text)["metadata"]
        self.assertEqual("summarize_flow", metadata["id"])
        self.assertEqual("Summarize flow", metadata["name"])

    def test_the_returned_text_is_already_a_clean_json_object(self) -> None:
        """No fence, no prose, nothing for `_extract_json` to salvage."""

        text = _generator(_StubAgent(_generated()))("go")
        self.assertTrue(text.startswith("{"))
        self.assertNotIn("```", text)
        self.assertEqual(
            AuthoredWorkflow.model_validate(WORKFLOW).to_dsl_document(),
            json.loads(text),
        )

    def test_the_agent_is_built_once_at_construction(self) -> None:
        built: list[object] = []

        def factory(model, instructions):
            built.append(model)
            return _StubAgent(_generated())

        generator = StructuredDslGenerator("stub:model", agent_factory=factory)
        generator("one")
        generator("two")
        self.assertEqual(["stub:model"], built)

    def test_model_settings_are_only_passed_when_set(self) -> None:
        plain = _StubAgent(_generated())
        _generator(plain)("go")
        self.assertEqual([None], plain.settings)

        tuned = _StubAgent(_generated())
        _generator(tuned, model_settings={"temperature": 0})("go")
        self.assertEqual([{"temperature": 0}], tuned.settings)

    def test_an_empty_model_name_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            StructuredDslGenerator("   ", agent_factory=lambda model, i: _StubAgent())

    def test_a_missing_optional_dependency_is_reported_as_unavailable(self) -> None:
        def factory(model, instructions):
            raise ImportError("No module named 'pydantic_ai'")

        with self.assertRaises(AuthoringUnavailableError):
            StructuredDslGenerator("stub:model", agent_factory=_unavailable(factory))

    def test_a_non_workflow_output_is_a_plain_failure(self) -> None:
        with self.assertRaises(AuthoringFailedError):
            _generator(_StubAgent("just a string"))("go")


def _unavailable(factory):
    """Wrap a factory so an ImportError surfaces the way the real one does."""

    def build(model, instructions):
        try:
            return factory(model, instructions)
        except ImportError as exc:
            raise AuthoringUnavailableError(str(exc)) from exc

    return build


class ErrorTranslationTests(unittest.TestCase):
    def _raises(self, error: Exception):
        generator = _generator(_StubAgent(error=error))
        with self.assertRaises(Exception) as caught:
            generator("go")
        return caught.exception

    def test_a_transport_error_is_unknown_because_the_call_may_have_landed(
        self,
    ) -> None:
        error = type("ModelHTTPError", (Exception,), {})("503")
        self.assertIsInstance(self._raises(error), AuthoringUnknownResultError)

    def test_a_timeout_is_unknown_not_failed(self) -> None:
        self.assertIsInstance(
            self._raises(TimeoutError("slow")), AuthoringUnknownResultError
        )

    def test_a_usage_limit_is_a_failure_the_model_already_answered_for(self) -> None:
        error = type("UsageLimitExceeded", (Exception,), {})("too many tokens")
        self.assertIsInstance(self._raises(error), AuthoringFailedError)

    def test_a_misconfigured_client_is_unavailable(self) -> None:
        self.assertIsInstance(
            self._raises(ValueError("unknown model")), AuthoringUnavailableError
        )

    def test_an_unrecognised_error_defaults_to_unknown(self) -> None:
        self.assertIsInstance(
            self._raises(RuntimeError("something else")), AuthoringUnknownResultError
        )


class CancellationTests(unittest.TestCase):
    def test_a_scope_cancelled_before_the_call_never_reaches_the_model(self) -> None:
        agent = _StubAgent(_generated())
        scope = CancelScope()
        scope.cancel()
        with cancellable(scope):
            with self.assertRaises(AuthoringUnknownResultError):
                _generator(agent)("go")
        self.assertEqual([], agent.prompts)

    def test_a_scope_cancelled_during_the_call_makes_the_result_unknown(self) -> None:
        scope = CancelScope()

        class _CancellingAgent(_StubAgent):
            def run_sync(self, prompt: str, **kwargs):
                scope.cancel()
                return super().run_sync(prompt, **kwargs)

        agent = _CancellingAgent(_generated())
        with cancellable(scope):
            with self.assertRaises(AuthoringUnknownResultError):
                _generator(agent)("go")
        self.assertEqual(["go"], agent.prompts)


class RegistrationTests(unittest.TestCase):
    def test_named_generators_land_in_the_services_mapping_shape(self) -> None:
        generators = structured_generators(
            [("gpt", "openai:gpt-5.2"), ("claude", "anthropic:claude-opus-5")],
            agent_factory=lambda model, instructions: _StubAgent(_generated()),
        )
        self.assertEqual(["claude", "gpt"], sorted(generators))
        self.assertTrue(all(callable(item) for item in generators.values()))

    def test_a_duplicate_name_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            structured_generators(
                [("gpt", "a"), ("gpt", "b")],
                agent_factory=lambda model, instructions: _StubAgent(),
            )

    def test_a_blank_name_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            structured_generators(
                [("  ", "a")],
                agent_factory=lambda model, instructions: _StubAgent(),
            )


class FunnelIntegrationTests(unittest.TestCase):
    """The claim this file rests on: it plugs into the existing funnel.

    Nothing in `WorkflowAuthoringService` knows a structured generator from a
    CLI one — it asks a callable for text and compiles what comes back. These
    drive the real service to show that is actually true.
    """

    def _service(self, agent, **kwargs):
        return WorkflowAuthoringService(
            InMemoryHandlerCatalog([MANIFEST]),
            SCHEMAS,
            _generator(agent),
            handler_facts=[{
                "name": "transform", "version": "1.0.0",
                "config_schema": dict(MANIFEST.config_schema),
            }],
            **kwargs,
        )

    def _typed(self, workflow_id: str = "generated") -> GeneratedWorkflow:
        return GeneratedWorkflow.model_validate({"workflow": _funnel_document(workflow_id)})

    def test_a_structured_answer_compiles_and_publishes_in_one_attempt(self) -> None:
        agent = _StubAgent(self._typed())
        outcome = self._service(agent).generate("make me a flow")
        self.assertEqual(1, outcome.attempts)
        self.assertEqual("workflow:generated", outcome.workflow_id)
        self.assertEqual(2, outcome.node_count)
        self.assertTrue(outcome.definition_hash)

    def test_the_prompt_the_service_built_reaches_the_model_unchanged(self) -> None:
        agent = _StubAgent(self._typed())
        self._service(agent).generate("make me a flow")
        self.assertIn("make me a flow", agent.prompts[0])
        self.assertIn("transform", agent.prompts[0])

    def test_a_named_structured_generator_is_selectable_like_a_cli_agent(self) -> None:
        agent = _StubAgent(self._typed())
        authoring = WorkflowAuthoringService(
            InMemoryHandlerCatalog([MANIFEST]),
            SCHEMAS,
            _generator(_StubAgent(self._typed())),
            handler_facts=[{"name": "transform", "version": "1.0.0"}],
            generators={"gpt": _generator(agent)},
        )
        self.assertEqual(("gpt",), authoring.available_agents)
        authoring.generate("make me a flow", agent="gpt")
        self.assertEqual(1, len(agent.prompts))

    def test_an_unknown_generator_name_is_still_refused(self) -> None:
        authoring = WorkflowAuthoringService(
            InMemoryHandlerCatalog([MANIFEST]),
            SCHEMAS,
            _generator(_StubAgent(self._typed())),
            handler_facts=[{"name": "transform", "version": "1.0.0"}],
            generators={"gpt": _generator(_StubAgent(self._typed()))},
        )
        with self.assertRaises(UnknownGenerationAgentError):
            authoring.generate("go", agent="claude")

    def test_a_transport_failure_keeps_the_services_unknown_taxonomy(self) -> None:
        error = type("ModelHTTPError", (Exception,), {})("503")
        with self.assertRaises(AuthoringUnknownResultError):
            self._service(_StubAgent(error=error)).generate("go")


def _funnel_document(workflow_id: str = "generated") -> dict:
    """The `test_workflow_authoring` fixture, at this model's dsl_version."""

    return {
        "dsl_version": "1.3",
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
        "result": {"node": "work", "port": "value"},
    }


if __name__ == "__main__":
    unittest.main()


class CliArgumentTests(unittest.TestCase):
    """`--structured-agent NAME=MODEL`, refused at the prompt when malformed."""

    def _parse(self, values):
        from orbit.__main__ import _structured_agents

        return _structured_agents(values)

    def test_no_flag_means_no_structured_agents(self) -> None:
        self.assertIsNone(self._parse(None))
        self.assertIsNone(self._parse([]))

    def test_each_entry_becomes_a_name_and_a_model(self) -> None:
        self.assertEqual(
            {"gpt": "openai:gpt-5.2", "claude": "anthropic:claude-opus-5"},
            self._parse(["gpt=openai:gpt-5.2", " claude = anthropic:claude-opus-5 "]),
        )

    def test_a_model_containing_equals_keeps_everything_after_the_first(self) -> None:
        self.assertEqual({"x": "a=b"}, self._parse(["x=a=b"]))

    def test_a_missing_half_is_refused(self) -> None:
        for entry in ("gpt", "gpt=", "=openai:gpt-5.2", "  =  "):
            with self.subTest(entry=entry), self.assertRaises(SystemExit):
                self._parse([entry])

    def test_a_duplicate_name_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            self._parse(["gpt=a", "gpt=b"])


class AppWiringTests(unittest.TestCase):
    """A configured structured agent has to reach the authoring service."""

    def _app(self, **kwargs):
        """Built with the optional dependency stubbed out at its seam.

        `structured_generators` looks `StructuredDslGenerator` up as a module
        global, so replacing it here exercises the real wiring in `create_app`
        without pydantic-ai installed or a network call.
        """

        from orbit.web.app import create_app

        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        return create_app(
            Path(directory.name) / "orbit.sqlite3",
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: (READ_SCOPE,)),
            **kwargs,
        )

    def setUp(self) -> None:
        patcher = mock.patch(
            "orbit.workflow.authoring.structured.StructuredDslGenerator",
            lambda model, **kwargs: _generator(_StubAgent(_generated())),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_structured_agent_is_offered_as_a_generation_agent(self) -> None:
        app = self._app(structured_agents={"gpt": "stub:model"}, discover_agents=False)
        with AsgiHarness(app) as client:
            capability = client.get(
                "/api/v1/capabilities", actor="test:reader"
            ).json()["data"]["capabilities"]["workflow_generation"]
        self.assertTrue(capability["available"])
        self.assertIn("gpt", capability["agents"])
        self.assertEqual("gpt", capability["default_agent"])

    def test_two_structured_agents_both_appear_and_one_is_the_default(self) -> None:
        app = self._app(
            structured_agents={"zeta": "stub:z", "alpha": "stub:a"},
            discover_agents=False,
        )
        with AsgiHarness(app) as client:
            capability = client.get(
                "/api/v1/capabilities", actor="test:reader"
            ).json()["data"]["capabilities"]["workflow_generation"]
        self.assertEqual(["alpha", "zeta"], sorted(capability["agents"]))
        self.assertEqual("alpha", capability["default_agent"])

    def test_a_name_colliding_with_another_writer_is_refused(self) -> None:
        """Two writers behind one name means the author cannot be told the truth."""

        with self.assertRaises(ValueError) as caught:
            self._app(
                structured_agents={"cli": "stub:model"},
                workflow_generators={"cli": lambda prompt: "{}"},
                discover_agents=False,
            )
        self.assertIn("collide", str(caught.exception))

    def test_no_structured_agents_leaves_the_wiring_untouched(self) -> None:
        app = self._app(
            workflow_generators={"cli": lambda prompt: "{}"}, discover_agents=False
        )
        with AsgiHarness(app) as client:
            capability = client.get(
                "/api/v1/capabilities", actor="test:reader"
            ).json()["data"]["capabilities"]["workflow_generation"]
        self.assertEqual(["cli"], capability["agents"])


class RevisionByPatchTests(unittest.TestCase):
    """A revision is operations, not a rewritten document."""

    BASE = {
        "dsl_version": "1.3",
        "metadata": {"id": "flow", "name": "Flow"},
        "nodes": [
            {
                "id": "work", "kind": "action", "label": "Transform",
                "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
                "outputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
                "handler": {"name": "transform", "version": "1.0.0"},
                "config": {"mode": "fast"},
            },
            {
                "id": "done", "kind": "terminal", "label": "Done",
                "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
            },
        ],
        "edges": [{
            "id": "flow_edge",
            "from": {"node": "work", "port": "value"},
            "to": {"node": "done", "port": "value"},
        }],
        "entry": ["work"], "terminals": ["done"],
        "result": {"node": "work", "port": "value"},
    }

    def reviser(self, operations, error=None, declared_base=1):
        from orbit.workflow.authoring.structured import WorkflowRevision

        revision = None if error else WorkflowRevision.model_validate(
            {"patch": {"base_version": declared_base, "operations": operations}}
        )
        agent = _StubAgent(revision, error)
        self.agent = agent
        return StructuredDslGenerator(
            "stub:model",
            agent_factory=lambda model, i: _StubAgent(_generated()),
            revision_factory=lambda model, i: agent,
        )

    def revise(self, operations):
        generator = self.reviser(operations)
        return json.loads(generator.revise("change it", self.BASE))

    def test_a_patch_written_against_another_version_is_refused(self) -> None:
        """`base_version` was carried and never checked.

        The model is told which version it was handed and states the one it
        worked against. When those differ it was reasoning about a document
        other than the one it was given, and every operation may still apply:
        the result would be a workflow nobody asked for, with nothing about it
        to notice. Refused, and refused as feedback the funnel can retry with.
        """

        from orbit.workflow.authoring.generator import AuthoringFailedError

        generator = self.reviser(
            [{"op": "set_node_label", "node_id": "work", "label": "x"}],
            declared_base=3,
        )
        with self.assertRaisesRegex(AuthoringFailedError, "written against v3"):
            generator.revise("change it", self.BASE, 5)

    def test_a_patch_against_the_version_it_was_given_applies(self) -> None:
        generator = self.reviser(
            [{"op": "set_node_label", "node_id": "work", "label": "x"}],
            declared_base=5,
        )
        payload = json.loads(generator.revise("change it", self.BASE, 5))
        document = payload["workflow"]
        self.assertEqual("x", document["nodes"][0]["label"])

    def test_a_caller_that_does_not_know_the_version_checks_nothing(self) -> None:
        """A check against a number nobody has is not a check."""

        generator = self.reviser(
            [{"op": "set_node_label", "node_id": "work", "label": "x"}],
            declared_base=3,
        )
        payload = json.loads(generator.revise("change it", self.BASE))
        self.assertEqual("x", payload["workflow"]["nodes"][0]["label"])

    def test_only_what_the_operations_named_is_different(self) -> None:
        payload = self.revise([
            {"op": "set_node_config", "node_id": "work", "config": {"mode": "thorough"}},
        ])
        document = payload["workflow"]
        self.assertEqual({"mode": "thorough"}, document["nodes"][0]["config"])
        # Everything else is the document that went in.
        self.assertEqual(self.BASE["nodes"][1], document["nodes"][1])
        self.assertEqual(self.BASE["edges"], document["edges"])
        self.assertEqual(self.BASE["result"], document["result"])

    def test_the_base_it_was_given_is_not_mutated(self) -> None:
        before = json.loads(json.dumps(self.BASE))
        self.revise([{"op": "remove_node", "node_id": "done"}])
        self.assertEqual(before, self.BASE)

    def test_the_summary_is_read_off_the_operations(self) -> None:
        """Derived, not declared: it cannot describe an edit that did not happen."""

        payload = self.revise([
            {"op": "add_node", "node": {"id": "review", "kind": "human", "label": "Review"}},
            {"op": "set_node_label", "node_id": "work", "label": "Renamed"},
        ])
        self.assertEqual(
            [
                {"kind": "added", "node_id": "review", "label": "Review"},
                {"kind": "changed", "node_id": "work", "label": "Transform"},
            ],
            payload["change_summary"],
        )

    def test_a_node_touched_twice_is_one_change_to_read_about(self) -> None:
        payload = self.revise([
            {"op": "set_node_label", "node_id": "work", "label": "A"},
            {"op": "set_node_config", "node_id": "work", "config": {"mode": "x"}},
        ])
        self.assertEqual(1, len(payload["change_summary"]))

    def test_a_change_with_nothing_node_scoped_returns_a_bare_document(self) -> None:
        # The envelope exists to carry a summary; without one it would only
        # make a plain revision look different from the CLI path's output.
        payload = self.revise([{"op": "set_metadata", "name": "Renamed"}])
        self.assertEqual("1.3", payload["dsl_version"])
        self.assertNotIn("workflow", payload)
        self.assertEqual("Renamed", payload["metadata"]["name"])

    def test_an_operation_that_cannot_apply_says_which_one(self) -> None:
        generator = self.reviser([
            {"op": "set_node_label", "node_id": "work", "label": "A"},
            {"op": "set_node_config", "node_id": "ghost", "config": {}},
        ])
        with self.assertRaises(AuthoringFailedError) as caught:
            generator.revise("change it", self.BASE)
        self.assertIn("operation 1", str(caught.exception))
        self.assertIn("ghost", str(caught.exception))

    def test_a_reply_that_is_not_a_patch_is_a_plain_failure(self) -> None:
        generator = StructuredDslGenerator(
            "stub:model",
            agent_factory=lambda model, i: _StubAgent(_generated()),
            revision_factory=lambda model, i: _StubAgent("just a string"),
        )
        with self.assertRaises(AuthoringFailedError):
            generator.revise("change it", self.BASE)

    def test_the_prompt_reaches_the_reviser_and_not_the_generator(self) -> None:
        generation = _StubAgent(_generated())
        revision = _StubAgent(None, None)
        from orbit.workflow.authoring.structured import WorkflowRevision

        revision.output = WorkflowRevision.model_validate(
            {"patch": {"base_version": 1, "operations": [
                {"op": "set_metadata", "name": "Renamed"},
            ]}}
        )
        generator = StructuredDslGenerator(
            "stub:model",
            agent_factory=lambda model, i: generation,
            revision_factory=lambda model, i: revision,
        )
        generator.revise("rename it", self.BASE)
        self.assertEqual(["rename it"], revision.prompts)
        self.assertEqual([], generation.prompts)


class RevisionSeamTests(unittest.TestCase):
    """The service asks for a patch where the writer offers one."""

    def test_a_writer_without_revise_falls_back_to_a_document(self) -> None:
        """Every discovered Agent CLI is one of these.

        It is asked for operations first and gets the same guarantee when it
        can answer with them; when it cannot, the person still gets their
        edit rather than a refusal.
        """

        asked = []

        def cli(prompt):
            asked.append(prompt)
            return json.dumps(_funnel_document())

        authoring = WorkflowAuthoringService(
            InMemoryHandlerCatalog([MANIFEST]), SCHEMAS, cli,
            handler_facts=[{"name": "transform", "version": "1.0.0"}],
        )
        outcome = authoring.revise(
            json.dumps(_funnel_document()), "change it",
            expected_workflow_id="workflow:generated",
        )
        # Asked for operations first — that is where the guarantee is — and
        # only then for a document, which is what this writer can give. The
        # outcome says which arrived, because the two are not equally
        # trustworthy and the difference must not be invisible.
        self.assertEqual("document", outcome.revision_mode)
        self.assertIn("operations", asked[0])
        self.assertNotIn("operations", asked[-1])
        self.assertTrue(len(asked) > 1)

    def test_a_cli_that_answers_with_operations_gets_the_guarantee(self) -> None:
        """The point of asking a CLI for operations at all.

        The guarantee is a property of applying operations to a base, not of
        the library that called the model: a part of the workflow no operation
        names is a part that could not have changed. A CLI is held to it here
        by refusing anything that is not a patch, which is why it earns the
        same promise a structured writer does.
        """

        base = _funnel_document()
        asked = []

        def cli(prompt):
            asked.append(prompt)
            return json.dumps({
                "base_version": 3,
                "summary": "rename the step",
                "operations": [
                    {"op": "set_node_label", "node_id": "work", "label": "Renamed"},
                ],
            })

        authoring = WorkflowAuthoringService(
            InMemoryHandlerCatalog([MANIFEST]), SCHEMAS, cli,
            handler_facts=[{"name": "transform", "version": "1.0.0"}],
        )
        outcome = authoring.revise(
            json.dumps(base), "rename the step",
            expected_workflow_id="workflow:generated", base_version=3,
        )
        revised = json.loads(outcome.source)

        self.assertEqual("patch", outcome.revision_mode)
        self.assertEqual(1, outcome.attempts)
        self.assertEqual(1, len(asked), "the fallback should not have run")
        self.assertIn("operations", asked[0])
        self.assertEqual("Renamed", revised["nodes"][0]["label"])
        # Everything the operations did not name is byte-identical.
        self.assertEqual(base["edges"], revised["edges"])
        self.assertEqual(base["metadata"], revised["metadata"])

    def test_a_patch_against_the_wrong_version_is_fed_back(self) -> None:
        """A refusal the model can act on, not one the operator has to read."""

        attempts = []

        def cli(prompt):
            attempts.append(prompt)
            return json.dumps({
                "base_version": 99,
                "operations": [
                    {"op": "set_node_label", "node_id": "work", "label": "x"},
                ],
            })

        authoring = WorkflowAuthoringService(
            InMemoryHandlerCatalog([MANIFEST]), SCHEMAS, cli,
            handler_facts=[{"name": "transform", "version": "1.0.0"}],
        )
        with self.assertRaises(AuthoringFailedError):
            authoring.revise(
                json.dumps(_funnel_document()), "rename it",
                expected_workflow_id="workflow:generated", base_version=3,
                # No fallback budget, so the failure is the patch pass's own.
                # (`max_attempts` is halved for operations.)
            )
        # The second attempt carries the first one's refusal.
        self.assertIn("written against v99", attempts[1])
        self.assertIn("PATCH_NOT_APPLICABLE", attempts[1])

    def test_a_writer_with_revise_is_given_the_base_to_patch(self) -> None:
        seen = {}

        class PatchWriter:
            def __call__(self, prompt):  # pragma: no cover - must not be used
                raise AssertionError("revision must not take the generation path")

            def revise(self, prompt, base, base_version=None):
                seen["base"] = base
                seen["base_version"] = base_version
                document = json.loads(json.dumps(base))
                document["nodes"][0]["label"] = "Renamed by patch"
                return json.dumps(document)

        authoring = WorkflowAuthoringService(
            InMemoryHandlerCatalog([MANIFEST]), SCHEMAS, PatchWriter(),
            handler_facts=[{"name": "transform", "version": "1.0.0"}],
        )
        outcome = authoring.revise(
            json.dumps(_funnel_document()), "rename it",
            expected_workflow_id="workflow:generated", base_version=7,
        )
        self.assertEqual("generated", seen["base"]["metadata"]["id"])
        # The version travels with the base, so the applier can hold the patch
        # to the version it claims rather than to whatever it says.
        self.assertEqual(7, seen["base_version"])
        self.assertIn("Renamed by patch", outcome.source)
