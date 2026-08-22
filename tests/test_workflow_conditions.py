"""The condition evaluator, exercised directly rather than through a run.

Every routed edge in every workflow goes through this, and it had no tests
of its own: what covered it was workflows that happened to take a branch,
which only ever walks the paths where a condition is true and well formed.
The refusals are the half that decides what a malformed workflow does, and
they were the half nobody had looked at.
"""

from __future__ import annotations

import unittest

from orbit.workflow.graph.conditions import (
    ConditionEvaluationError, MAX_CONDITION_DEPTH, MAX_CONDITION_NODES,
    evaluate_condition,
)

SOURCE = {"result": {"text": "hello", "count": 3, "items": [10, 20], "flag": True}}


def ref(path):
    return {"op": "ref", "path": path}


def lit(value):
    return {"op": "literal", "value": value}


class ReferenceScopeTests(unittest.TestCase):
    def test_a_reference_may_only_leave_from_source_or_workflow_inputs(self) -> None:
        # `source` on its own is in scope — it names the whole object — so the
        # cases here are the ones that name something the condition cannot see.
        for path in ("elsewhere.text", "results.text", "workflow.text", "inputs.mode"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(
                    ConditionEvaluationError, "outside condition scope",
                ):
                    evaluate_condition(ref(path), SOURCE)

    def test_workflow_inputs_are_reachable_and_scoped(self) -> None:
        condition = {"op": "eq", "left": ref("workflow.inputs.mode"), "right": lit("fast")}
        self.assertTrue(evaluate_condition(
            condition, SOURCE, workflow_inputs={"mode": "fast"},
        ))
        with self.assertRaisesRegex(ConditionEvaluationError, "does not exist"):
            evaluate_condition(condition, SOURCE, workflow_inputs={})

    def test_a_missing_member_is_refused_rather_than_read_as_absent(self) -> None:
        with self.assertRaisesRegex(ConditionEvaluationError, "does not exist"):
            evaluate_condition(ref("source.result.missing"), SOURCE)

    def test_walking_into_a_scalar_is_refused(self) -> None:
        with self.assertRaisesRegex(ConditionEvaluationError, "traverses a scalar"):
            evaluate_condition(ref("source.result.count.deeper"), SOURCE)

    def test_an_array_takes_an_index_and_nothing_else(self) -> None:
        self.assertTrue(evaluate_condition(
            {"op": "eq", "left": ref("source.result.items.1"), "right": lit(20)}, SOURCE,
        ))
        with self.assertRaisesRegex(ConditionEvaluationError, "array index 'first' is invalid"):
            evaluate_condition(ref("source.result.items.first"), SOURCE)
        for index in ("2", "-1"):
            with self.subTest(index=index):
                with self.assertRaisesRegex(ConditionEvaluationError, "out of range"):
                    evaluate_condition(ref(f"source.result.items.{index}"), SOURCE)

    def test_a_string_is_a_scalar_and_not_an_array(self) -> None:
        """`str` is a Sequence; indexing one would make `text.0` mean a letter."""

        with self.assertRaisesRegex(ConditionEvaluationError, "traverses a scalar"):
            evaluate_condition(ref("source.result.text.0"), SOURCE)


class OperandTypeTests(unittest.TestCase):
    def test_boolean_operators_refuse_non_boolean_operands(self) -> None:
        for op in ("and", "or"):
            with self.subTest(op=op):
                with self.assertRaisesRegex(
                    ConditionEvaluationError, f"{op} operands must be boolean",
                ):
                    evaluate_condition({"op": op, "args": [lit("yes")]}, SOURCE)
        with self.assertRaisesRegex(
            ConditionEvaluationError, "not operand must be boolean",
        ):
            evaluate_condition({"op": "not", "arg": lit(1)}, SOURCE)

    def test_and_or_short_circuit_so_exists_can_guard_what_follows(self) -> None:
        """`and(exists(x), x == 1)` is the only guard the vocabulary offers.

        Evaluating both operands first raised on the comparison exactly when
        the guard had already answered False.
        """

        guarded = {"op": "and", "args": [
            {"op": "call", "name": "exists", "args": [ref("source.result.missing")]},
            {"op": "eq", "left": ref("source.result.missing"), "right": lit(1)},
        ]}
        self.assertFalse(evaluate_condition(guarded, SOURCE))

        either = {"op": "or", "args": [
            {"op": "eq", "left": ref("source.result.count"), "right": lit(3)},
            {"op": "eq", "left": ref("source.result.missing"), "right": lit(1)},
        ]}
        self.assertTrue(evaluate_condition(either, SOURCE))

    def test_comparisons_refuse_operands_they_cannot_order(self) -> None:
        with self.assertRaisesRegex(ConditionEvaluationError, "invalid lt operands"):
            evaluate_condition(
                {"op": "lt", "left": ref("source.result.text"), "right": lit(1)}, SOURCE,
            )
        with self.assertRaisesRegex(ConditionEvaluationError, "invalid in operands"):
            evaluate_condition(
                {"op": "in", "left": lit("x"), "right": ref("source.result.count")},
                SOURCE,
            )

    def test_the_result_of_a_condition_must_itself_be_boolean(self) -> None:
        with self.assertRaises(ConditionEvaluationError):
            evaluate_condition(ref("source.result.count"), SOURCE)


class VocabularyTests(unittest.TestCase):
    def test_an_unknown_operation_is_refused_not_ignored(self) -> None:
        with self.assertRaisesRegex(ConditionEvaluationError, "unsupported condition operation"):
            evaluate_condition({"op": "matches", "left": lit(1), "right": lit(1)}, SOURCE)

    def test_a_condition_node_must_be_an_object(self) -> None:
        for value in ("true", 1, ["and"], None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ConditionEvaluationError, "condition node must be an object",
                ):
                    evaluate_condition(value, SOURCE)

    def test_call_takes_exactly_one_argument(self) -> None:
        with self.assertRaisesRegex(ConditionEvaluationError, "requires one argument"):
            evaluate_condition(
                {"op": "call", "name": "exists", "args": [lit(1), lit(2)]}, SOURCE,
            )

    def test_length_measures_collections_and_refuses_scalars(self) -> None:
        self.assertTrue(evaluate_condition({
            "op": "eq",
            "left": {"op": "call", "name": "length", "args": [ref("source.result.items")]},
            "right": lit(2),
        }, SOURCE))
        with self.assertRaisesRegex(ConditionEvaluationError, "unsupported condition call"):
            evaluate_condition(
                {"op": "call", "name": "length", "args": [ref("source.result.count")]},
                SOURCE,
            )

    def test_an_unknown_call_is_refused(self) -> None:
        with self.assertRaisesRegex(ConditionEvaluationError, "unsupported condition call"):
            evaluate_condition(
                {"op": "call", "name": "upper", "args": [ref("source.result.text")]},
                SOURCE,
            )

    def test_a_list_builds_the_collection_membership_reads(self) -> None:
        self.assertTrue(evaluate_condition({
            "op": "in",
            "left": ref("source.result.count"),
            "right": {"op": "list", "items": [lit(1), lit(3), lit(5)]},
        }, SOURCE))


class ResourceLimitTests(unittest.TestCase):
    def test_a_condition_may_not_outgrow_its_budget(self) -> None:
        wide = {"op": "and", "args": [lit(True)] * (MAX_CONDITION_NODES + 1)}
        with self.assertRaisesRegex(ConditionEvaluationError, "resource limit exceeded"):
            evaluate_condition(wide, SOURCE)

        deep = lit(True)
        for _ in range(MAX_CONDITION_DEPTH + 2):
            deep = {"op": "not", "arg": deep}
        with self.assertRaisesRegex(ConditionEvaluationError, "resource limit exceeded"):
            evaluate_condition(deep, SOURCE)


if __name__ == "__main__":
    unittest.main()
