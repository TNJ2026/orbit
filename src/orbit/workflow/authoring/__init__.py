"""Prompt-driven workflow authoring (docs/workflow-prompt-authoring-design.md)."""

from .external import (
    ExternalAuthoringBroker,
    UnknownAuthoringRequestError,
    client_agent_name,
)
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
    "AuthoringFailedError",
    "AuthoringUnavailableError", "AuthoringUnknownResultError", "CancelScope",
    "ExternalAuthoringBroker", "GenerationOutcome", "TrustedCliDslGenerator",
    "UnknownAuthoringRequestError", "UnknownGenerationAgentError",
    "WorkflowAuthoringService", "active_scope", "cancellable",
    "client_agent_name",
]
