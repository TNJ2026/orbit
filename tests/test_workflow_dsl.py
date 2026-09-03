from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from orbit.workflow.domain.serialization import to_primitive
from orbit.workflow.catalogs import (
    ExtensionManifest,
    HandlerManifest,
    InMemoryExtensionRegistry,
    InMemoryHandlerCatalog,
    InMemorySchemaCatalog,
)
from orbit.workflow.domain.definitions import IRHandlerRef
from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.handlers import ResourceProfile
from orbit.workflow.domain.ir_schema import validate_workflow_ir, workflow_ir_from_primitive
from orbit.workflow.domain.schemas import SchemaValidationError
from orbit.workflow.application import load_catalogs
from orbit.workflow.dsl import (
    DiagnosticError,
    analyze_dsl,
    canonical_ir_json,
    compile_source,
    parse_dsl,
    parse_dsl_file,
    validate_dsl_structure,
)
from orbit.workflow.dsl.semantic import _find_cycle


VALID_DSL = {
    "dsl_version": "1.0",
    "metadata": {"id": "approval_flow", "name": "Approval flow"},
    "nodes": [
        {
            "id": "collect",
            "kind": "action",
            "outputs": [{"id": "request", "schema_id": "example://request/1.0"}],
            "handler": {"name": "collect", "version": "^1.0"},
        },
        {
            "id": "done",
            "kind": "terminal",
            "inputs": [{"id": "request", "schema_id": "example://request/1.0"}],
        },
    ],
    "edges": [
        {
            "id": "collect_done",
            "from": {"node": "collect", "port": "request"},
            "to": {"node": "done", "port": "request"},
        }
    ],
    "entry": ["collect"],
    "terminals": ["done"],
}


class WorkflowDslParserTests(unittest.TestCase):
    def test_equivalent_yaml_and_json_parse_to_same_object(self) -> None:
        json_document = parse_dsl(json.dumps(VALID_DSL), source_format="json")
        yaml_document = parse_dsl(
            """
dsl_version: "1.0"
metadata:
  id: approval_flow
  name: Approval flow
nodes:
  - id: collect
    kind: action
    outputs:
      - id: request
        schema_id: example://request/1.0
    handler:
      name: collect
      version: ^1.0
  - id: done
    kind: terminal
    inputs:
      - id: request
        schema_id: example://request/1.0
edges:
  - id: collect_done
    from: {node: collect, port: request}
    to: {node: done, port: request}
entry: [collect]
terminals: [done]
""",
            source_name="flow.yaml",
            source_format="yaml",
        )
        self.assertEqual(to_primitive(json_document.data), to_primitive(yaml_document.data))
        self.assertEqual("yaml", yaml_document.source_format)
        self.assertEqual(4, yaml_document.source_map[("metadata", "id")].start_line)

    def test_duplicate_keys_are_rejected_in_both_formats(self) -> None:
        for text, source_format in [
            ('{"dsl_version":"1.0","dsl_version":"1.0"}', "json"),
            ('dsl_version: "1.0"\ndsl_version: "1.0"\n', "yaml"),
        ]:
            with self.subTest(source_format=source_format):
                with self.assertRaises(DiagnosticError) as raised:
                    parse_dsl(text, source_format=source_format)
                self.assertEqual("DSL_DUPLICATE_KEY", raised.exception.diagnostics[0].code)

    def test_yaml_dates_and_ambiguous_booleans_remain_strings(self) -> None:
        document = parse_dsl(
            "date: 2026-07-17\nanswer: yes\nenabled: true\n",
            source_format="yaml",
        )
        self.assertEqual("2026-07-17", document.data["date"])
        self.assertEqual("yes", document.data["answer"])
        self.assertIs(True, document.data["enabled"])

    def test_non_finite_json_number_is_rejected(self) -> None:
        with self.assertRaises(DiagnosticError) as raised:
            parse_dsl('{"value": NaN}', source_format="json")
        self.assertEqual("DSL_PARSE_ERROR", raised.exception.diagnostics[0].code)

    def test_yaml_alias_limit_is_enforced(self) -> None:
        aliases = "\n".join(f"item_{index}: *shared" for index in range(51))
        with self.assertRaises(DiagnosticError) as raised:
            parse_dsl(f"shared: &shared value\n{aliases}\n", source_format="yaml")
        self.assertEqual("DSL_UNSAFE_YAML", raised.exception.diagnostics[0].code)

    def test_deep_json_and_yaml_return_diagnostics_not_recursion_errors(self) -> None:
        nested = "value"
        for _ in range(140):
            nested = {"child": nested}
        with self.assertRaises(DiagnosticError) as json_error:
            parse_dsl(json.dumps(nested), source_format="json")
        self.assertEqual("DSL_PARSE_ERROR", json_error.exception.diagnostics[0].code)

        yaml_source = "  " * 140 + "leaf: value\n"
        for depth in range(139, -1, -1):
            yaml_source = "  " * depth + "child:\n" + yaml_source
        with self.assertRaises(DiagnosticError) as yaml_error:
            parse_dsl(yaml_source, source_format="yaml")
        self.assertIn(yaml_error.exception.diagnostics[0].code, {"DSL_PARSE_ERROR", "DSL_UNSAFE_YAML"})

    def test_parse_file_accepts_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "workflow.json"
            path.write_text("\ufeff" + json.dumps(VALID_DSL), encoding="utf-8")
            document = parse_dsl_file(path)
        self.assertEqual("approval_flow", document.data["metadata"]["id"])


class WorkflowDslSchemaTests(unittest.TestCase):
    def test_valid_document_passes_structural_validation(self) -> None:
        document = parse_dsl(json.dumps(VALID_DSL), source_format="json")
        self.assertEqual((), validate_dsl_structure(document))

    def test_unknown_field_has_exact_path(self) -> None:
        value = dict(VALID_DSL)
        value["surprise"] = True
        document = parse_dsl(json.dumps(value), source_format="json")
        with self.assertRaises(DiagnosticError) as raised:
            validate_dsl_structure(document)
        diagnostic = raised.exception.diagnostics[0]
        self.assertEqual("DSL_SCHEMA_ERROR", diagnostic.code)
        self.assertEqual("$", diagnostic.json_path)
        self.assertIn("'surprise'", diagnostic.message)

    def test_missing_required_field_path_uses_yaml_source_context(self) -> None:
        document = parse_dsl(
            'dsl_version: "1.0"\nmetadata:\n  id: flow\nnodes: []\nedges: []\nentry: [start]\nterminals: [done]\n',
            source_name="broken.yaml",
            source_format="yaml",
        )
        with self.assertRaises(DiagnosticError) as raised:
            validate_dsl_structure(document)
        diagnostic = next(item for item in raised.exception.diagnostics if item.path == ("metadata", "name"))
        self.assertEqual("DSL_SCHEMA_ERROR", diagnostic.code)
        self.assertEqual("$.metadata.name", diagnostic.json_path)

    def test_unsupported_version_has_stable_code(self) -> None:
        value = dict(VALID_DSL)
        value["dsl_version"] = "2.0"
        document = parse_dsl(json.dumps(value), source_format="json")
        with self.assertRaises(DiagnosticError) as raised:
            validate_dsl_structure(document)
        self.assertEqual("DSL_UNSUPPORTED_VERSION", raised.exception.diagnostics[0].code)

    def test_json_schema_error_has_source_location(self) -> None:
        value = dict(VALID_DSL)
        value["dsl_version"] = "9.0"
        document = parse_dsl(
            json.dumps(value, indent=2),
            source_name="broken.json",
            source_format="json",
        )
        with self.assertRaises(DiagnosticError) as raised:
            validate_dsl_structure(document)
        diagnostic = next(item for item in raised.exception.diagnostics if item.path == ("dsl_version",))
        self.assertIsNotNone(diagnostic.source_range)
        self.assertEqual("broken.json", diagnostic.source_range.source)
        self.assertEqual(2, diagnostic.source_range.start_line)


class WorkflowDslSemanticTests(unittest.TestCase):
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

    def document(self, value: dict | None = None):
        document = parse_dsl(json.dumps(value or VALID_DSL), source_format="json")
        validate_dsl_structure(document)
        return document

    def test_valid_graph_resolves_exact_handler_version_and_indexes(self) -> None:
        analysis = analyze_dsl(self.document(), self.handlers, self.schemas)
        self.assertEqual("1.2.0", analysis.handlers["collect"].version)
        self.assertEqual(("done",), analysis.outgoing["collect"])
        self.assertEqual(("collect",), analysis.incoming["done"])

    def test_missing_handler_is_a_semantic_diagnostic(self) -> None:
        value = json.loads(json.dumps(VALID_DSL))
        value["nodes"][0]["handler"]["name"] = "missing"
        with self.assertRaises(DiagnosticError) as raised:
            analyze_dsl(self.document(value), self.handlers, self.schemas)
        self.assertIn("DSL_HANDLER_NOT_FOUND", {item.code for item in raised.exception.diagnostics})

    def test_cycle_and_terminal_outgoing_are_rejected(self) -> None:
        value = json.loads(json.dumps(VALID_DSL))
        value["nodes"][0]["inputs"] = [
            {"id": "request", "schema_id": "example://request/1.0"}
        ]
        value["edges"].append(
            {
                "id": "done_collect",
                "from": {"node": "done", "port": "request"},
                "to": {"node": "collect", "port": "request"},
            }
        )
        value["nodes"][1]["outputs"] = [
            {"id": "request", "schema_id": "example://request/1.0"}
        ]
        with self.assertRaises(DiagnosticError) as raised:
            analyze_dsl(self.document(value), self.handlers, self.schemas)
        codes = {item.code for item in raised.exception.diagnostics}
        self.assertIn("DSL_GRAPH_CYCLE", codes)
        self.assertIn("DSL_GRAPH_NO_TERMINAL_PATH", codes)

    def test_ir_handler_reference_requires_exact_version(self) -> None:
        with self.assertRaises(ValueError):
            IRHandlerRef("collect", "^1.0", "sha256:" + "a" * 64)

    def test_artifact_port_policy_is_normalized_into_ir_1_1(self) -> None:
        value = json.loads(json.dumps(VALID_DSL))
        policy = {
            "transport": "artifact_ref", "max_size_bytes": 4096,
            "content_types": ["application/json"], "visibility": "run",
        }
        value["nodes"][0]["outputs"][0].update(policy)
        value["nodes"][1]["inputs"][0].update(policy)
        compiled = compile_source(
            json.dumps(value), self.handlers, self.schemas, source_format="json"
        )
        self.assertEqual("1.1", compiled.ir.ir_version)
        output = compiled.ir.nodes[0].outputs[0]
        self.assertEqual("artifact_ref", output.data_policy.transport.value)
        self.assertEqual(("application/json",), output.data_policy.content_types)

    def test_human_output_schema_must_accept_the_submission_shape(self) -> None:
        # A schema that rejects {"decision": ..., "value": null} would publish
        # fine and then fail every submit — the task could never be answered.
        schemas = InMemorySchemaCatalog({
            "example://request/1.0": {"type": "object"},
            "example://integer/1.0": {"type": "integer"},
        })
        value = {
            "dsl_version": "1.2",
            "metadata": {"id": "human_bad_port", "name": "Human"},
            "nodes": [
                {
                    "id": "approve", "kind": "human",
                    "inputs": [{"id": "value", "schema_id": "example://request/1.0"}],
                    "outputs": [{"id": "result", "schema_id": "example://integer/1.0"}],
                    "config": {
                        "task_kind": "approval", "participants": ["local"],
                        "quorum": "any",
                    },
                },
                {
                    "id": "done", "kind": "terminal",
                    "inputs": [{"id": "result", "schema_id": "example://integer/1.0"}],
                },
            ],
            "edges": [{
                "id": "approved",
                "from": {"node": "approve", "port": "result"},
                "to": {"node": "done", "port": "result"},
            }],
            "entry": ["approve"], "terminals": ["done"],
        }
        with self.assertRaises(DiagnosticError) as raised:
            compile_source(
                json.dumps(value), self.handlers, schemas, source_format="json"
            )
        self.assertIn(
            "DSL_PORT_INCOMPATIBLE",
            {item.code for item in raised.exception.diagnostics},
        )
        # The permissive object schema accepts the shape, so the same graph
        # with that port publishes cleanly.
        value["nodes"][0]["outputs"][0]["schema_id"] = "example://request/1.0"
        value["nodes"][1]["inputs"][0]["schema_id"] = "example://request/1.0"
        compiled = compile_source(
            json.dumps(value), self.handlers, schemas, source_format="json"
        )
        self.assertEqual("human", compiled.ir.nodes[0].kind)

    def test_join_merge_mode_must_match_its_declared_output_schema(self) -> None:
        port = lambda name: [{
            "id": name, "schema_id": "example://request/1.0",
        }]
        value = {
            "dsl_version": "1.2",
            "metadata": {"id": "typed_join", "name": "Typed join"},
            "nodes": [
                {"id": "fork", "kind": "decision", "outputs": port("value"), "route_mode": "parallel"},
                {"id": "left", "kind": "decision", "inputs": port("value"), "outputs": port("value")},
                {"id": "right", "kind": "decision", "inputs": port("value"), "outputs": port("value")},
                {"id": "join", "kind": "join", "inputs": port("value"), "outputs": port("value"), "policies": ["join_all"]},
                {"id": "done", "kind": "terminal", "inputs": port("value")},
            ],
            "edges": [
                {"id": "fork_left", "from": {"node": "fork", "port": "value"}, "to": {"node": "left", "port": "value"}},
                {"id": "fork_right", "from": {"node": "fork", "port": "value"}, "to": {"node": "right", "port": "value"}},
                {"id": "left_join", "from": {"node": "left", "port": "value"}, "to": {"node": "join", "port": "value"}},
                {"id": "right_join", "from": {"node": "right", "port": "value"}, "to": {"node": "join", "port": "value"}},
                {"id": "join_done", "from": {"node": "join", "port": "value"}, "to": {"node": "done", "port": "value"}},
            ],
            "entry": ["fork"], "terminals": ["done"],
            "policies": [{
                "id": "join_all", "kind": "join",
                "config": {"mode": "all", "merge_mode": "array_by_edge"},
            }],
        }

        with self.assertRaises(DiagnosticError) as raised:
            compile_source(
                json.dumps(value), self.handlers, self.schemas, source_format="json",
            )
        finding = next(
            item for item in raised.exception.diagnostics
            if item.code == "DSL_JOIN_INVALID"
        )
        self.assertIn("produces an array", finding.message)

        value["policies"][0]["config"]["merge_mode"] = "object_by_edge"
        compile_source(
            json.dumps(value), self.handlers, self.schemas, source_format="json",
        )

    def test_artifact_and_secret_edges_fail_closed(self) -> None:
        artifact = json.loads(json.dumps(VALID_DSL))
        for port in (
            artifact["nodes"][0]["outputs"][0],
            artifact["nodes"][1]["inputs"][0],
        ):
            port.update({"transport": "artifact_ref", "visibility": "node"})
        with self.assertRaises(DiagnosticError) as raised:
            compile_source(
                json.dumps(artifact), self.handlers, self.schemas,
                source_format="json",
            )
        self.assertIn(
            "DSL_PORT_INCOMPATIBLE",
            {item.code for item in raised.exception.diagnostics},
        )

        secret = json.loads(json.dumps(VALID_DSL))
        for port in (
            secret["nodes"][0]["outputs"][0],
            secret["nodes"][1]["inputs"][0],
        ):
            port["transport"] = "secret_ref"
        secret["edges"][0]["mapping"] = {
            "schema_id": "example://request/1.0", "value": "$source.request"
        }
        with self.assertRaises(DiagnosticError) as raised:
            compile_source(
                json.dumps(secret), self.handlers, self.schemas,
                source_format="json",
            )
        self.assertIn(
            "DSL_MAPPING_INVALID",
            {item.code for item in raised.exception.diagnostics},
        )

    def test_compiler_normalizes_order_defaults_and_handler_version(self) -> None:
        value = json.loads(json.dumps(VALID_DSL))
        value["edges"][0]["condition"] = "source.request.approved == True"
        first = compile_source(json.dumps(value), self.handlers, self.schemas, source_format="json")
        value["nodes"].reverse()
        second = compile_source(json.dumps(value, indent=2), self.handlers, self.schemas, source_format="json")
        self.assertEqual(first.definition_hash, second.definition_hash)
        self.assertEqual(canonical_ir_json(first), canonical_ir_json(second))
        self.assertEqual("1.2.0", first.ir.nodes[0].handler.version)
        self.assertEqual("workflow:approval_flow", first.ir.workflow_id)
        self.assertEqual({"op": "eq", "left": {"op": "ref", "path": "source.request.approved"}, "right": {"op": "literal", "value": True}}, to_primitive(first.ir.edges[0].condition))
        self.assertEqual({"op": "identity", "schema_id": "example://request/1.0"}, to_primitive(first.ir.edges[0].mapping))

    def test_compiler_rejects_arbitrary_expression_calls(self) -> None:
        value = json.loads(json.dumps(VALID_DSL))
        value["edges"][0]["condition"] = "open('/tmp/unsafe')"
        with self.assertRaises(DiagnosticError) as raised:
            compile_source(json.dumps(value), self.handlers, self.schemas, source_format="json")
        self.assertEqual("DSL_EXPRESSION_INVALID", raised.exception.diagnostics[0].code)

    def test_mapping_is_compiled_to_structured_ast(self) -> None:
        value = json.loads(json.dumps(VALID_DSL))
        value["edges"][0]["mapping"] = {
            "schema_id": "example://request/1.0",
            "value": {"id": "$source.request.id", "approved": False},
        }
        compiled = compile_source(json.dumps(value), self.handlers, self.schemas, source_format="json")
        mapping = to_primitive(compiled.ir.edges[0].mapping)
        self.assertEqual("map", mapping["op"])
        self.assertEqual({"op": "ref", "path": "source.request.id"}, mapping["value"]["fields"]["id"])

    def test_a_bare_edge_between_same_named_ports_stays_identity(self) -> None:
        compiled = compile_source(
            json.dumps(VALID_DSL), self.handlers, self.schemas, source_format="json",
        )
        mapping = to_primitive(compiled.ir.edges[0].mapping)
        self.assertEqual("identity", mapping["op"])

    def test_a_bare_edge_across_differently_named_ports_renames(self) -> None:
        """`request` out, `prompt` in: identity would hand the target `request`.

        The downstream node requires `prompt`, so a bare edge across the two
        must compile to the rename it plainly means, or the run only fails at
        the target with `missing required input ports`.
        """

        value = json.loads(json.dumps(VALID_DSL))
        value["nodes"][1]["inputs"] = [
            {"id": "prompt", "schema_id": "example://request/1.0"}
        ]
        value["edges"][0]["to"]["port"] = "prompt"
        compiled = compile_source(
            json.dumps(value), self.handlers, self.schemas, source_format="json",
        )
        mapping = to_primitive(compiled.ir.edges[0].mapping)
        self.assertEqual("object", mapping["op"])
        self.assertEqual(
            {"prompt": {"op": "ref", "path": "source.request"}}, mapping["fields"]
        )

    def test_workflow_ir_schema_round_trip_is_lossless(self) -> None:
        compiled = compile_source(json.dumps(VALID_DSL), self.handlers, self.schemas, source_format="json")
        primitive = to_primitive(compiled.ir)
        validate_workflow_ir(primitive)
        restored = workflow_ir_from_primitive(primitive)
        self.assertEqual(canonical_ir_json(compiled), json.dumps(to_primitive(restored), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        broken = dict(primitive)
        broken.pop("ir_version")
        with self.assertRaises(SchemaValidationError) as raised:
            validate_workflow_ir(broken)
        self.assertEqual("$.ir_version", raised.exception.json_path)

    def test_expression_and_mapping_references_are_scope_checked(self) -> None:
        value = json.loads(json.dumps(VALID_DSL))
        value["edges"][0]["condition"] = "other.secret == True"
        with self.assertRaises(DiagnosticError) as raised:
            compile_source(json.dumps(value), self.handlers, self.schemas, source_format="json")
        self.assertEqual("DSL_REFERENCE_NOT_FOUND", raised.exception.diagnostics[0].code)

    def test_extension_requires_registered_version_and_schema(self) -> None:
        value = json.loads(json.dumps(VALID_DSL))
        value["extensions"] = [
            {
                "extension_id": "orbit.agentic-region",
                "extension_version": "draft-1",
                "config": {"region": "main"},
            }
        ]
        with self.assertRaises(DiagnosticError) as raised:
            compile_source(json.dumps(value), self.handlers, self.schemas, source_format="json")
        self.assertEqual("DSL_UNSUPPORTED_VERSION", raised.exception.diagnostics[0].code)
        registry = InMemoryExtensionRegistry(
            [
                ExtensionManifest(
                    "orbit.agentic-region",
                    "draft-1",
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["region"],
                        "properties": {"region": {"type": "string"}},
                    },
                )
            ]
        )
        compiled = compile_source(
            json.dumps(value), self.handlers, self.schemas,
            source_format="json", extensions=registry,
        )
        self.assertEqual("orbit.agentic-region", compiled.ir.extensions[0].extension_id)

    def test_ui_structured_condition_ast_matches_human_string(self) -> None:
        text_value = json.loads(json.dumps(VALID_DSL))
        text_value["edges"][0]["condition"] = "source.request.approved == True"
        ast_value = json.loads(json.dumps(VALID_DSL))
        ast_value["edges"][0]["condition"] = {
            "op": "eq",
            "left": {"op": "ref", "path": "source.request.approved"},
            "right": {"op": "literal", "value": True},
        }
        text_compiled = compile_source(json.dumps(text_value), self.handlers, self.schemas, source_format="json")
        ast_compiled = compile_source(json.dumps(ast_value), self.handlers, self.schemas, source_format="json")
        self.assertEqual(text_compiled.definition_hash, ast_compiled.definition_hash)

    def test_entry_may_be_terminal_for_zero_step_workflow(self) -> None:
        value = {
            "dsl_version": "1.0",
            "metadata": {"id": "empty", "name": "Empty"},
            "nodes": [{"id": "done", "kind": "terminal"}],
            "edges": [],
            "entry": ["done"],
            "terminals": ["done"],
        }
        compiled = compile_source(json.dumps(value), self.handlers, self.schemas, source_format="json")
        self.assertEqual(("done",), compiled.ir.entry)

    def test_long_graph_cycle_detection_is_iterative(self) -> None:
        outgoing = {f"n{index}": [f"n{index + 1}"] for index in range(5000)}
        outgoing["n5000"] = []
        nodes = set(outgoing)
        self.assertIsNone(_find_cycle(nodes, outgoing))
        outgoing["n5000"] = ["n0"]
        cycle = _find_cycle(nodes, outgoing)
        self.assertEqual("n0", cycle[0])
        self.assertEqual("n0", cycle[-1])


class ArtifactEdgeTests(unittest.TestCase):
    """A bare artifact edge may rename ports; an explicit one may not.

    Large data rides an artifact_ref port, and one node's `result` feeds the
    next node's `prompt` — different names. A bare edge across the two must
    carry the reference to the target port, exactly as an inline edge would,
    while a transform mapping on an artifact edge stays forbidden so a blob's
    reference can never be rewritten in flight.
    """

    ARTIFACT_POLICY = {
        "transport": "artifact_ref", "max_size_bytes": 1_000_000,
        "content_types": ["text/plain"], "visibility": "run",
    }

    def setUp(self) -> None:
        self.schemas = InMemorySchemaCatalog({"x://blob/1.0": {"type": "object"}})
        self.handlers = InMemoryHandlerCatalog([
            HandlerManifest(
                name="produce", version="1.0.0", node_kinds=("action",),
                inputs={}, outputs={"result": "x://blob/1.0"},
                config_schema={"type": "object", "additionalProperties": False},
                execution_safety=ExecutionSafety.REPLAY_SAFE,
                resource_profile=ResourceProfile(0, 0, 0, 60, 1_000_000, "free"),
                result_schema_id="x://blob/1.0",
            ),
            HandlerManifest(
                name="consume", version="1.0.0", node_kinds=("action",),
                inputs={"prompt": "x://blob/1.0"}, outputs={"result": "x://blob/1.0"},
                config_schema={"type": "object", "additionalProperties": False},
                execution_safety=ExecutionSafety.REPLAY_SAFE,
                resource_profile=ResourceProfile(0, 0, 0, 60, 1_000_000, "free"),
                result_schema_id="x://blob/1.0",
            ),
        ])

    def document(self, edge_extra=None):
        policy = self.ARTIFACT_POLICY
        return {
            "dsl_version": "1.2",
            "metadata": {"id": "blobs", "name": "Blobs"},
            "nodes": [
                {"id": "produce", "kind": "action",
                 "outputs": [{"id": "result", "schema_id": "x://blob/1.0", **policy}],
                 "handler": {"name": "produce", "version": "1.0.0"}},
                {"id": "consume", "kind": "action",
                 "inputs": [{"id": "prompt", "schema_id": "x://blob/1.0", **policy}],
                 "outputs": [{"id": "result", "schema_id": "x://blob/1.0", **policy}],
                 "handler": {"name": "consume", "version": "1.0.0"}},
                {"id": "done", "kind": "terminal",
                 "inputs": [{"id": "result", "schema_id": "x://blob/1.0", **policy}]},
            ],
            "edges": [
                {"id": "p_c", "from": {"node": "produce", "port": "result"},
                 "to": {"node": "consume", "port": "prompt"}, **(edge_extra or {})},
                {"id": "c_d", "from": {"node": "consume", "port": "result"},
                 "to": {"node": "done", "port": "result"}},
            ],
            "entry": ["produce"], "terminals": ["done"],
        }

    def test_a_bare_artifact_edge_carries_the_reference_to_the_target_port(self) -> None:
        compiled = compile_source(
            json.dumps(self.document()), self.handlers, self.schemas,
            source_format="json",
        )
        edge = next(e for e in compiled.ir.edges if e.id == "p_c")
        mapping = to_primitive(edge.mapping)
        self.assertEqual("object", mapping["op"])
        self.assertEqual(
            {"prompt": {"op": "ref", "path": "source.result"}}, mapping["fields"]
        )

    def test_a_condition_cannot_read_artifact_content_as_an_inline_member(self) -> None:
        document = self.document()
        document["edges"][1]["condition"] = {
            "op": "call", "name": "exists",
            "args": [{"op": "ref", "path": "source.result.text"}],
        }

        with self.assertRaises(DiagnosticError) as raised:
            compile_source(
                json.dumps(document), self.handlers, self.schemas,
                source_format="json",
            )

        diagnostic = raised.exception.diagnostics[0]
        self.assertEqual("DSL_EXPRESSION_INVALID", diagnostic.code)
        self.assertIn("artifact_ref", diagnostic.message)
        self.assertIn("source.result.text", diagnostic.message)

    def test_a_transform_mapping_on_an_artifact_edge_is_refused(self) -> None:
        document = self.document(edge_extra={
            "mapping": {"schema_id": "x://blob/1.0",
                        "value": {"artifact_id": "$source.result.artifact_id"}},
        })
        with self.assertRaises(DiagnosticError) as raised:
            compile_source(
                json.dumps(document), self.handlers, self.schemas, source_format="json",
            )
        self.assertIn(
            "DSL_MAPPING_INVALID", [d.code for d in raised.exception.diagnostics]
        )


class WorkflowDslGoldenTests(unittest.TestCase):
    def test_dsl_1_3_result_survives_canonical_ir_round_trip(self) -> None:
        root = Path(__file__).parent / "fixtures" / "workflow_dsl" / "v1"
        catalogs = load_catalogs(root / "catalog.json")
        value = json.loads((root / "linear.json").read_text(encoding="utf-8"))
        value["dsl_version"] = "1.3"
        value["result"] = {"node": "collect", "port": "request"}

        compiled = compile_source(
            json.dumps(value), catalogs.handlers, catalogs.schemas,
            source_format="json", extensions=catalogs.extensions,
        )
        primitive = json.loads(canonical_ir_json(compiled))
        restored = workflow_ir_from_primitive(primitive)

        self.assertEqual("1.3", restored.ir_version)
        self.assertEqual("collect", restored.result.node_id)
        self.assertEqual("request", restored.result.output_port_id)

    def test_dsl_1_3_requires_a_result(self) -> None:
        root = Path(__file__).parent / "fixtures" / "workflow_dsl" / "v1"
        catalogs = load_catalogs(root / "catalog.json")
        value = json.loads((root / "linear.json").read_text(encoding="utf-8"))
        value["dsl_version"] = "1.3"

        with self.assertRaises(DiagnosticError) as raised:
            compile_source(
                json.dumps(value), catalogs.handlers, catalogs.schemas,
                source_format="json", extensions=catalogs.extensions,
            )
        self.assertEqual("DSL_RESULT_REQUIRED", raised.exception.diagnostics[0].code)

    def test_yaml_and_json_match_canonical_ir_and_hash_golden(self) -> None:
        root = Path(__file__).parent / "fixtures"
        dsl_root = root / "workflow_dsl" / "v1"
        ir_root = root / "workflow_ir" / "v1"
        catalogs = load_catalogs(dsl_root / "catalog.json")
        outputs = []
        for filename, source_format in [("linear.json", "json"), ("linear.yaml", "yaml")]:
            source = (dsl_root / filename).read_text(encoding="utf-8")
            outputs.append(
                compile_source(
                    source,
                    catalogs.handlers,
                    catalogs.schemas,
                    source_name=filename,
                    source_format=source_format,
                    extensions=catalogs.extensions,
                )
            )
        expected_ir = (ir_root / "linear.json").read_text(encoding="utf-8").strip()
        expected_hash = (ir_root / "linear.sha256").read_text(encoding="utf-8").strip()
        for compiled in outputs:
            self.assertEqual(expected_ir, canonical_ir_json(compiled))
            self.assertEqual(expected_hash, compiled.definition_hash.value)

    def test_negative_fixture_matrix_emits_registered_codes(self) -> None:
        root = Path(__file__).parent / "fixtures" / "workflow_dsl" / "v1"
        catalogs = load_catalogs(root / "catalog.json")
        cases = json.loads((root / "negative-cases.json").read_text(encoding="utf-8"))
        for case in cases:
            value = json.loads((root / "linear.json").read_text(encoding="utf-8"))
            mutation = case["mutation"]
            if mutation == "unsupported_version":
                value["dsl_version"] = "9.0"
            elif mutation == "unknown_field":
                value["unknown"] = True
            elif mutation == "too_many_errors":
                value["nodes"] = [{} for _ in range(101)]
            elif mutation == "duplicate_id":
                value["nodes"].append(json.loads(json.dumps(value["nodes"][0])))
            elif mutation == "missing_handler":
                value["nodes"][0]["handler"]["name"] = "missing"
            elif mutation == "missing_node":
                value["edges"][0]["to"]["node"] = "missing"
            elif mutation == "cycle":
                value["nodes"][0]["inputs"] = [{"id": "request", "schema_id": "example://request/1.0"}]
                value["nodes"][1]["outputs"] = [{"id": "request", "schema_id": "example://request/1.0"}]
                value["edges"].append({"id": "back", "from": {"node": "done", "port": "request"}, "to": {"node": "collect", "port": "request"}})
            elif mutation == "unreachable":
                value["nodes"].append({"id": "orphan", "kind": "terminal"})
                value["terminals"].append("orphan")
            elif mutation == "no_terminal_path":
                value["edges"] = []
            elif mutation == "incompatible_port":
                value["nodes"][1]["inputs"][0]["schema_id"] = "example://other/1.0"
            elif mutation == "invalid_expression":
                value["edges"][0]["condition"] = "open('/tmp/no')"
            elif mutation == "invalid_mapping":
                value["edges"][0]["mapping"] = {"value": "$source.request"}
            with self.subTest(case=case["id"]), self.assertRaises(DiagnosticError) as raised:
                compile_source(
                    json.dumps(value), catalogs.handlers, catalogs.schemas,
                    source_format="json", extensions=catalogs.extensions,
                )
            self.assertIn(case["expected_code"], {item.code for item in raised.exception.diagnostics})


class NodeLabelTests(unittest.TestCase):
    """A step's reader-facing name is a node field, not handler config.

    The label exists because people read the flow: the execution page names the
    current step with it and the diagram draws it. It deliberately sits beside
    `config` rather than inside, because `config` is validated against the
    Handler's own schema — a Handler that closes its config would otherwise
    reject the one field that was never meant for it.
    """

    def setUp(self) -> None:
        self.schemas = InMemorySchemaCatalog(
            {"example://request/1.0": {"type": "object"}}
        )

    def catalog(self, *, closed_config: bool):
        return InMemoryHandlerCatalog([
            HandlerManifest(
                "collect", "1.2.0", ("action",), {},
                {"request": "example://request/1.0"},
                {"type": "object", "additionalProperties": False}
                if closed_config else {"type": "object"},
                ExecutionSafety.REPLAY_SAFE,
                ResourceProfile(0, 0, 0, 60, 0, "free"),
                "example://request/1.0", (), (), True, True,
            )
        ])

    def document(self, **node_fields) -> dict:
        return {
            "dsl_version": "1.3",
            "metadata": {"id": "labelled", "name": "Labelled"},
            "nodes": [
                {
                    "id": "collect", "kind": "action",
                    "outputs": [{"id": "request", "schema_id": "example://request/1.0"}],
                    "handler": {"name": "collect", "version": "1.2.0"},
                    **node_fields,
                },
                {
                    "id": "done", "kind": "terminal",
                    "inputs": [{"id": "request", "schema_id": "example://request/1.0"}],
                },
            ],
            "edges": [{
                "id": "flow", "from": {"node": "collect", "port": "request"},
                "to": {"node": "done", "port": "request"},
            }],
            "entry": ["collect"], "terminals": ["done"],
            "result": {"node": "collect", "port": "request"},
        }

    def compile(self, document, *, closed_config=False):
        return compile_source(
            json.dumps(document), self.catalog(closed_config=closed_config),
            self.schemas, source_format="json",
        )

    def test_a_label_survives_a_handler_that_closes_its_config(self) -> None:
        compiled = self.compile(
            self.document(label="整理销售数据"), closed_config=True,
        )
        self.assertEqual("整理销售数据", compiled.ir.nodes[0].label)

    def test_the_same_name_inside_config_is_refused_by_the_handler(self) -> None:
        """Which is exactly why the label is not carried in config."""

        with self.assertRaises(DiagnosticError) as raised:
            self.compile(
                self.document(config={"display_name": "整理销售数据"}),
                closed_config=True,
            )
        self.assertIn(
            "DSL_SCHEMA_ERROR", {item.code for item in raised.exception.diagnostics},
        )

    def test_a_definition_without_labels_still_compiles(self) -> None:
        """Every definition published before labels existed must keep working."""

        compiled = self.compile(self.document())
        self.assertIsNone(compiled.ir.nodes[0].label)

    def test_a_label_is_bounded_and_cannot_be_blank(self) -> None:
        for label in ("", "   ", "x" * 81):
            with self.subTest(label=label):
                with self.assertRaises(DiagnosticError):
                    self.compile(self.document(label=label))

    def test_a_label_survives_the_canonical_ir_round_trip(self) -> None:
        from orbit.workflow.domain.ir_schema import workflow_ir_from_primitive
        from orbit.workflow.domain.serialization import to_primitive

        compiled = self.compile(self.document(label="Collect the data"))
        restored = workflow_ir_from_primitive(to_primitive(compiled.ir))
        self.assertEqual("Collect the data", restored.nodes[0].label)


if __name__ == "__main__":
    unittest.main()


class RetryOnAHandlerlessNodeTests(unittest.TestCase):
    """Retry re-runs a Handler's work; some nodes have none to re-run.

    This was accepted here and refused by the engine as a node whose "retry
    policy requires a retry-safe Handler" — a Handler that kind may not
    declare. The author was told to fix something the contract forbids them
    to have. Refused where the concept lives, saying what is actually wrong.
    """

    OBJECT = "example://request/1.0"

    def setUp(self) -> None:
        self.schemas = InMemorySchemaCatalog({
            self.OBJECT: {"type": "object"},
            "schema://object/1.0": {"type": "object"},
        })
        self.handlers = InMemoryHandlerCatalog([
            HandlerManifest(
                name="collect", version="1.2.0", node_kinds=("action",),
                inputs={"prompt": self.OBJECT}, outputs={"result": self.OBJECT},
                config_schema={"type": "object", "additionalProperties": False},
                execution_safety=ExecutionSafety.REPLAY_SAFE,
                resource_profile=ResourceProfile(0, 0, 0, 60, 0, "free"),
                result_schema_id=self.OBJECT,
            )
        ])

    def document(self, *, retried: str):
        submission = {"id": "result", "schema_id": "schema://object/1.0"}
        return {
            "dsl_version": "1.3",
            "metadata": {"id": "chased", "name": "Chased"},
            "nodes": [
                {
                    "id": "act", "kind": "action",
                    "inputs": [{"id": "prompt", "schema_id": self.OBJECT}],
                    "outputs": [{"id": "result", "schema_id": self.OBJECT}],
                    "handler": {"name": "collect", "version": "1.2.0"},
                    "policies": ["again"] if retried == "act" else [],
                },
                {
                    "id": "ask", "kind": "human",
                    "inputs": [{"id": "result", "schema_id": self.OBJECT}],
                    "outputs": [dict(submission)],
                    "config": {
                        "task_kind": "approval", "participants": ["local"],
                        "quorum": "any",
                    },
                    "policies": ["again"] if retried == "ask" else [],
                },
                {"id": "done", "kind": "terminal", "inputs": [dict(submission)]},
            ],
            "edges": [
                {"id": "a", "from": {"node": "act", "port": "result"},
                 "to": {"node": "ask", "port": "result"}},
                {"id": "b", "from": {"node": "ask", "port": "result"},
                 "to": {"node": "done", "port": "result"}},
            ],
            "entry": ["act"], "terminals": ["done"],
            "result": {"node": "ask", "port": "result"},
            "policies": [{
                "id": "again", "kind": "retry", "config": {"max_attempts": 2},
            }],
        }

    def compile(self, document):
        return compile_source(
            json.dumps(document), self.handlers, self.schemas,
            source_format="json",
        )

    def test_a_human_node_cannot_be_retried(self) -> None:
        with self.assertRaises(DiagnosticError) as caught:
            self.compile(self.document(retried="ask"))
        found = [
            item for item in caught.exception.diagnostics
            if item.code == "DSL_POLICY_INVALID"
        ]
        self.assertTrue(found)
        self.assertIn("cannot carry a retry policy", found[0].message)
        self.assertIn("reminder and escalation", found[0].hint or "")

    def test_human_branch_reads_only_the_standard_decision_field(self) -> None:
        document = self.document(retried="")
        document["edges"][1]["condition"] = {
            "op": "eq",
            "left": {"op": "ref", "path": "source.result.approved"},
            "right": {"op": "literal", "value": True},
        }

        with self.assertRaises(DiagnosticError) as caught:
            self.compile(document)

        self.assertIn("source.result.decision", str(caught.exception))

    def test_an_action_still_can(self) -> None:
        """The refusal is about having no work to re-run, not about retry."""

        compiled = self.compile(self.document(retried="act"))
        self.assertEqual("workflow:chased", compiled.ir.workflow_id)


class UnsatisfiableJoinTests(unittest.TestCase):
    """A join no run can fill is a contradiction, not a runtime surprise.

    A node routes to one outgoing edge unless it declares `route_mode:
    parallel`. An Agent asked for parallel checks wrote the fan-out without
    it, so only one branch ran and the `all` join downstream waited for an
    input that could never arrive — the run died there, after the Agent that
    did run had already done its work. It is visible in the definition, so it
    is refused in the definition.
    """

    def document(self, *, route_mode=None):
        port = {"id": "prompt", "schema_id": "example://request/1.0"}
        result = {"id": "result", "schema_id": "example://request/1.0"}

        def action(node_id):
            return {
                "id": node_id, "kind": "action",
                "inputs": [dict(port)], "outputs": [dict(result)],
                "handler": {"name": "collect", "version": "1.2.0"},
            }

        fan = action("fan")
        if route_mode is not None:
            fan["route_mode"] = route_mode
        return {
            "dsl_version": "1.3",
            "metadata": {"id": "split", "name": "Split"},
            "nodes": [
                fan, action("left"), action("right"),
                {
                    "id": "gather", "kind": "join",
                    "inputs": [
                        {"id": "from_left", "schema_id": "example://request/1.0"},
                        {"id": "from_right", "schema_id": "example://request/1.0"},
                    ],
                    "outputs": [dict(result)],
                    "policies": ["all_of_them"],
                },
                {"id": "done", "kind": "terminal", "inputs": [dict(result)]},
            ],
            "edges": [
                {"id": "to_left", "from": {"node": "fan", "port": "result"},
                 "to": {"node": "left", "port": "prompt"}},
                {"id": "to_right", "from": {"node": "fan", "port": "result"},
                 "to": {"node": "right", "port": "prompt"}},
                {"id": "left_in", "from": {"node": "left", "port": "result"},
                 "to": {"node": "gather", "port": "from_left"}},
                {"id": "right_in", "from": {"node": "right", "port": "result"},
                 "to": {"node": "gather", "port": "from_right"}},
                {"id": "finish", "from": {"node": "gather", "port": "result"},
                 "to": {"node": "done", "port": "result"}},
            ],
            "entry": ["fan"], "terminals": ["done"],
            "result": {"node": "gather", "port": "result"},
            "policies": [{
                "id": "all_of_them", "kind": "join",
                "config": {"mode": "all", "merge_mode": "object_by_edge"},
            }],
        }

    def setUp(self) -> None:
        self.schemas = InMemorySchemaCatalog(
            {"example://request/1.0": {"type": "object"}}
        )
        self.handlers = InMemoryHandlerCatalog([
            HandlerManifest(
                name="collect", version="1.2.0", node_kinds=("action",),
                inputs={"prompt": "example://request/1.0"},
                outputs={"result": "example://request/1.0"},
                config_schema={"type": "object", "additionalProperties": False},
                execution_safety=ExecutionSafety.REPLAY_SAFE,
                resource_profile=ResourceProfile(0, 0, 0, 60, 0, "free"),
                result_schema_id="example://request/1.0",
            )
        ])

    def compile(self, document):
        return compile_source(
            json.dumps(document), self.handlers, self.schemas,
            source_format="json",
        )

    def test_an_exclusive_fan_out_into_an_all_join_is_refused(self) -> None:
        with self.assertRaises(DiagnosticError) as caught:
            self.compile(self.document())
        codes = {item.code for item in caught.exception.diagnostics}
        self.assertIn("DSL_JOIN_INVALID", codes)
        message = " ".join(item.message for item in caught.exception.diagnostics)
        self.assertIn("routes exclusively", message)
        self.assertIn("from_left", message)
        self.assertIn("from_right", message)

    def test_the_same_graph_is_accepted_when_the_fan_out_is_parallel(self) -> None:
        """The refusal is about the contradiction, not about the shape."""

        compiled = self.compile(self.document(route_mode="parallel"))
        self.assertEqual("workflow:split", compiled.ir.workflow_id)


class WorkspaceAccessPolicyTests(unittest.TestCase):
    """`workspace_access` is shape-checked here; whether a deployment can
    actually satisfy it is a fact about the Runtime, checked at compile time
    instead (`test_workflow_langgraph_runtime.py`)."""

    def setUp(self) -> None:
        self.schemas = InMemorySchemaCatalog(
            {"example://request/1.0": {"type": "object"}}
        )
        self.handlers = InMemoryHandlerCatalog([
            HandlerManifest(
                name="agent.opencode", version="1.0.0", node_kinds=("action",),
                inputs={"prompt": "example://request/1.0"},
                outputs={"result": "example://request/1.0"},
                config_schema={"type": "object", "additionalProperties": False},
                execution_safety=ExecutionSafety.UNKNOWN_ON_LEASE_LOSS,
                resource_profile=ResourceProfile(0, 0, 0, 60, 0, "free"),
                result_schema_id="example://request/1.0",
            )
        ])

    def document(self, config: dict) -> dict:
        return {
            "dsl_version": "1.3",
            "metadata": {"id": "review", "name": "Review"},
            "nodes": [
                {
                    "id": "review", "kind": "action",
                    "inputs": [{"id": "prompt", "schema_id": "example://request/1.0"}],
                    "outputs": [{"id": "result", "schema_id": "example://request/1.0"}],
                    "handler": {"name": "agent.opencode", "version": "1.0.0"},
                    "policies": ["access"],
                },
                {
                    "id": "done", "kind": "terminal",
                    "inputs": [{"id": "result", "schema_id": "example://request/1.0"}],
                },
            ],
            "edges": [
                {"id": "e", "from": {"node": "review", "port": "result"},
                 "to": {"node": "done", "port": "result"}},
            ],
            "entry": ["review"], "terminals": ["done"],
            "result": {"node": "review", "port": "result"},
            "policies": [{"id": "access", "kind": "workspace_access", "config": config}],
        }

    def analyze(self, config: dict):
        document = parse_dsl(json.dumps(self.document(config)), source_format="json")
        validate_dsl_structure(document)
        return analyze_dsl(document, self.handlers, self.schemas)

    def test_read_only_with_no_files_is_accepted(self) -> None:
        analysis = self.analyze({"mode": "read_only"})
        self.assertEqual(("done",), analysis.outgoing["review"])

    def test_read_only_with_a_files_allowlist_is_accepted(self) -> None:
        analysis = self.analyze({
            "mode": "read_only", "files": ["README.md", "docs/**/*.md"],
        })
        self.assertEqual(("done",), analysis.outgoing["review"])

    def test_a_missing_mode_is_refused(self) -> None:
        with self.assertRaises(DiagnosticError) as caught:
            self.analyze({})
        found = [
            item for item in caught.exception.diagnostics
            if item.code == "DSL_POLICY_INVALID"
        ]
        self.assertTrue(found)
        self.assertIn("read_only", found[0].message)

    def test_read_write_is_not_yet_supported(self) -> None:
        with self.assertRaises(DiagnosticError) as caught:
            self.analyze({"mode": "read_write"})
        codes = {item.code for item in caught.exception.diagnostics}
        self.assertIn("DSL_POLICY_INVALID", codes)

    def test_an_empty_files_list_is_refused(self) -> None:
        with self.assertRaises(DiagnosticError) as caught:
            self.analyze({"mode": "read_only", "files": []})
        codes = {item.code for item in caught.exception.diagnostics}
        self.assertIn("DSL_POLICY_INVALID", codes)

    def test_a_parent_traversal_in_files_is_refused(self) -> None:
        with self.assertRaises(DiagnosticError) as caught:
            self.analyze({"mode": "read_only", "files": ["../secrets.txt"]})
        codes = {item.code for item in caught.exception.diagnostics}
        self.assertIn("DSL_POLICY_INVALID", codes)

    def test_an_absolute_path_in_files_is_refused(self) -> None:
        with self.assertRaises(DiagnosticError) as caught:
            self.analyze({"mode": "read_only", "files": ["/etc/passwd"]})
        codes = {item.code for item in caught.exception.diagnostics}
        self.assertIn("DSL_POLICY_INVALID", codes)

    def test_a_non_string_entry_in_files_is_refused(self) -> None:
        with self.assertRaises(DiagnosticError) as caught:
            self.analyze({"mode": "read_only", "files": [123]})
        codes = {item.code for item in caught.exception.diagnostics}
        self.assertIn("DSL_POLICY_INVALID", codes)


class ProjectAccessModeTests(unittest.TestCase):
    """`workspace_access` with `isolation: none` — the real project directory.

    Both refusals here are decided by the document alone, so they belong on
    the publish path and must give the same answer on every machine. The
    capability gate that asks whether *this* Runtime granted the access lives
    elsewhere on purpose (`langgraph_runtime.compiler`, which only runs when
    the IR is bound at start), and is not exercised here. See
    docs/project-file-access-design.md §2.1.
    """

    OBJ = "schema://object/1.0"

    def setUp(self) -> None:
        self.schemas = InMemorySchemaCatalog({self.OBJ: {"type": "object"}})
        self.handlers = InMemoryHandlerCatalog([
            HandlerManifest(
                name="agent.opencode", version="1.18.16", node_kinds=("action",),
                inputs={"prompt": self.OBJ}, outputs={"result": self.OBJ},
                config_schema={"type": "object"},
                execution_safety=ExecutionSafety.UNKNOWN_ON_LEASE_LOSS,
                resource_profile=ResourceProfile(0, 0, 0, 60, 0, "agent-cli"),
                result_schema_id=self.OBJ,
            ),
            HandlerManifest(
                name="dev_tool", version="1.0.0", node_kinds=("action",),
                inputs={"workspace_ref": self.OBJ}, outputs={"result": self.OBJ},
                config_schema={"type": "object"},
                execution_safety=ExecutionSafety.REPLAY_SAFE,
                resource_profile=ResourceProfile(0, 0, 0, 60, 0, "dev-tool"),
                result_schema_id=self.OBJ,
            ),
        ])

    def agent(self, node_id, *, parallel=False):
        node = {
            "id": node_id, "kind": "action",
            "inputs": [{"id": "prompt", "schema_id": self.OBJ}],
            "outputs": [{"id": "result", "schema_id": self.OBJ}],
            "handler": {"name": "agent.opencode", "version": "1.18.16"},
        }
        if parallel:
            node["route_mode"] = "parallel"
        return node

    def terminal(self, node_id):
        return {
            "id": node_id, "kind": "terminal",
            "inputs": [{"id": "result", "schema_id": self.OBJ}],
        }

    def edge(self, edge_id, source, target, port="prompt"):
        return {
            "id": edge_id, "from": {"node": source, "port": "result"},
            "to": {"node": target, "port": port},
        }

    def document(self, nodes, edges, *, entry, terminals, result, config, holder):
        for node in nodes:
            if node["id"] == holder:
                node["policies"] = ["project"]
        return {
            "dsl_version": "1.3",
            "metadata": {"id": "project_access", "name": "Project access"},
            "nodes": nodes, "edges": edges, "entry": entry,
            "terminals": terminals, "result": result,
            "policies": [
                {"id": "project", "kind": "workspace_access", "config": config},
            ],
        }

    def compile(self, document):
        return compile_source(
            json.dumps(document), self.handlers, self.schemas,
            source_format="json",
        )

    def fan_out(self, config):
        return self.document(
            [
                self.agent("fan", parallel=True), self.agent("left"),
                self.agent("right"), self.terminal("d1"), self.terminal("d2"),
            ],
            [
                self.edge("e1", "fan", "left"), self.edge("e2", "fan", "right"),
                self.edge("e3", "left", "d1", "result"),
                self.edge("e4", "right", "d2", "result"),
            ],
            entry=["fan"], terminals=["d1", "d2"],
            result={"node": "fan", "port": "result"},
            config=config, holder="fan",
        )

    def test_parallel_agents_are_refused_when_the_project_is_held_directly(self) -> None:
        with self.assertRaises(DiagnosticError) as caught:
            self.compile(self.fan_out(
                {"mode": "read_write", "isolation": "none"},
            ))
        message = " ".join(item.message for item in caught.exception.diagnostics)
        self.assertIn("fans out in parallel to Agent nodes", message)
        self.assertIn("left", message)
        self.assertIn("right", message)

    def test_the_same_fan_out_is_fine_with_a_disposable_copy(self) -> None:
        """Under `worktree` each node gets its own copy, so nothing overlaps."""

        compiled = self.compile(self.fan_out(
            {"mode": "read_only", "isolation": "worktree"},
        ))
        self.assertEqual("workflow:project_access", compiled.ir.workflow_id)

    def test_a_sequential_agent_chain_is_accepted(self) -> None:
        compiled = self.compile(self.document(
            [self.agent("first"), self.agent("second"), self.terminal("done")],
            [
                self.edge("e1", "first", "second"),
                self.edge("e2", "second", "done", "result"),
            ],
            entry=["first"], terminals=["done"],
            result={"node": "second", "port": "result"},
            config={"mode": "read_write", "isolation": "none"}, holder="first",
        ))
        self.assertEqual("workflow:project_access", compiled.ir.workflow_id)

    def test_a_dev_tool_node_is_refused_when_the_project_is_held_directly(self) -> None:
        tool = {
            "id": "tool", "kind": "action",
            "inputs": [{"id": "workspace_ref", "schema_id": self.OBJ}],
            "outputs": [{"id": "result", "schema_id": self.OBJ}],
            "handler": {"name": "dev_tool", "version": "1.0.0"},
        }
        with self.assertRaises(DiagnosticError) as caught:
            self.compile(self.document(
                [self.agent("first"), tool, self.terminal("done")],
                [
                    self.edge("e1", "first", "tool", "workspace_ref"),
                    self.edge("e2", "tool", "done", "result"),
                ],
                entry=["first"], terminals=["done"],
                result={"node": "first", "port": "result"},
                config={"mode": "read_write", "isolation": "none"},
                holder="first",
            ))
        message = " ".join(item.message for item in caught.exception.diagnostics)
        self.assertIn("works in its own worktree", message)

    def test_read_write_requires_isolation_none(self) -> None:
        with self.assertRaises(DiagnosticError) as caught:
            self.compile(self.document(
                [self.agent("first"), self.terminal("done")],
                [self.edge("e1", "first", "done", "result")],
                entry=["first"], terminals=["done"],
                result={"node": "first", "port": "result"},
                config={"mode": "read_write", "isolation": "worktree"},
                holder="first",
            ))
        message = " ".join(item.message for item in caught.exception.diagnostics)
        self.assertIn("requires isolation 'none'", message)

    def test_files_is_refused_alongside_isolation_none(self) -> None:
        with self.assertRaises(DiagnosticError) as caught:
            self.compile(self.document(
                [self.agent("first"), self.terminal("done")],
                [self.edge("e1", "first", "done", "result")],
                entry=["first"], terminals=["done"],
                result={"node": "first", "port": "result"},
                config={
                    "mode": "read_only", "isolation": "none",
                    "files": ["README.md"],
                },
                holder="first",
            ))
        message = " ".join(item.message for item in caught.exception.diagnostics)
        self.assertIn("isolation 'worktree' only", message)

    def test_an_unknown_isolation_is_refused(self) -> None:
        with self.assertRaises(DiagnosticError) as caught:
            self.compile(self.document(
                [self.agent("first"), self.terminal("done")],
                [self.edge("e1", "first", "done", "result")],
                entry=["first"], terminals=["done"],
                result={"node": "first", "port": "result"},
                config={"mode": "read_only", "isolation": "sandbox"},
                holder="first",
            ))
        codes = {item.code for item in caught.exception.diagnostics}
        self.assertIn("DSL_POLICY_INVALID", codes)
