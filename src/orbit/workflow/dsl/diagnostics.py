"""Stable diagnostics shared by every DSL compilation phase."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping


JsonPath = tuple[str | int, ...]


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class SourceRange:
    source: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int

    def __post_init__(self) -> None:
        values = (self.start_line, self.start_column, self.end_line, self.end_column)
        if any(isinstance(value, bool) or value < 1 for value in values):
            raise ValueError("source positions are 1-based positive integers")


def format_json_path(path: JsonPath) -> str:
    result = "$"
    for part in path:
        if isinstance(part, int):
            result += f"[{part}]"
        elif part.isidentifier():
            result += f".{part}"
        else:
            escaped = part.replace("\\", "\\\\").replace("'", "\\'")
            result += f"['{escaped}']"
    return result


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    phase: str
    path: JsonPath = ()
    severity: Severity = Severity.ERROR
    source_range: SourceRange | None = None
    related_paths: tuple[JsonPath, ...] = ()
    hint: str | None = None

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.message.strip() or not self.phase.strip():
            raise ValueError("diagnostic code, message, and phase are required")

    @property
    def json_path(self) -> str:
        return format_json_path(self.path)

    def to_dict(self) -> dict[str, Any]:
        source = None
        if self.source_range is not None:
            source = {
                "source": self.source_range.source,
                "start": {
                    "line": self.source_range.start_line,
                    "column": self.source_range.start_column,
                },
                "end": {
                    "line": self.source_range.end_line,
                    "column": self.source_range.end_column,
                },
            }
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "path": self.json_path,
            "source_range": source,
            "related_paths": [format_json_path(item) for item in self.related_paths],
            "hint": self.hint,
            "phase": self.phase,
        }


DIAGNOSTIC_CODES: Mapping[str, str] = MappingProxyType(
    {
        "DSL_PARSE_ERROR": "Input is not valid YAML or JSON.",
        "DSL_DUPLICATE_KEY": "An object key is repeated.",
        "DSL_UNSAFE_YAML": "YAML uses a forbidden or excessive feature.",
        "DSL_SCHEMA_ERROR": "DSL shape does not match the versioned schema.",
        "DSL_UNSUPPORTED_VERSION": "DSL or extension version is unsupported.",
        "DSL_TOO_MANY_ERRORS": "Validation stopped after the diagnostic limit.",
        "DSL_DUPLICATE_ID": "A workflow identifier is repeated.",
        "DSL_REFERENCE_NOT_FOUND": "A workflow reference cannot be resolved.",
        "DSL_GRAPH_CYCLE": "The core workflow graph contains a cycle.",
        "DSL_GRAPH_UNREACHABLE": "A node cannot be reached from an entry.",
        "DSL_GRAPH_NO_TERMINAL_PATH": "A node has no path to a terminal.",
        "DSL_HANDLER_NOT_FOUND": "A handler version cannot be resolved.",
        "DSL_PORT_INCOMPATIBLE": "Connected port schemas are incompatible.",
        "DSL_EXPRESSION_INVALID": "A condition expression is invalid.",
        "DSL_MAPPING_INVALID": "A data mapping is invalid.",
        "WORKFLOW_PUBLISH_CONFLICT": "Workflow publication version conflicts.",
    }
)


def diagnostic_sort_key(diagnostic: Diagnostic) -> tuple[str, tuple[str, ...], str]:
    return (
        diagnostic.phase,
        tuple(f"{type(item).__name__}:{item}" for item in diagnostic.path),
        diagnostic.code,
    )


def sorted_diagnostics(items: Iterable[Diagnostic]) -> tuple[Diagnostic, ...]:
    return tuple(sorted(items, key=diagnostic_sort_key))


class DiagnosticError(ValueError):
    """Raised when a phase cannot produce a value because diagnostics contain errors."""

    def __init__(self, diagnostics: Iterable[Diagnostic]) -> None:
        self.diagnostics = sorted_diagnostics(diagnostics)
        if not self.diagnostics:
            raise ValueError("DiagnosticError requires at least one diagnostic")
        first = self.diagnostics[0]
        super().__init__(f"{first.code} at {first.json_path}: {first.message}")
