from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from orbit.workflow.catalogs import (
    HandlerManifest,
    InMemoryHandlerCatalog,
    InMemorySchemaCatalog,
)
from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.handlers import ResourceProfile
from orbit.workflow.dsl import (
    LANGGRAPH_NODE_KINDS,
    AuthoredWorkflow,
    authoring_json_schema,
    compile_source,
    parse_dsl,
    validate_dsl_structure,
)


AUTHORED = {
    "dsl_version": "1.3",
    "metadata": {"id": "summarize_flow", "name": "Summarize flow"},
    "nodes": [
        {
            "id": "draft",
            "kind": "action",
            "label": "Draft the summary",
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


def _authored(**overrides) -> dict:
    document = json.loads(json.dumps(AUTHORED))
    document.update(overrides)
    return document


class AuthoredWorkflowShapeTests(unittest.TestCase):
    def test_emitting_is_a_fixed_point(self) -> None:
        """Normal form, not a copy: implicit fields come back explicit."""

        workflow = AuthoredWorkflow.model_validate(AUTHORED)
        document = workflow.to_dsl_document()
        self.assertEqual(workflow, AuthoredWorkflow.model_validate(document))
        self.assertEqual(document, AuthoredWorkflow.model_validate(document).to_dsl_document())

    def test_emitting_fills_in_the_defaults_the_dsl_schema_declares(self) -> None:
        document = AuthoredWorkflow.model_validate(AUTHORED).to_dsl_document()
        self.assertEqual("success", document["edges"][0]["route"])
        self.assertEqual(0, document["edges"][0]["priority"])
        self.assertFalse(document["edges"][0]["back_edge"])
        self.assertEqual({}, document["nodes"][0]["config"])

    def test_what_the_author_wrote_survives_unchanged(self) -> None:
        document = AuthoredWorkflow.model_validate(AUTHORED).to_dsl_document()
        for key in ("dsl_version", "entry", "terminals", "result"):
            self.assertEqual(AUTHORED[key], document[key])
        self.assertEqual(AUTHORED["metadata"]["id"], document["metadata"]["id"])
        self.assertEqual(
            [node["id"] for node in AUTHORED["nodes"]],
            [node["id"] for node in document["nodes"]],
        )

    def test_edge_endpoints_keep_their_dsl_names(self) -> None:
        workflow = AuthoredWorkflow.model_validate(AUTHORED)
        edge = workflow.to_dsl_document()["edges"][0]
        self.assertIn("from", edge)
        self.assertIn("to", edge)
        self.assertNotIn("source", edge)

    def test_absent_optionals_are_dropped_rather_than_emitted_as_null(self) -> None:
        node = AuthoredWorkflow.model_validate(AUTHORED).to_dsl_document()["nodes"][1]
        self.assertNotIn("handler", node)
        self.assertNotIn("label", node)
        self.assertNotIn("route_mode", node)

    def test_unknown_field_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            AuthoredWorkflow.model_validate(_authored(surprise="value"))

    def test_json_schema_exposes_the_aliased_edge_endpoints(self) -> None:
        schema = authoring_json_schema()
        edge = schema["$defs"]["Edge"]["properties"]
        self.assertIn("from", edge)
        self.assertIn("to", edge)


class AuthoringBoundaryTests(unittest.TestCase):
    """The subset is the point: unsupported kinds must be unrepresentable."""

    def test_only_langgraph_node_kinds_are_admitted(self) -> None:
        self.assertEqual(
            ("action", "decision", "human", "join", "terminal"), LANGGRAPH_NODE_KINDS
        )

    def test_legacy_runtime_node_kinds_are_rejected(self) -> None:
        for kind in ("agentic", "foreach", "subflow", "extension"):
            document = _authored()
            document["nodes"][0]["kind"] = kind
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                AuthoredWorkflow.model_validate(document)

    def test_node_extension_field_is_not_expressible(self) -> None:
        document = _authored()
        document["nodes"][0]["extension"] = {
            "extension_id": "x", "extension_version": "1.0", "config": {},
        }
        with self.assertRaises(ValidationError):
            AuthoredWorkflow.model_validate(document)

    def test_top_level_extensions_are_not_expressible(self) -> None:
        with self.assertRaises(ValidationError):
            AuthoredWorkflow.model_validate(_authored(extensions=[]))

    def test_a_handler_reference_cannot_carry_its_own_fingerprint(self) -> None:
        document = _authored()
        document["nodes"][0]["handler"]["fingerprint"] = "deadbeef"
        with self.assertRaises(ValidationError):
            AuthoredWorkflow.model_validate(document)


class InternalConsistencyTests(unittest.TestCase):
    def _message(self, document: dict) -> str:
        with self.assertRaises(ValidationError) as caught:
            AuthoredWorkflow.model_validate(document)
        return str(caught.exception)

    def test_duplicate_node_ids_are_named(self) -> None:
        document = _authored()
        document["nodes"].append(dict(document["nodes"][0]))
        self.assertIn("duplicate node id 'draft'", self._message(document))

    def test_duplicate_edge_ids_are_named(self) -> None:
        document = _authored()
        document["edges"].append(dict(document["edges"][0]))
        self.assertIn("duplicate edge id 'draft_done'", self._message(document))

    def test_entry_naming_an_unknown_node_is_rejected(self) -> None:
        self.assertIn(
            "entry names unknown node 'nowhere'",
            self._message(_authored(entry=["nowhere"])),
        )

    def test_terminals_naming_an_unknown_node_is_rejected(self) -> None:
        self.assertIn(
            "terminals names unknown node 'nowhere'",
            self._message(_authored(terminals=["nowhere"])),
        )

    def test_edge_source_must_name_a_declared_output_port(self) -> None:
        document = _authored()
        document["edges"][0]["from"]["port"] = "absent"
        self.assertIn("does not declare as an output", self._message(document))

    def test_edge_target_must_name_a_declared_input_port(self) -> None:
        document = _authored()
        document["edges"][0]["to"]["port"] = "absent"
        self.assertIn("does not declare as an input", self._message(document))

    def test_edge_to_an_unknown_node_is_rejected(self) -> None:
        document = _authored()
        document["edges"][0]["to"]["node"] = "nowhere"
        self.assertIn("names unknown node 'nowhere'", self._message(document))

    def test_a_missing_result_is_rejected_at_authoring_time(self) -> None:
        """DSL 1.3 requires one; the compiler would say so a round trip later."""

        document = _authored()
        del document["result"]
        self.assertIn("result", self._message(document))

    def test_result_must_name_a_declared_output_port(self) -> None:
        self.assertIn(
            "does not declare as an output",
            self._message(_authored(result={"node": "draft", "port": "absent"})),
        )

    def test_node_policy_reference_must_resolve(self) -> None:
        document = _authored()
        document["nodes"][0]["policies"] = ["retry_twice"]
        self.assertIn("names unknown policy 'retry_twice'", self._message(document))

    def test_back_edge_without_a_policy_is_rejected(self) -> None:
        document = _authored()
        document["edges"][0]["back_edge"] = True
        self.assertIn("requires a loop or rework policy", self._message(document))

    def test_back_edge_with_a_declared_policy_is_accepted(self) -> None:
        document = _authored(
            policies=[
                {"id": "bounded", "kind": "loop", "config": {"max_iterations": 3}}
            ]
        )
        document["edges"][0]["back_edge"] = True
        document["edges"][0]["policy"] = "bounded"
        emitted = AuthoredWorkflow.model_validate(document).to_dsl_document()
        self.assertTrue(emitted["edges"][0]["back_edge"])
        self.assertEqual("bounded", emitted["edges"][0]["policy"])

    def test_every_problem_is_reported_together(self) -> None:
        message = self._message(_authored(entry=["nowhere"], terminals=["elsewhere"]))
        self.assertIn("entry names unknown node 'nowhere'", message)
        self.assertIn("terminals names unknown node 'elsewhere'", message)


class CompilesThroughTheRealPipelineTests(unittest.TestCase):
    """`to_dsl_document()` has to be what `compile_source` already accepts."""

    def setUp(self) -> None:
        self.schemas = InMemorySchemaCatalog(
            {"example://request/1.0": {"type": "object"}}
        )
        self.handlers = InMemoryHandlerCatalog(
            [
                HandlerManifest(
                    name="collect",
                    version="1.2.0",
                    node_kinds=("action",),
                    inputs={},
                    outputs={"request": "example://request/1.0"},
                    config_schema={"type": "object", "additionalProperties": False},
                    execution_safety=ExecutionSafety.REPLAY_SAFE,
                    resource_profile=ResourceProfile(0, 0, 0, 60, 0, "free"),
                    result_schema_id="example://request/1.0",
                )
            ]
        )

    def _compile(self, document: dict):
        return compile_source(
            json.dumps(document), self.handlers, self.schemas, source_format="json"
        )

    def test_authored_document_passes_structural_validation(self) -> None:
        workflow = AuthoredWorkflow.model_validate(AUTHORED)
        document = parse_dsl(
            json.dumps(workflow.to_dsl_document()), source_format="json"
        )
        validate_dsl_structure(document)

    def test_authored_document_compiles_to_ir(self) -> None:
        workflow = AuthoredWorkflow.model_validate(AUTHORED)
        compiled = compile_source(
            json.dumps(workflow.to_dsl_document()),
            self.handlers,
            self.schemas,
            source_format="json",
        )
        self.assertEqual("workflow:summarize_flow", compiled.ir.workflow_id)
        self.assertEqual(("done",), compiled.ir.terminals)

    def test_emitted_normal_form_compiles_to_the_same_ir_as_the_original(self) -> None:
        emitted = AuthoredWorkflow.model_validate(AUTHORED).to_dsl_document()
        self.assertEqual(
            self._compile(AUTHORED).definition_hash,
            self._compile(emitted).definition_hash,
        )

    def test_the_compiler_resolves_the_fingerprint_the_author_never_supplied(
        self,
    ) -> None:
        workflow = AuthoredWorkflow.model_validate(AUTHORED)
        self.assertEqual(
            {"name", "version"}, set(workflow.nodes[0].handler.model_dump())
        )
        compiled = self._compile(workflow.to_dsl_document())
        handler = next(
            node.handler for node in compiled.ir.nodes if node.id == "draft"
        )
        # The author wrote "^1.0" and no fingerprint; the catalog decided both.
        self.assertEqual("1.2.0", handler.version)
        self.assertTrue(handler.manifest_fingerprint)


if __name__ == "__main__":
    unittest.main()
