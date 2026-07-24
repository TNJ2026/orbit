"""Prompt-driven workflow authoring (docs/workflow-prompt-authoring-design.md)."""

from .generator import (
    AuthoringFailedError,
    AuthoringUnavailableError,
    AuthoringUnknownResultError,
    CancelScope,
    GenerationOutcome,
    cancellable,
    TrustedCliDslGenerator,
    UnknownGenerationAgentError,
    WorkflowAuthoringService,
)

__all__ = [
    "AuthoringFailedError", "AuthoringUnavailableError",
    "AuthoringUnknownResultError", "CancelScope", "GenerationOutcome",
    "TrustedCliDslGenerator", "UnknownGenerationAgentError",
    "WorkflowAuthoringService", "cancellable",
]
