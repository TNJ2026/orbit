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
from orbit.workflow.dsl import AuthoredWorkflow, compile_source
from orbit.workflow.dsl.patch import GraphPatch, PatchError, apply_patch


BASE = {
    "dsl_version": "1.3",
    "metadata": {"id": "flow", "name": "Flow", "description": "does a thing"},
    "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
    "nodes": [
        {
            "id": "work", "kind": "action", "label": "Transform",
            "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
            "outputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
            "handler": {"name": "transform", "version": "1.0.0"},
            "config": {"mode": "fast"},
            "policies": ["retry"],
        },
        {
            "id": "done", "kind": "terminal", "label": "Done",
            "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
        },
    ],
    "edges": [{
        "id": "flow_edge", "policy": "retry",
        "from": {"node": "work", "port": "value"},
        "to": {"node": "done", "port": "value"},
    }],
    "entry": ["work"],
    "terminals": ["done"],
    "result": {"node": "work", "port": "value"},
    "policies": [{"id": "retry", "kind": "retry", "config": {"max_attempts": 2}}],
}


def patch(*operations, base_version: int = 1) -> GraphPatch:
    return GraphPatch.model_validate(
        {"base_version": base_version, "operations": list(operations)}
    )


def clone() -> dict:
    return json.loads(json.dumps(BASE))


class PatchShapeTests(unittest.TestCase):
    def test_a_patch_carries_the_version_it_was_computed_against(self) -> None:
        """Applied to a different version it produces what nobody asked for."""

        with self.assertRaises(ValidationError):
            GraphPatch.model_validate({"operations": [{"op": "remove_edge", "edge_id": "x"}]})

    def test_a_patch_must_do_something(self) -> None:
        with self.assertRaises(ValidationError):
            GraphPatch.model_validate({"base_version": 1, "operations": []})

    def test_an_unknown_operation_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            patch({"op": "rewrite_everything"})

    def test_an_operation_cannot_carry_fields_it_does_not_declare(self) -> None:
        with self.assertRaises(ValidationError):
            patch({"op": "remove_edge", "edge_id": "flow_edge", "also": "this"})

    def test_an_added_node_is_held_to_the_authoring_contract(self) -> None:
        # A model cannot smuggle in a shape the contract does not admit.
        with self.assertRaises(ValidationError):
            patch({"op": "add_node", "node": {"id": "x", "kind": "agentic"}})
        with self.assertRaises(ValidationError):
            patch({
                "op": "add_node",
                "node": {
                    "id": "x", "kind": "action",
                    "handler": {"name": "t", "version": "1", "fingerprint": "deadbeef"},
                },
            })


class ApplyTests(unittest.TestCase):
    def test_the_document_given_is_never_mutated(self) -> None:
        original = clone()
        apply_patch(original, patch({"op": "remove_edge", "edge_id": "flow_edge"}))
        self.assertEqual(BASE, original)

    def test_a_node_is_added_and_a_duplicate_is_refused(self) -> None:
        added = apply_patch(clone(), patch({
            "op": "add_node",
            "node": {"id": "extra", "kind": "terminal", "label": "Extra"},
        }))
        self.assertEqual(
            ["work", "done", "extra"], [node["id"] for node in added["nodes"]]
        )
        with self.assertRaises(PatchError):
            apply_patch(clone(), patch({
                "op": "add_node", "node": {"id": "work", "kind": "action"},
            }))

    def test_removing_a_node_removes_everything_that_named_it(self) -> None:
        result = apply_patch(clone(), patch({"op": "remove_node", "node_id": "work"}))
        self.assertEqual(["done"], [node["id"] for node in result["nodes"]])
        self.assertEqual([], result["edges"])
        self.assertEqual([], result["entry"])
        self.assertNotIn("result", result)
        self.assertEqual(["done"], result["terminals"])

    def test_a_config_is_replaced_and_emptying_it_removes_the_key(self) -> None:
        set_config = apply_patch(clone(), patch({
            "op": "set_node_config", "node_id": "work", "config": {"mode": "thorough"},
        }))
        self.assertEqual({"mode": "thorough"}, set_config["nodes"][0]["config"])
        cleared = apply_patch(clone(), patch({
            "op": "set_node_config", "node_id": "work", "config": {},
        }))
        self.assertNotIn("config", cleared["nodes"][0])

    def test_an_operation_naming_an_absent_node_says_which_operation(self) -> None:
        with self.assertRaises(PatchError) as caught:
            apply_patch(clone(), patch(
                {"op": "set_node_label", "node_id": "work", "label": "Renamed"},
                {"op": "set_node_config", "node_id": "ghost", "config": {}},
            ))
        # A model that is told which one can fix that one.
        self.assertEqual(1, caught.exception.index)
        self.assertIn("ghost", caught.exception.reason)

    def test_removing_a_policy_removes_every_reference_to_it(self) -> None:
        result = apply_patch(clone(), patch({"op": "remove_policy", "policy_id": "retry"}))
        self.assertNotIn("policies", result)
        self.assertNotIn("policies", result["nodes"][0])
        self.assertNotIn("policy", result["edges"][0])

    def test_entry_and_terminals_must_name_nodes_that_exist(self) -> None:
        with self.assertRaises(PatchError) as caught:
            apply_patch(clone(), patch({"op": "set_entry", "entry": ["ghost"]}))
        self.assertIn("ghost", caught.exception.reason)
        ok = apply_patch(clone(), patch({"op": "set_terminals", "terminals": ["work", "done"]}))
        self.assertEqual(["work", "done"], ok["terminals"])

    def test_metadata_is_patched_but_never_its_id(self) -> None:
        result = apply_patch(clone(), patch({
            "op": "set_metadata", "name": "Renamed", "description": "  ",
        }))
        self.assertEqual("Renamed", result["metadata"]["name"])
        self.assertNotIn("description", result["metadata"])
        # The id is not an operand of any operation, so no instruction can
        # publish an edit onto a different aggregate.
        self.assertEqual("flow", result["metadata"]["id"])

    def test_an_edge_is_replaced_whole_and_keeps_its_place(self) -> None:
        result = apply_patch(clone(), patch({
            "op": "set_edge",
            "edge": {
                "id": "flow_edge",
                "from": {"node": "work", "port": "value"},
                "to": {"node": "done", "port": "value"},
                "condition": "source.value > 5",
                "priority": 2,
            },
        }))
        edge = result["edges"][0]
        self.assertEqual("source.value > 5", edge["condition"])
        self.assertEqual(2, edge["priority"])
        # Replacing it drops the policy it no longer names, rather than
        # keeping a field the new edge did not ask for.
        self.assertNotIn("policy", edge)

    def test_operations_are_applied_in_order(self) -> None:
        result = apply_patch(clone(), patch(
            {"op": "add_node", "node": {"id": "extra", "kind": "terminal"}},
            {"op": "set_terminals", "terminals": ["done", "extra"]},
        ))
        self.assertEqual(["done", "extra"], result["terminals"])
        # And the reverse order cannot work, which is what "in order" means.
        with self.assertRaises(PatchError):
            apply_patch(clone(), patch(
                {"op": "set_terminals", "terminals": ["done", "extra"]},
                {"op": "add_node", "node": {"id": "extra", "kind": "terminal"}},
            ))


class StillCompilesTests(unittest.TestCase):
    """A patch decides how a document got here; the compiler still judges it."""

    def setUp(self) -> None:
        self.schemas = InMemorySchemaCatalog(
            {"example://integer/1.0": {"type": "integer"}}
        )
        self.handlers = InMemoryHandlerCatalog([
            HandlerManifest(
                name="transform", version="1.0.0", node_kinds=("action",),
                inputs={"value": "example://integer/1.0"},
                outputs={"value": "example://integer/1.0"},
                config_schema={"type": "object"},
                execution_safety=ExecutionSafety.REPLAY_SAFE,
                resource_profile=ResourceProfile(0, 0, 0, 60, 0, "free"),
                result_schema_id="example://integer/1.0",
            )
        ])

    def compile(self, document: dict):
        return compile_source(
            json.dumps(document), self.handlers, self.schemas, source_format="json"
        )

    def test_a_patched_document_is_still_a_valid_workflow(self) -> None:
        result = apply_patch(clone(), patch({
            "op": "set_node_label", "node_id": "work", "label": "Renamed",
        }))
        AuthoredWorkflow.model_validate(result)
        compiled = self.compile(result)
        self.assertEqual("workflow:flow", compiled.ir.workflow_id)

    def test_a_patch_that_leaves_a_broken_document_is_caught_by_the_compiler(self) -> None:
        """The patch applies; the workflow is refused. That division is the point."""

        from orbit.workflow.dsl import DiagnosticError

        result = apply_patch(clone(), patch({"op": "remove_node", "node_id": "done"}))
        # Applying succeeded — nothing dangling was left behind …
        self.assertEqual(["work"], [node["id"] for node in result["nodes"]])
        self.assertEqual([], result["terminals"])
        # … but a workflow with no terminal is not a workflow.
        with self.assertRaises((DiagnosticError, ValidationError)):
            AuthoredWorkflow.model_validate(result)
        with self.assertRaises(DiagnosticError):
            self.compile(result)

    def test_only_what_the_patch_named_is_different(self) -> None:
        """The whole reason for operations rather than a rewritten document."""

        before = clone()
        after = apply_patch(before, patch({
            "op": "set_node_config", "node_id": "work", "config": {"mode": "thorough"},
        }))
        changed = [key for key in after if after[key] != before[key]]
        self.assertEqual(["nodes"], changed)
        self.assertEqual(before["nodes"][1], after["nodes"][1])
        node_changed = [
            key for key in after["nodes"][0]
            if after["nodes"][0][key] != before["nodes"][0][key]
        ]
        self.assertEqual(["config"], node_changed)


if __name__ == "__main__":
    unittest.main()
