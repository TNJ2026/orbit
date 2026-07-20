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


class WorkflowDslGoldenTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
