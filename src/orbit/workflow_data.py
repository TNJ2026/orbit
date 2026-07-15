"""Structured workflow results, schemas, and edge input mappings."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any


_WORKFLOW_RESULT_RE = re.compile(r"(?im)^WORKFLOW_RESULT\s*:\s*(\{.*\})\s*$")
_SCHEMA_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "null": type(None),
}
_JSONLOGIC_COMPARISONS = {"==", "!=", "===", "!==", ">", ">=", "<", "<="}


def _jsonlogic_var(data: Any, path: str, default: Any = None) -> Any:
    if not path:
        return data
    current = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return default
    return current


def _jsonlogic_truthy(value: Any) -> bool:
    return not (
        value is None or value is False or value == 0 or value == "" or value == []
    )


def validate_jsonlogic(expression: Any, path: str = "condition") -> list[str]:
    """Statically validate the non-extensible orbit-safe-v1 JSONLogic subset."""
    if isinstance(expression, (str, int, float, bool)) or expression is None:
        return []
    if isinstance(expression, list):
        errors: list[str] = []
        for index, item in enumerate(expression):
            errors.extend(validate_jsonlogic(item, f"{path}[{index}]"))
        return errors
    if not isinstance(expression, dict) or len(expression) != 1:
        return [f"{path}: JSONLogic expression must be a single-operator object"]
    operator, raw_args = next(iter(expression.items()))
    allowed = _JSONLOGIC_COMPARISONS | {
        "var", "!", "!!", "and", "or", "length", "in", "missing"
    }
    if operator not in allowed:
        return [f"{path}: unsupported JSONLogic operator {operator!r}"]
    args = raw_args if isinstance(raw_args, list) else [raw_args]
    arity = len(args)
    if operator in _JSONLOGIC_COMPARISONS and arity != 2:
        return [f"{path}: operator {operator!r} expects 2 arguments"]
    if operator in {"!", "!!", "length"} and arity != 1:
        return [f"{path}: operator {operator!r} expects 1 argument"]
    if operator in {"and", "or"} and arity < 1:
        return [f"{path}: operator {operator!r} expects at least 1 argument"]
    if operator == "in" and arity != 2:
        return [f"{path}: operator 'in' expects 2 arguments"]
    if operator == "var":
        if arity not in {1, 2} or not isinstance(args[0], str):
            return [f"{path}: operator 'var' expects a string path and optional default"]
        return validate_jsonlogic(args[1], f"{path}.var[1]") if arity == 2 else []
    if operator == "missing":
        keys = raw_args if isinstance(raw_args, list) else [raw_args]
        if not all(isinstance(key, str) for key in keys):
            return [f"{path}: operator 'missing' expects string paths"]
        return []
    errors = []
    for index, item in enumerate(args):
        errors.extend(validate_jsonlogic(item, f"{path}.{operator}[{index}]"))
    return errors


def evaluate_jsonlogic(expression: Any, data: Any) -> Any:
    """Evaluate an already validated orbit-safe-v1 JSONLogic expression."""
    errors = validate_jsonlogic(expression)
    if errors:
        raise ValueError("; ".join(errors))
    if isinstance(expression, list):
        return [evaluate_jsonlogic(item, data) for item in expression]
    if not isinstance(expression, dict):
        return deepcopy(expression)
    operator, raw_args = next(iter(expression.items()))
    args = raw_args if isinstance(raw_args, list) else [raw_args]
    if operator == "var":
        default = evaluate_jsonlogic(args[1], data) if len(args) == 2 else None
        return deepcopy(_jsonlogic_var(data, args[0], default))
    if operator == "missing":
        keys = raw_args if isinstance(raw_args, list) else [raw_args]
        return [key for key in keys if _jsonlogic_var(data, key, None) is None]
    values = [evaluate_jsonlogic(item, data) for item in args]
    if operator == "!":
        return not _jsonlogic_truthy(values[0])
    if operator == "!!":
        return _jsonlogic_truthy(values[0])
    if operator == "and":
        return all(_jsonlogic_truthy(value) for value in values)
    if operator == "or":
        return any(_jsonlogic_truthy(value) for value in values)
    if operator == "length":
        return len(values[0]) if isinstance(values[0], (str, list, dict)) else 0
    if operator == "in":
        try:
            return values[0] in values[1]
        except TypeError:
            return False
    left, right = values
    if operator in {"==", "==="}:
        return left == right and (operator == "==" or type(left) is type(right))
    if operator in {"!=", "!=="}:
        return left != right or (operator == "!==" and type(left) is not type(right))
    try:
        return {
            ">": left > right,
            ">=": left >= right,
            "<": left < right,
            "<=": left <= right,
        }[operator]
    except TypeError:
        return False


def parse_workflow_result(text: str) -> tuple[dict[str, Any] | None, str]:
    """Parse the last single-line WORKFLOW_RESULT JSON object."""
    matches = _WORKFLOW_RESULT_RE.findall(text or "")
    if not matches:
        return None, ""
    try:
        value = json.loads(matches[-1])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, f"invalid WORKFLOW_RESULT JSON: {exc}"
    if not isinstance(value, dict):
        return None, "WORKFLOW_RESULT must be a JSON object"
    output = value.get("output", {})
    if not isinstance(output, dict):
        return None, "WORKFLOW_RESULT.output must be a JSON object"
    artifacts = value.get("artifacts", [])
    if not isinstance(artifacts, list):
        return None, "WORKFLOW_RESULT.artifacts must be a JSON array"
    normalized = {
        "port": str(value.get("port") or "").strip().lower(),
        "output": deepcopy(output),
        "summary": str(value.get("summary") or "").strip(),
        "artifacts": deepcopy(artifacts),
    }
    return normalized, ""


def validate_json_schema(value: Any, schema: dict[str, Any], path: str = "$.output") -> list[str]:
    """Validate the safe JSON-Schema subset used by workflow node outputs."""
    if not schema:
        return []
    errors: list[str] = []
    expected = schema.get("type")
    if expected:
        expected_types = expected if isinstance(expected, list) else [expected]
        valid = False
        for item in expected_types:
            py_type = _SCHEMA_TYPES.get(str(item))
            if py_type is None:
                errors.append(f"{path}: unsupported schema type {item!r}")
                continue
            if isinstance(value, py_type) and not (
                str(item) in {"number", "integer"} and isinstance(value, bool)
            ):
                valid = True
        if not valid:
            errors.append(f"{path}: expected {expected!r}")
            return errors
    if isinstance(value, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if str(key) not in value:
                    errors.append(f"{path}.{key}: required field is missing")
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for key, child_schema in properties.items():
                if key in value and isinstance(child_schema, dict):
                    errors.extend(validate_json_schema(value[key], child_schema, f"{path}.{key}"))
        if schema.get("additionalProperties") is False and isinstance(properties, dict):
            for key in value:
                if key not in properties:
                    errors.append(f"{path}.{key}: additional property is not allowed")
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            errors.extend(validate_json_schema(item, schema["items"], f"{path}[{index}]"))
    return errors


def resolve_path(value: Any, path: str) -> Any:
    if path == "$":
        return deepcopy(value)
    if not path.startswith("$"):
        raise ValueError(f"mapping source must start with '$': {path!r}")
    remainder = path[1:]
    tokens: list[str | int] = []
    while remainder:
        if remainder.startswith("."):
            match = re.match(r"^\.([^\.\[\]]+)", remainder)
            if not match:
                raise ValueError(f"invalid mapping source path: {path!r}")
            tokens.append(match.group(1))
            remainder = remainder[match.end():]
            continue
        if remainder.startswith("["):
            wildcard = re.match(r"^\[\*\]", remainder)
            if wildcard:
                tokens.append("*")
                remainder = remainder[wildcard.end():]
                continue
            match = re.match(r"^\[(\d+)\]", remainder)
            if not match:
                raise ValueError(f"mapping array index must be a non-negative integer: {path!r}")
            tokens.append(int(match.group(1)))
            remainder = remainder[match.end():]
            continue
        raise ValueError(f"invalid mapping source path: {path!r}")
    def resolve(current: Any, index: int) -> Any:
        if index >= len(tokens):
            return deepcopy(current)
        token = tokens[index]
        if token == "*":
            if not isinstance(current, list):
                raise KeyError(path)
            return [resolve(item, index + 1) for item in current]
        if isinstance(token, int):
            if not isinstance(current, list) or token >= len(current):
                raise KeyError(path)
            return resolve(current[token], index + 1)
        elif not isinstance(current, dict) or token not in current:
            raise KeyError(path)
        return resolve(current[token], index + 1)

    return resolve(value, 0)


def build_mapping_context(
    workflow_result: dict[str, Any],
    task: dict[str, Any],
    node_results: list[dict[str, Any]],
    current_step: str,
    item_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the task/item-local data scope exposed to edge mappings.

    Top-level result fields remain aliases for the just-completed node so old
    ``$.output`` mappings keep working. Node history is intentionally limited
    to the current task, which is the current workflow run in today's runtime.
    """
    nodes: dict[str, dict[str, Any]] = {}
    for stored in node_results:
        step = str(stored.get("step") or "")
        if not step:
            continue
        run = {
            "port": str(stored.get("port") or ""),
            "output": deepcopy(stored.get("output") or {}),
            "summary": str(stored.get("summary") or ""),
            "artifacts": deepcopy(stored.get("artifacts") or []),
        }
        entry = nodes.setdefault(step, {"latest": run, "runs": []})
        entry["runs"].append(run)
        entry["latest"] = run

    current_runs = (nodes.get(current_step) or {}).get("runs") or []
    task_input = {
        "id": task.get("id"),
        "title": str(task.get("title") or ""),
        "content": str(task.get("content") or ""),
    }
    if item_scope is None:
        item: Any = {
            **task_input,
            "parent_task_id": task.get("parent_task_id"),
        }
        item_meta: dict[str, Any] = {}
    else:
        item = deepcopy(item_scope.get("item_value"))
        item_meta = {
            "id": item_scope.get("id"),
            "group_id": item_scope.get("group_id"),
            "index": item_scope.get("item_index"),
            "key": item_scope.get("scope_key"),
            "depends_on": deepcopy(item_scope.get("depends_on") or []),
            "status": str(item_scope.get("status") or ""),
        }
    return {
        **deepcopy(workflow_result),
        "run": {"inputs": {"task": task_input}, "variables": {}},
        "scope": {
            "item": item,
            "item_meta": item_meta,
            "iteration": max(0, len(current_runs) - 1),
        },
        "nodes": nodes,
    }


def _assign_path(target: dict[str, Any], path: str, value: Any) -> None:
    parts = [part for part in str(path).split(".") if part]
    if not parts:
        raise ValueError("mapping target must not be empty")
    current = target
    for part in parts[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            raise ValueError(f"mapping target conflicts at {part!r}")
        current = child
    current[parts[-1]] = value


def apply_input_mapping(
    workflow_result: dict[str, Any], mapping: dict[str, Any] | None
) -> dict[str, Any]:
    """Map normalized result fields into a target node input object."""
    if not mapping:
        return {}
    if not isinstance(mapping, dict):
        raise ValueError("edge mapping must be an object")
    mapped: dict[str, Any] = {}
    for target, rule in mapping.items():
        optional = False
        has_default = False
        default: Any = None
        if isinstance(rule, str):
            source = rule
        elif isinstance(rule, dict):
            source = str(rule.get("from") or "")
            optional = bool(rule.get("optional", False))
            has_default = "default" in rule
            default = deepcopy(rule.get("default"))
        else:
            raise ValueError(f"mapping for {target!r} must be a path string or object")
        try:
            mapped_value = resolve_path(workflow_result, source)
        except KeyError:
            if has_default:
                mapped_value = default
            elif optional:
                continue
            else:
                raise ValueError(f"mapping source is missing: {source}") from None
        _assign_path(mapped, str(target), mapped_value)
    return mapped
