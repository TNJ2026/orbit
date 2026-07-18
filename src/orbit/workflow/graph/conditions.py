"""Pure evaluator for the structured Condition AST."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


MAX_CONDITION_NODES = 128
MAX_CONDITION_DEPTH = 16


class ConditionEvaluationError(ValueError):
    code = "graph_contract_invalid"


def _resolve(path: str, source: Any, workflow_inputs: Mapping[str, Any]) -> Any:
    parts = path.split(".")
    if parts[0] == "source":
        value, parts = source, parts[1:]
    elif parts[:2] == ["workflow", "inputs"]:
        value, parts = workflow_inputs, parts[2:]
    else:
        raise ConditionEvaluationError(f"reference {path!r} is outside condition scope")
    for part in parts:
        if isinstance(value, Mapping):
            if part not in value:
                raise ConditionEvaluationError(f"reference member {part!r} does not exist")
            value = value[part]
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            try:
                index = int(part)
            except ValueError:
                raise ConditionEvaluationError(f"array index {part!r} is invalid") from None
            if index < 0 or index >= len(value):
                raise ConditionEvaluationError(f"array index {index} is out of range")
            value = value[index]
        else:
            raise ConditionEvaluationError("reference traverses a scalar")
    return value


def _evaluate(node, source, workflow_inputs, counter, depth):
    counter[0] += 1
    if counter[0] > MAX_CONDITION_NODES or depth > MAX_CONDITION_DEPTH:
        raise ConditionEvaluationError("condition resource limit exceeded")
    if not isinstance(node, Mapping):
        raise ConditionEvaluationError("condition node must be an object")
    op = node.get("op")
    if op == "literal":
        return node.get("value")
    if op == "ref":
        return _resolve(node.get("path", ""), source, workflow_inputs)
    if op == "list":
        return [_evaluate(item, source, workflow_inputs, counter, depth + 1) for item in node.get("items", ())]
    if op in {"and", "or"}:
        values = [_evaluate(item, source, workflow_inputs, counter, depth + 1) for item in node.get("args", ())]
        if any(not isinstance(item, bool) for item in values):
            raise ConditionEvaluationError(f"{op} operands must be boolean")
        return all(values) if op == "and" else any(values)
    if op == "not":
        value = _evaluate(node.get("arg"), source, workflow_inputs, counter, depth + 1)
        if not isinstance(value, bool):
            raise ConditionEvaluationError("not operand must be boolean")
        return not value
    if op == "call":
        args = node.get("args", ())
        if len(args) != 1:
            raise ConditionEvaluationError("condition call requires one argument")
        if node.get("name") == "exists":
            try:
                _evaluate(args[0], source, workflow_inputs, counter, depth + 1)
                return True
            except ConditionEvaluationError:
                return False
        value = _evaluate(args[0], source, workflow_inputs, counter, depth + 1)
        if node.get("name") == "length" and isinstance(value, (Mapping, Sequence)):
            return len(value)
        raise ConditionEvaluationError("unsupported condition call")
    if op in {"eq", "ne", "lt", "lte", "gt", "gte", "in", "not_in"}:
        left = _evaluate(node.get("left"), source, workflow_inputs, counter, depth + 1)
        right = _evaluate(node.get("right"), source, workflow_inputs, counter, depth + 1)
        try:
            return {
                "eq": lambda: left == right, "ne": lambda: left != right,
                "lt": lambda: left < right, "lte": lambda: left <= right,
                "gt": lambda: left > right, "gte": lambda: left >= right,
                "in": lambda: left in right, "not_in": lambda: left not in right,
            }[op]()
        except (TypeError, KeyError) as error:
            raise ConditionEvaluationError(f"invalid {op} operands") from error
    raise ConditionEvaluationError(f"unsupported condition operation {op!r}")


def evaluate_condition(expression, source, *, workflow_inputs=None) -> bool:
    result = _evaluate(expression, source, workflow_inputs or {}, [0], 0)
    if not isinstance(result, bool):
        raise ConditionEvaluationError("condition result must be boolean")
    return result
