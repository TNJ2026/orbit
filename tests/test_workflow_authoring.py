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
    AuthoringFailedError, AuthoringUnavailableError, AuthoringUnknownResultError,
    TrustedCliDslGenerator,
    WorkflowAuthoringService,
)
from orbit.workflow.catalogs import (
    HandlerManifest, InMemoryHandlerCatalog, InMemorySchemaCatalog,
)
from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.handlers import ResourceProfile
from orbit.workflow.dsl.schema import ID_PATTERN


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
    def test_generation_retries_when_goal_cannot_bind_to_one_entry(self) -> None:
        agent_manifest = HandlerManifest(
            "agent.test", "1.0.0", ("action",),
            {"prompt": "schema://object/1.0"},
            {"value": "example://integer/1.0"},
            {"type": "object"}, ExecutionSafety.REPLAY_SAFE,
            ResourceProfile(100_000, 100_000, 0, 300, 0, "builtin"),
            "schema://object/1.0", (), (), True, True,
        )
        corrected = valid_document()
        corrected["nodes"][0]["inputs"] = [
            {"id": "prompt", "schema_id": "schema://object/1.0"},
        ]
        corrected["nodes"][0]["handler"] = {
            "name": "agent.test", "version": "1.0.0",
        }
        artifact_policy = {
            "transport": "artifact_ref", "max_size_bytes": 262144,
            "content_types": ["text/markdown"], "visibility": "run",
        }
        corrected["nodes"][0]["outputs"][0].update(artifact_policy)
        corrected["nodes"][1]["inputs"][0].update(artifact_policy)
        corrected["dsl_version"] = "1.3"
        corrected["result"] = {"node": "work", "port": "value"}
        broken = json.loads(json.dumps(corrected))
        broken["entry"] = ["work", "done"]
        model = ScriptedModel([json.dumps(broken), json.dumps(corrected)])
        authoring = WorkflowAuthoringService(
            InMemoryHandlerCatalog([agent_manifest]), SCHEMAS, model,
            handler_facts=[{
                "name": "agent.test", "version": "1.0.0",
                "inputs": {"prompt": "schema://object/1.0"},
                "outputs": {"value": "example://integer/1.0"},
            }],
            require_goal_binding=True,
        )

        outcome = authoring.generate("research then report")

        self.assertEqual(2, outcome.attempts)
        self.assertIn("GOAL_BINDING_MISSING", model.prompts[1])
        self.assertIn("exactly one entry", model.prompts[0])

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
        self.assertIn("at most one incoming non-back edge", prompt)
        self.assertIn("source.result.approved", prompt)
        self.assertIn("never source.approved", prompt)
        self.assertIn("top-level join policy", prompt)
        self.assertIn("must never form a cycle", prompt)
        self.assertIn("content_types:['text/markdown']", prompt)
        self.assertIn("Never return such a deliverable only as inline JSON", prompt)
        self.assertIn("long-form or otherwise substantial text", prompt)
        self.assertIn("Reserve inline transport for short structured values", prompt)
        self.assertIn("uses Artifact transport for long text", prompt)
        self.assertIn("full test suites", prompt)
        self.assertIn("prefer targeted tests", prompt)
        self.assertIn("useful partial result", prompt)
        self.assertIn("explicit back_edge", prompt)

    def test_prompt_tiers_its_rules_and_shows_one_whole_document(self) -> None:
        """Two dozen peer rules give no way to tell a compile error from taste."""

        model = ScriptedModel([json.dumps(valid_document())])
        service(model).generate("flow")
        prompt = model.prompts[0]
        for tier in ("[HARD]", "[SHAPE]", "[STYLE]"):
            self.assertIn(tier, prompt)
        # A whole example, not only fragments: entry, a typed edge, a terminal
        # and a result that names a port which exists.
        example = prompt.split("SHAPE-EXAMPLE", 1)[1]
        for key in ('"entry"', '"terminals"', '"result"', '"dsl_version"'):
            self.assertIn(key, example)
        # The constraints models actually break are repeated last, next to the
        # instruction they apply to.
        tail = prompt.split("BEFORE YOU ANSWER, RE-CHECK", 1)[1]
        self.assertIn("no edge field named `default`", tail)
        self.assertIn("source.<from.port>", tail)

    def test_handler_ports_are_pre_rendered_for_the_model_to_copy(self) -> None:
        """A conversion the model performs by hand is a conversion it can botch."""

        model = ScriptedModel([json.dumps(valid_document())])
        WorkflowAuthoringService(
            InMemoryHandlerCatalog([MANIFEST]), SCHEMAS, model,
            handler_facts=[{
                "name": "transform", "version": "1.0.0",
                "inputs": {"value": "example://integer/1.0"},
                "outputs": {"value": "example://integer/1.0"},
            }],
        ).generate("flow")
        facts = json.loads(
            model.prompts[0].split("FACTS: ", 1)[1].split("\n\n", 1)[0]
        )
        ports = facts["handlers"][0]["ports"]
        self.assertEqual(
            [{"id": "value", "schema_id": "example://integer/1.0"}], ports["inputs"]
        )
        self.assertEqual(ports["inputs"], ports["outputs"])

    def test_the_slug_rule_states_the_grammar_it_is_validated_against(self) -> None:
        """"A readable name" invites a space, and a space is a failed compile."""

        model = ScriptedModel([json.dumps(valid_document())])
        service(model).generate("flow", workflow_id="workflow:wf_1")
        prompt = model.prompts[0]
        self.assertIn(ID_PATTERN, prompt)
        self.assertIn("never a space", prompt)

    def test_an_assigned_identity_overwrites_the_model_id(self) -> None:
        """The Runtime owns identity; what the model called it becomes the slug."""

        model = ScriptedModel([json.dumps(valid_document(workflow_id="research"))])
        outcome = service(model).generate("flow", workflow_id="workflow:wf_1")

        metadata = json.loads(outcome.source)["metadata"]
        self.assertEqual("wf_1", metadata["id"])
        self.assertEqual("research", metadata["slug"])
        self.assertEqual("workflow:wf_1", outcome.workflow_id)

    def test_an_obeyed_assignment_leaves_no_slug_rather_than_a_uuid_one(self) -> None:
        """A model that copies the assigned id has not named anything.

        The prompt asks for both the assigned id and a slug. When only the
        first arrives, metadata.id holds an opaque uuid — using it as the slug
        would list the workflow under `wf_1` and call that a readable name.
        """

        model = ScriptedModel([json.dumps(valid_document(workflow_id="wf_1"))])
        outcome = service(model).generate("flow", workflow_id="workflow:wf_1")

        metadata = json.loads(outcome.source)["metadata"]
        self.assertEqual("wf_1", metadata["id"])
        self.assertNotIn("slug", metadata)

    def test_an_empty_slug_is_dropped_instead_of_failing_the_schema(self) -> None:
        """A blank string is not a name, and the id grammar rejects it."""

        blank = valid_document(workflow_id="wf_1")
        blank["metadata"]["slug"] = "   "
        model = ScriptedModel([json.dumps(blank)])
        outcome = service(model).generate("flow", workflow_id="workflow:wf_1")

        self.assertNotIn("slug", json.loads(outcome.source)["metadata"])

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
        self.assertIn("RETRY-CONTEXT", model.prompts[1])
        self.assertIn("DSL_HANDLER_NOT_FOUND", model.prompts[1])
        self.assertIn('\\"name\\": \\"missing\\"', model.prompts[1])
        self.assertIn("Do not return a repair summary", model.prompts[1])

    def test_retry_names_the_rule_each_finding_came_from(self) -> None:
        """A complaint is not a constraint; repair needs the rule itself."""

        broken = valid_document()
        broken["nodes"][0]["handler"] = {"name": "missing", "version": "1.0.0"}
        model = ScriptedModel([
            json.dumps(broken), json.dumps(valid_document()),
        ])

        service(model).generate("flow")

        retry = model.prompts[1]
        self.assertIn("DSL_HANDLER_NOT_FOUND", retry)
        self.assertIn("must name one of the entries in `handlers`", retry)
        self.assertIn("fix EVERY finding listed", retry)

    def test_each_rejected_attempt_reports_structured_diagnostics(self) -> None:
        broken = valid_document()
        broken["nodes"][0]["handler"] = {"name": "missing", "version": "1.0.0"}
        reports = []
        model = ScriptedModel([json.dumps(broken), json.dumps(valid_document())])

        service(model).generate(
            "flow",
            on_diagnostics=lambda attempt, maximum, findings: reports.append(
                (attempt, maximum, findings)
            ),
        )

        self.assertEqual((1, 5), reports[0][:2])
        self.assertEqual("DSL_HANDLER_NOT_FOUND", reports[0][2][0]["code"])
        self.assertIn("rule", reports[0][2][0])

    def test_retry_context_keeps_the_head_of_a_long_answer(self) -> None:
        """Truncating from the front drops metadata and nodes — the part to fix."""

        from orbit.workflow.authoring.generator import MAX_RETRY_CONTEXT_CHARS

        broken = valid_document()
        broken["metadata"]["padding"] = "x" * (MAX_RETRY_CONTEXT_CHARS + 1000)
        model = ScriptedModel([
            json.dumps(broken), json.dumps(valid_document()),
        ])

        service(model).generate("flow")

        retry = model.prompts[1]
        self.assertIn("dsl_version", retry)

    def test_a_trailing_comma_is_repaired_instead_of_costing_a_retry(self) -> None:
        """A stray comma should not buy another CLI call and another charge."""

        extract = WorkflowAuthoringService._extract_json
        self.assertEqual({"a": 1}, extract('{"a": 1,}'))
        self.assertEqual({"a": [1, 2]}, extract('{"a": [1, 2,],}'))
        self.assertEqual(
            {"text": ",} and ,]"},
            extract('{"text": ",} and ,]",}'),
        )
        # Corrupted text still fails: a mangled name must never reach publish.
        with self.assertRaisesRegex(ValueError, "U\\+FFFD"):
            extract('{"a": "�"}')

    def test_a_guard_failure_keeps_its_own_code(self) -> None:
        """Flattening every guard to GENERATION_PROTOCOL hides which one fired."""

        from orbit.workflow.authoring.generator import _protocol_finding

        self.assertEqual(
            "GOAL_BINDING_MISSING",
            _protocol_finding(ValueError("GOAL_BINDING_MISSING: no entry"))["code"],
        )
        self.assertEqual(
            "GENERATION_PROTOCOL",
            _protocol_finding(ValueError("no JSON object in the response"))["code"],
        )

    def test_unicode_replacement_character_is_repaired_before_publish(self) -> None:
        broken = valid_document()
        broken["metadata"]["name"] = "网��搜索"
        model = ScriptedModel([
            json.dumps(broken, ensure_ascii=False), json.dumps(valid_document()),
        ])

        outcome = service(model).generate("flow")

        self.assertEqual(2, outcome.attempts)
        self.assertIn("Unicode replacement character U+FFFD", model.prompts[1])
        self.assertNotIn("�", outcome.source)

    def test_exhausted_retries_surface_diagnostics_and_raw_output(self) -> None:
        model = ScriptedModel(["not json at all"] * 5)
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
        model = ScriptedModel([json.dumps(huge)] * 5)
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


class PatchPromptTests(unittest.TestCase):
    """The operations prompt must not also ask for a document.

    The `[HARD]` tier outranks everything else, and on a revision it said
    "return the COMPLETE modified document" and "wrap your answer as
    {workflow, change_summary}" — in the same prompt that asked for
    operations. Measured against the real CLIs: opencode returned a document
    on every first attempt and complied only after the parse failure was fed
    back; codex did it on half. With the contradiction gone, all three
    answered with operations first time, on all eight kinds of change.
    """

    def prompts_for(self, shape):
        current = json.dumps(valid_document())
        model = ScriptedModel([current] * 4)
        service(model).revise(
            current, "rename it", expected_workflow_id="workflow:generated",
        )
        # The operations pass runs first, the document pass after it.
        return (model.prompts[0] if shape == "patch"
                else model.prompts[AuthoringReviseTests.PATCH_BUDGET])

    def test_the_operations_prompt_never_asks_for_a_document(self) -> None:
        prompt = self.prompts_for("patch")
        self.assertIn("Answer with the operations that change it", prompt)
        self.assertIn('"patch_contract"', prompt)
        for contradiction in (
            "return the COMPLETE modified document",
            'Wrap your answer as {"workflow"',
        ):
            self.assertNotIn(contradiction, prompt)

    def test_the_document_prompt_still_asks_for_one(self) -> None:
        """The fallback is unchanged; only the pass before it was wrong."""

        prompt = self.prompts_for("document")
        self.assertIn("return the COMPLETE modified document", prompt)
        self.assertNotIn("Answer with the operations that change it", prompt)


class ExpressionVocabularyTests(unittest.TestCase):
    """What a condition may be, told rather than guessed.

    The prompt showed one `ref` and a bare `true`. A model asked for anything
    else — a comparison, a membership test — had to invent the operator
    vocabulary, and `DSL_EXPRESSION_INVALID: expression AST nodes must be
    objects` is what it got for guessing. Measured against the real codex CLI
    on "change the urgent branch to trigger on severity": three runs failed
    every patch attempt and fell back to a whole document; with the vocabulary
    in the prompt, three runs answered with `set_edge` on the first or second
    attempt and left every node untouched.
    """

    def test_every_operator_the_compiler_accepts_is_offered(self) -> None:
        from orbit.workflow.authoring.generator import _expression_vocabulary
        from orbit.workflow.dsl.expressions import _CALLS, _COMPARE

        vocabulary = _expression_vocabulary()
        self.assertEqual(
            sorted(set(_COMPARE.values())), vocabulary["comparison"]["ops"]
        )
        self.assertEqual(sorted(_CALLS), vocabulary["call"]["names"])
        self.assertEqual(
            {"literal", "ref", "comparison", "and_or", "not", "call", "list",
             "note", "reading_an_agent_result"},
            set(vocabulary),
        )

    def test_reading_an_agent_result_is_named_as_a_guess(self) -> None:
        """`exists` was in the vocabulary and nothing said when to reach for it.

        An action's result is `schema://object/1.0` — an open object — so a
        condition reading into it is a guess about what the Agent will write,
        and a wrong guess fails the run *after* the Agent has done its work.
        Observed: opencode wrote `source.result.review_complete.issues_found`
        and the run died on `reference member 'issues_found' does not exist`.
        """

        from orbit.workflow.authoring.generator import _expression_vocabulary

        guidance = _expression_vocabulary()["reading_an_agent_result"]
        self.assertIn("exists", guidance)
        self.assertIn("open object", guidance)

    def test_the_offered_comparison_example_actually_compiles(self) -> None:
        """A worked example in a prompt is a promise the compiler must keep."""

        from orbit.workflow.authoring.generator import _expression_vocabulary
        from orbit.workflow.dsl.expressions import validate_expression_ast

        example = _expression_vocabulary()["comparison"]["example"]
        self.assertEqual(example, validate_expression_ast(example, "$.condition"))

    def test_the_prompt_states_what_a_person_s_answer_looks_like(self) -> None:
        """The Runtime defines it, the compiler enforces it, nothing said it.

        A human node's output must accept `{decision, value}` — but the model
        was left to guess, wrote a condition on a field of its own invention,
        guarded it with `exists`, and produced an approval workflow whose
        approval branch could never be taken. Observed: it always published
        the rejection.
        """

        current = json.dumps(valid_document())
        model = ScriptedModel([current] * 4)
        service(model).revise(
            current, "add an approval step",
            expected_workflow_id="workflow:generated",
        )
        prompt = model.prompts[0]
        self.assertIn('"human_submission"', prompt)
        self.assertIn('"decision"', prompt)
        self.assertIn("field of your own invention", prompt)

    def test_a_revision_prompt_carries_the_vocabulary(self) -> None:
        current = json.dumps(valid_document())
        model = ScriptedModel([current] * 4)
        service(model).revise(
            current, "route on severity",
            expected_workflow_id="workflow:generated",
        )
        self.assertIn('"condition_ast"', model.prompts[0])
        self.assertIn('"not_in"', model.prompts[0])


class AuthoringReviseTests(unittest.TestCase):
    """The document pass — what a CLI that cannot answer with operations gets.

    A revision asks for operations first. These are about what happens after
    that, so each script begins with answers that are documents rather than
    patches: the patch pass spends its budget refusing them, and the fallback
    begins. `document_prompts` skips past that opening, so an assertion about
    "the first revision prompt" still means the first one of this pass.
    """

    # `max_attempts - 2`, which is what `revise` reserves for operations.
    PATCH_BUDGET = 3

    def _revise(self, model, **kwargs):
        base = json.dumps(valid_document())
        return service(model).revise(
            base, "rename it", expected_workflow_id="workflow:generated", **kwargs,
        )

    def _script(self, *responses):
        """`responses` for the document pass, behind a spent patch budget."""

        documents = [json.dumps(valid_document())] * self.PATCH_BUDGET
        return ScriptedModel([*documents, *responses])

    def document_prompts(self, model):
        return model.prompts[self.PATCH_BUDGET:]

    def test_revise_carries_current_source_and_the_keep_id_rule(self) -> None:
        renamed = valid_document()
        renamed["metadata"]["name"] = "Renamed"
        model = self._script(json.dumps(renamed))
        outcome = self._revise(model)
        self.assertEqual("workflow:generated", outcome.workflow_id)
        self.assertEqual("document", outcome.revision_mode)
        prompt = self.document_prompts(model)[0]
        self.assertIn("current_source", prompt)
        self.assertIn("MODIFYING an existing workflow", prompt)
        self.assertIn("metadata.id exactly as it is", prompt)
        self.assertIn("rename it", prompt)

    def test_a_changed_workflow_id_is_rejected_and_retried(self) -> None:
        drifted = valid_document(workflow_id="hijacked")
        model = self._script(json.dumps(drifted), json.dumps(valid_document()))
        outcome = self._revise(model)
        self.assertEqual(2, outcome.attempts)
        self.assertIn("must not change", self.document_prompts(model)[1])

    def test_persistent_id_drift_exhausts_and_fails(self) -> None:
        drifted = json.dumps(valid_document(workflow_id="hijacked"))
        model = ScriptedModel([drifted] * 5)
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
    cancelled: bool = False


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
        # Deep enough for both passes: a revision asks for operations first,
        # so the opening attempts are spent refusing these documents as
        # patches and the fallback then succeeds on one of them.
        script = [json.dumps(valid_document())] * 4
        self.codex = ScriptedModel(list(script))
        self.claude = ScriptedModel(list(script))
        self.default = ScriptedModel(list(script))
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
        # Every attempt of both passes went to the chosen Agent, and none of
        # them to the default: a fallback must not change who is writing.
        self.assertTrue(len(self.claude.prompts) > 1)
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
        self.assertIsNone(calls["timeout"])
        self.assertEqual({"PATH", "HOME", "USER", "LOGNAME"}, set(calls["env"]))

    def test_positional_prompt_uses_non_interactive_cli_command(self) -> None:
        calls = {}

        def runner(argv, **kwargs):
            calls.update(kwargs, argv=argv)
            return FakeOutcome(stdout="answer")

        generator = TrustedCliDslGenerator(
            ["codex", "exec", "--skip-git-repo-check"],
            prompt_positional=True, runner=runner,
        )

        self.assertEqual("answer", generator("build a workflow"))
        self.assertEqual(
            ["codex", "exec", "--skip-git-repo-check", "--", "build a workflow"],
            calls["argv"],
        )
        self.assertEqual("", calls["stdin_text"])

    def test_the_prompt_demands_a_label_in_the_reader_s_language(self) -> None:
        """Step names are read by whoever opened the page, not the prompt writer.

        Without a stated language the Agent infers one from the instruction, so
        a Chinese interface and an English prompt produce English step names.
        """

        model = ScriptedModel([json.dumps(valid_document())])
        service(model).generate("build a research flow", language="zh-CN")
        prompt = model.prompts[0]

        self.assertIn('"display_language":"zh-CN"', prompt)
        self.assertIn("Give every node a `label`", prompt)
        self.assertIn("written in the `display_language`", prompt)
        self.assertIn("Keep it as short as practical", prompt)
        self.assertIn("avoid sentences, explanations and redundant words", prompt)
        # The label must not be smuggled into handler config, which may be
        # closed against unknown keys.
        self.assertIn("Never put it inside `config`", prompt)
        self.assertIn('"label"', prompt)

    def test_the_prompt_states_a_language_even_when_the_caller_omits_one(self) -> None:
        model = ScriptedModel([json.dumps(valid_document())])
        service(model).generate("build a research flow")
        self.assertIn('"display_language":"en-US"', model.prompts[0])

    def test_a_revision_prompt_also_carries_the_reader_s_language(self) -> None:
        current = json.dumps(valid_document())
        # Enough for the operations pass to spend its budget on documents and
        # the fallback to succeed; the language fact is in every prompt either
        # way, which is the point.
        model = ScriptedModel([current] * 4)
        service(model).revise(
            current, "add a step", expected_workflow_id="workflow:generated",
            language="zh-CN",
        )
        self.assertIn('"display_language":"zh-CN"', model.prompts[0])
        self.assertIn("long-form or otherwise substantial text", model.prompts[0])

    def test_a_cli_that_never_ran_is_unavailability(self) -> None:
        """Nothing started, so nothing was asked of a model and nothing spent."""

        for error in (FileNotFoundError("gone"), PermissionError("no"), OSError("fork")):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(AuthoringUnavailableError):
                    TrustedCliDslGenerator(
                        ["gen-cli"],
                        runner=lambda argv, **_: (_ for _ in ()).throw(error),
                    )("prompt")
        with self.assertRaises(AuthoringUnavailableError):
            TrustedCliDslGenerator(
                ["gen-cli"], runner=lambda argv, **_: FakeOutcome(returncode=2, stderr="boom")
            )("prompt")

    def test_a_silenced_cli_leaves_the_result_unknown(self) -> None:
        """A timeout or a stop is not a failure: the call may have happened.

        Reporting either as a plain failure would licence an automatic second
        call for work a model may already have done and been paid for.
        """

        for outcome in (
            FakeOutcome(timed_out=True), FakeOutcome(cancelled=True),
        ):
            with self.subTest(outcome=outcome):
                with self.assertRaises(AuthoringUnknownResultError):
                    TrustedCliDslGenerator(
                        ["gen-cli"], runner=lambda argv, **_: outcome
                    )("prompt")

    def test_a_generation_reports_its_child_so_it_can_be_stopped(self) -> None:
        """Without the handle, cancelling only discards a still-running Agent."""

        from orbit.workflow.authoring.generator import CancelScope, cancellable

        stopped: list[float | None] = []

        class Handle:
            def cancel(self, *, grace_seconds=None):
                stopped.append(grace_seconds)

        def runner(argv, *, on_start=None, **_):
            on_start(Handle())
            return FakeOutcome(cancelled=True)

        scope = CancelScope()
        with cancellable(scope):
            with self.assertRaises(AuthoringUnknownResultError):
                TrustedCliDslGenerator(["gen-cli"], runner=runner)("prompt")
            scope.cancel(grace_seconds=2)

        # The child was attached, and detached once the call returned, so a
        # later cancellation cannot reach a process that already exited.
        self.assertEqual([], stopped)

    def test_a_cancellation_reaches_a_child_that_starts_afterwards(self) -> None:
        from orbit.workflow.authoring.generator import CancelScope

        stopped: list[float | None] = []

        class Handle:
            def cancel(self, *, grace_seconds=None):
                stopped.append(grace_seconds)

        scope = CancelScope()
        scope.cancel(grace_seconds=3)
        scope.attach(Handle())
        self.assertTrue(scope.cancelled)
        self.assertEqual([None], stopped)

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
