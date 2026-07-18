"""Compiler for a deliberately small, deterministic condition language."""

from __future__ import annotations

import ast
from typing import Any

from .diagnostics import Diagnostic, DiagnosticError, JsonPath


MAX_EXPRESSION_NODES = 128
MAX_EXPRESSION_DEPTH = 16
_COMPARE = {
    ast.Eq: "eq",
    ast.NotEq: "ne",
    ast.Lt: "lt",
    ast.LtE: "lte",
    ast.Gt: "gt",
    ast.GtE: "gte",
    ast.In: "in",
    ast.NotIn: "not_in",
}
_CALLS = {"exists", "length"}


def _failure(message: str, path: JsonPath) -> DiagnosticError:
    return DiagnosticError([Diagnostic("DSL_EXPRESSION_INVALID", message, "compile", path)])


def _reference(node: ast.AST) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _compile(node: ast.AST, path: JsonPath, depth: int = 0) -> Any:
    if depth > MAX_EXPRESSION_DEPTH:
        raise _failure(f"condition exceeds depth limit {MAX_EXPRESSION_DEPTH}", path)
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool, type(None))):
        return {"op": "literal", "value": node.value}
    reference = _reference(node)
    if reference is not None:
        return {"op": "ref", "path": reference}
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        return {
            "op": "and" if isinstance(node.op, ast.And) else "or",
            "args": [_compile(item, path, depth + 1) for item in node.values],
        }
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return {"op": "not", "arg": _compile(node.operand, path, depth + 1)}
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        operator = _COMPARE.get(type(node.ops[0]))
        if operator is None:
            raise _failure("comparison operator is not allowed", path)
        return {
            "op": operator,
            "left": _compile(node.left, path, depth + 1),
            "right": _compile(node.comparators[0], path, depth + 1),
        }
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _CALLS:
        if len(node.args) != 1 or node.keywords:
            raise _failure(f"{node.func.id} requires exactly one positional argument", path)
        return {"op": "call", "name": node.func.id, "args": [_compile(node.args[0], path, depth + 1)]}
    if isinstance(node, (ast.List, ast.Tuple)):
        return {"op": "list", "items": [_compile(item, path, depth + 1) for item in node.elts]}
    raise _failure(f"expression syntax {type(node).__name__} is not allowed", path)


def compile_condition(value: Any, path: JsonPath) -> Any:
    if value is None:
        return {"op": "literal", "value": True}
    if isinstance(value, bool):
        return {"op": "literal", "value": value}
    if isinstance(value, dict):
        # Structured AST is accepted only after round-tripping through the same
        # validator, so callers cannot smuggle arbitrary runtime operations.
        return validate_expression_ast(value, path)
    if not isinstance(value, str) or len(value) > 4096:
        raise _failure("condition must be a boolean, string, or expression AST", path)
    try:
        parsed = ast.parse(value, mode="eval")
    except SyntaxError as exc:
        raise _failure(f"invalid condition syntax: {exc.msg}", path) from None
    if sum(1 for _ in ast.walk(parsed)) > MAX_EXPRESSION_NODES:
        raise _failure(f"condition exceeds node limit {MAX_EXPRESSION_NODES}", path)
    return _compile(parsed.body, path)


def validate_expression_ast(value: dict[str, Any], path: JsonPath, depth: int = 0) -> Any:
    if depth > MAX_EXPRESSION_DEPTH:
        raise _failure(f"condition exceeds depth limit {MAX_EXPRESSION_DEPTH}", path)
    if not isinstance(value, dict):
        raise _failure("expression AST nodes must be objects", path)
    op = value.get("op")
    if op == "literal" and set(value) == {"op", "value"}:
        literal = value["value"]
        if literal is None or isinstance(literal, (str, int, float, bool)):
            return {"op": "literal", "value": literal}
    if op == "ref" and set(value) == {"op", "path"} and isinstance(value["path"], str) and value["path"]:
        return {"op": "ref", "path": value["path"]}
    if op in {"and", "or"} and set(value) == {"op", "args"} and isinstance(value["args"], list):
        return {"op": op, "args": [validate_expression_ast(item, path, depth + 1) for item in value["args"]]}
    if op == "not" and set(value) == {"op", "arg"} and isinstance(value["arg"], dict):
        return {"op": "not", "arg": validate_expression_ast(value["arg"], path, depth + 1)}
    if op in set(_COMPARE.values()) and set(value) == {"op", "left", "right"}:
        return {
            "op": op,
            "left": validate_expression_ast(value["left"], path, depth + 1),
            "right": validate_expression_ast(value["right"], path, depth + 1),
        }
    if op == "call" and set(value) == {"op", "name", "args"} and value.get("name") in _CALLS and isinstance(value["args"], list) and len(value["args"]) == 1:
        return {"op": "call", "name": value["name"], "args": [validate_expression_ast(value["args"][0], path, depth + 1)]}
    if op == "list" and set(value) == {"op", "items"} and isinstance(value["items"], list):
        return {"op": "list", "items": [validate_expression_ast(item, path, depth + 1) for item in value["items"]]}
    raise _failure(f"invalid structured expression operation {op!r}", path)


def expression_references(value: Any) -> tuple[str, ...]:
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
