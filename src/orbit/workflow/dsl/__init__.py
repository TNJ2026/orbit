"""Workflow DSL 1.0 parsing and validation."""

from .diagnostics import Diagnostic, DiagnosticError, Severity, SourceRange
from .compiler import canonical_ir_json, compile_document, compile_source
from .parser import ParsedDslDocument, parse_dsl, parse_dsl_file
from .schema import DSL_SCHEMA_ID, WORKFLOW_DSL_SCHEMA
from .semantic import SemanticAnalysis, analyze_dsl
from .validator import validate_dsl_structure

__all__ = [
    "DSL_SCHEMA_ID",
    "Diagnostic",
    "DiagnosticError",
    "ParsedDslDocument",
    "Severity",
    "SemanticAnalysis",
    "SourceRange",
    "WORKFLOW_DSL_SCHEMA",
    "analyze_dsl",
    "canonical_ir_json",
    "compile_document",
    "compile_source",
    "parse_dsl",
    "parse_dsl_file",
    "validate_dsl_structure",
]
