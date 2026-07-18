from __future__ import annotations

import unittest

from orbit.workflow.data.mapping import MappingEvaluationError, evaluate_mapping
from orbit.workflow.testing import assert_reducer_source_is_pure, side_effect_guard


class WorkflowMappingRuntimeTests(unittest.TestCase):
    def test_all_compiler_operations_are_evaluated_deterministically(self):
        expression = {
            "op": "map", "schema_id": "schema://target/1.0",
            "value": {
                "op": "object",
                "fields": {
                    "constant": {"op": "literal", "value": True},
                    "items": {
                        "op": "array",
                        "items": [
                            {"op": "ref", "path": "source.request.values.1"},
                            {"op": "ref", "path": "workflow.inputs.prefix"},
                        ],
                    },
                },
            },
        }
        first = evaluate_mapping(
            expression, {"request": {"values": [1, 2]}},
            workflow_inputs={"prefix": "p"},
        )
        second = evaluate_mapping(
            expression, {"request": {"values": [1, 2]}},
            workflow_inputs={"prefix": "p"},
        )
        self.assertEqual(first, second)
        self.assertEqual({"constant": True, "items": (2, "p")}, first)
        with self.assertRaises(TypeError):
            first["items"] = ()

    def test_identity_and_schema_boundaries(self):
        calls = []

        def validate(schema_id, value):
            calls.append((schema_id, value))
            if schema_id == "bad":
                raise ValueError("invalid")

        result = evaluate_mapping(
            {"op": "identity"}, {"value": 1}, schema_validator=validate,
            source_schema_id="source", target_schema_id="target",
        )
        self.assertEqual({"value": 1}, result)
        self.assertEqual(["source", "target"], [item[0] for item in calls])
        with self.assertRaisesRegex(MappingEvaluationError, "target schema"):
            evaluate_mapping(
                {"op": "identity"}, {"value": 1}, schema_validator=validate,
                target_schema_id="bad",
            )

    def test_invalid_paths_and_resource_limits_return_stable_diagnostics(self):
        with self.assertRaises(MappingEvaluationError) as caught:
            evaluate_mapping(
                {"op": "ref", "path": "source.items.3"},
                {"items": [1]},
            )
        self.assertEqual("$.path", caught.exception.json_path)
        self.assertEqual("mapping_failed", caught.exception.code)

        nested = {"op": "literal", "value": 1}
        for _ in range(18):
            nested = {"op": "array", "items": [nested]}
        with self.assertRaisesRegex(MappingEvaluationError, "depth limit"):
            evaluate_mapping(nested, {})

    def test_evaluator_source_and_runtime_are_side_effect_free(self):
        assert_reducer_source_is_pure(evaluate_mapping)
        with side_effect_guard():
            self.assertEqual(
                {"value": 1},
                evaluate_mapping({"op": "identity"}, {"value": 1}),
            )


if __name__ == "__main__":
    unittest.main()
