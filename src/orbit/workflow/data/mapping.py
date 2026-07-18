"""Pure evaluator for the structured Mapping AST emitted by the DSL compiler."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Callable

from ..domain.serialization import canonical_json, freeze_json


MAX_MAPPING_DEPTH = 16
MAX_MAPPING_ITEMS = 256
MAX_MAPPING_OUTPUT_BYTES = 1_048_576


class MappingEvaluationError(ValueError):
    code = "mapping_failed"

    def __init__(self, path: tuple[str | int, ...], message: str) -> None:
        self.path = path
        self.json_path = "$" + "".join(
            f"[{item}]" if isinstance(item, int) else f".{item}" for item in path
        )
        super().__init__(f"{self.json_path}: {message}")


def _fail(path: tuple[str | int, ...], message: str) -> None:
    raise MappingEvaluationError(path, message)


def _resolve_reference(reference, source, workflow_inputs, path):
    parts = reference.split(".")
    if not parts or any(not item for item in parts):
        _fail(path, "reference path is invalid")
    if parts[0] == "source":
        value, parts = source, parts[1:]
    elif parts[:2] == ["workflow", "inputs"]:
        value, parts = workflow_inputs, parts[2:]
    else:
        _fail(path, "reference is outside source/workflow.inputs scope")
    for part in parts:
        if isinstance(value, Mapping):
            if part not in value:
                _fail(path, f"reference member {part!r} does not exist")
            value = value[part]
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            try:
                index = int(part)
            except ValueError:
                _fail(path, f"array index {part!r} is not an integer")
            if index < 0 or index >= len(value):
                _fail(path, f"array index {index} is out of range")
            value = value[index]
        else:
            _fail(path, "reference traverses a scalar value")
    return value


def _evaluate(node, source, workflow_inputs, path, counter, depth):
    counter[0] += 1
    if counter[0] > MAX_MAPPING_ITEMS:
        _fail(path, f"mapping exceeds item limit {MAX_MAPPING_ITEMS}")
    if depth > MAX_MAPPING_DEPTH:
        _fail(path, f"mapping exceeds depth limit {MAX_MAPPING_DEPTH}")
    if not isinstance(node, Mapping):
        _fail(path, "mapping AST node must be an object")
    op = node.get("op")
    if op == "identity":
        return source
    if op == "literal":
        return node.get("value")
    if op == "ref":
        reference = node.get("path")
        if not isinstance(reference, str):
            _fail(path + ("path",), "reference path must be a string")
        return _resolve_reference(reference, source, workflow_inputs, path + ("path",))
    if op == "object":
        fields = node.get("fields")
        if not isinstance(fields, Mapping):
            _fail(path + ("fields",), "object fields must be an object")
        return {
            key: _evaluate(fields[key], source, workflow_inputs, path + ("fields", key), counter, depth + 1)
            for key in sorted(fields)
        }
    if op == "array":
        items = node.get("items")
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
            _fail(path + ("items",), "array items must be an array")
        return [
            _evaluate(item, source, workflow_inputs, path + ("items", index), counter, depth + 1)
            for index, item in enumerate(items)
        ]
    if op == "map":
        if "value" not in node:
            _fail(path + ("value",), "map operation requires value")
        return _evaluate(node["value"], source, workflow_inputs, path + ("value",), counter, depth + 1)
    _fail(path + ("op",), f"unsupported mapping operation {op!r}")


def evaluate_mapping(
    expression: Mapping[str, Any], source: Any, *,
    workflow_inputs: Mapping[str, Any] | None = None,
    schema_validator: Callable[[str, Any], None] | None = None,
    source_schema_id: str | None = None,
    target_schema_id: str | None = None,
) -> Any:
    """Evaluate a compiled Mapping without clock, IO, imports, or mutable state."""

    workflow_inputs = workflow_inputs or {}
    if schema_validator is not None and source_schema_id is not None:
        try:
            schema_validator(source_schema_id, source)
        except Exception as error:
            raise MappingEvaluationError(("source",), f"source schema validation failed: {error}") from error
    result = freeze_json(_evaluate(expression, source, workflow_inputs, (), [0], 0))
    if len(canonical_json(result).encode("utf-8")) > MAX_MAPPING_OUTPUT_BYTES:
        _fail((), "mapping output exceeds the 1 MiB limit")
    if schema_validator is not None and target_schema_id is not None:
        try:
            schema_validator(target_schema_id, result)
        except Exception as error:
            raise MappingEvaluationError((), f"target schema validation failed: {error}") from error
    return result
