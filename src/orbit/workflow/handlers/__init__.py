"""Trusted first-party Handler runtime implementations."""

from .registry import (
    ExecutionRegistry, HandlerContractMismatchError, HandlerNotAvailableError,
    RegisteredHandler,
)
from .executor import HandlerExecutor
from .usage import InMemoryUsageReporter, NoopUsageReporter, PersistentBudgetUsageReporter, UsageConflictError
from .agent import AgentHandler, AgentClientPort, FakeAgentClient, TrustedCliAgentClient
from .fake import FakeHandler
from .tools import RegisteredTool, ToolAdapter, ToolHandler, ToolManifest, ToolRegistry
from .transform import TransformHandler

__all__ = [
    "ExecutionRegistry", "HandlerContractMismatchError",
    "HandlerNotAvailableError", "RegisteredHandler", "HandlerExecutor",
    "InMemoryUsageReporter", "NoopUsageReporter", "PersistentBudgetUsageReporter", "UsageConflictError",
    "AgentClientPort", "AgentHandler", "FakeAgentClient", "FakeHandler",
    "RegisteredTool", "ToolAdapter", "ToolHandler", "ToolManifest", "ToolRegistry",
    "TransformHandler",
    "TrustedCliAgentClient",
]
