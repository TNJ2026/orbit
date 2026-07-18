"""Compiler for deterministic edge value mappings."""

from __future__ import annotations

from typing import Any

from .diagnostics import Diagnostic, DiagnosticError, JsonPath


MAX_MAPPING_DEPTH = 16
MAX_MAPPING_ITEMS = 256


def _failure(message: str, path: JsonPath) -> DiagnosticError:
    return DiagnosticError([Diagnostic("DSL_MAPPING_INVALID", message, "compile", path)])


def _compile_value(value: Any, path: JsonPath, counter: list[int], depth: int = 0) -> Any:
    counter[0] += 1
    if counter[0] > MAX_MAPPING_ITEMS:
        raise _failure(f"mapping exceeds item limit {MAX_MAPPING_ITEMS}", path)
    if depth > MAX_MAPPING_DEPTH:
        raise _failure(f"mapping exceeds depth limit {MAX_MAPPING_DEPTH}", path)
    if isinstance(value, str) and value.startswith("$"):
        reference = value[1:]
        if not reference or any(not part for part in reference.split(".")):
            raise _failure(f"invalid mapping reference {value!r}", path)
        return {"op": "ref", "path": reference}
    if value is None or isinstance(value, (str, int, float, bool)):
        return {"op": "literal", "value": value}
    if isinstance(value, list):
        return {
            "op": "array",
            "items": [_compile_value(item, path + (index,), counter, depth + 1) for index, item in enumerate(value)],
        }
    if isinstance(value, dict):
        return {
            "op": "object",
            "fields": {
                key: _compile_value(value[key], path + (key,), counter, depth + 1)
                for key in sorted(value)
            },
        }
    raise _failure(f"unsupported mapping value {type(value).__name__}", path)


def compile_mapping(value: Any, source_schema_id: str, path: JsonPath) -> Any:
    if value is None or value == {}:
        return {"op": "identity", "schema_id": source_schema_id}
    if not isinstance(value, dict):
        raise _failure("mapping must be an object", path)
    unknown = set(value) - {"schema_id", "value"}
    if unknown:
        raise _failure(f"unknown mapping fields: {', '.join(sorted(unknown))}", path)
    schema_id = value.get("schema_id")
    if not isinstance(schema_id, str) or not schema_id:
        raise _failure("mapping requires a non-empty schema_id", path + ("schema_id",))
    if "value" not in value:
        raise _failure("mapping requires a value", path + ("value",))
    return {
        "op": "map",
        "schema_id": schema_id,
        "value": _compile_value(value["value"], path + ("value",), [0]),
    }


def mapping_references(value: Any) -> tuple[str, ...]:
    found: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("op") == "ref" and isinstance(node.get("path"), str):
                found.add(node["path"])
            for child in node.values():
                visit(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                visit(child)

    visit(value)
    return tuple(sorted(found))
