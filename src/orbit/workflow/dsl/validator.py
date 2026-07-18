"""Structural validation of parsed Workflow DSL documents."""

from __future__ import annotations

from typing import Any, Iterable

from jsonschema import Draft202012Validator

from ..domain.serialization import to_primitive
from .diagnostics import Diagnostic, DiagnosticError, JsonPath, sorted_diagnostics
from .parser import ParsedDslDocument
from .schema import WORKFLOW_DSL_SCHEMA


MAX_DIAGNOSTICS = 100
_VALIDATOR = Draft202012Validator(to_primitive(WORKFLOW_DSL_SCHEMA))


def _message(error: Any) -> str:
    if error.validator == "additionalProperties":
        return "unknown field is not allowed"
    if error.validator == "required":
        missing = error.message.split("'")[1] if "'" in error.message else "field"
        return f"required field {missing!r} is missing"
    return error.message


def _error_path(error: Any) -> JsonPath:
    path: JsonPath = tuple(error.absolute_path)
    if error.validator == "required" and "'" in error.message:
        path += (error.message.split("'")[1],)
    return path


def _source_location(document: ParsedDslDocument, path: JsonPath):
    candidate = path
    while candidate:
        location = document.source_map.get(candidate)
        if location is not None:
            return location
        candidate = candidate[:-1]
    return document.source_map.get(())


def _diagnostics(document: ParsedDslDocument) -> Iterable[Diagnostic]:
    errors = sorted(
        _VALIDATOR.iter_errors(to_primitive(document.data)),
        key=lambda item: (tuple(str(part) for part in item.absolute_path), item.validator or ""),
    )
    for error in errors[:MAX_DIAGNOSTICS]:
        path = _error_path(error)
        yield Diagnostic(
            code="DSL_UNSUPPORTED_VERSION" if path == ("dsl_version",) else "DSL_SCHEMA_ERROR",
            message=_message(error),
            phase="schema",
            path=path,
            source_range=_source_location(document, path),
        )
    if len(errors) > MAX_DIAGNOSTICS:
        yield Diagnostic(
            code="DSL_TOO_MANY_ERRORS",
            message=f"validation stopped after {MAX_DIAGNOSTICS} errors",
            phase="schema",
        )


def validate_dsl_structure(document: ParsedDslDocument) -> tuple[Diagnostic, ...]:
    diagnostics = sorted_diagnostics(_diagnostics(document))
    if diagnostics:
        raise DiagnosticError(diagnostics)
    return diagnostics
