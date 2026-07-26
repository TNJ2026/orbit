"""Prompt-driven workflow authoring (docs/workflow-prompt-authoring-design.md)."""

from .generator import (
    AuthoringFailedError,
    AuthoringUnavailableError,
    AuthoringUnknownResultError,
    CancelScope,
    GenerationOutcome,
    active_scope,
    cancellable,
    TrustedCliDslGenerator,
    UnknownGenerationAgentError,
    WorkflowAuthoringService,
)

__all__ = [
    "AuthoringFailedError", "AuthoringUnavailableError",
    "AuthoringUnknownResultError", "CancelScope", "GenerationOutcome",
    "TrustedCliDslGenerator", "UnknownGenerationAgentError",
    "WorkflowAuthoringService", "active_scope", "cancellable",
]
