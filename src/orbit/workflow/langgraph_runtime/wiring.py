"""Safe production wiring for the opt-in LangGraph adapter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..handlers import TransformHandler
from ..persistence.workflow_versions import SQLiteWorkflowVersionStore
from .compiler import BoundHandler, LangGraphHandlerRegistry
from .service import LangGraphWorkflowService


def _transform(inputs: Mapping[str, Any], config: Mapping[str, Any], _context):
    operation = config.get("operation", "identity")
    if operation == "identity":
        return dict(inputs)
    if operation == "select_fields":
        fields = config.get("fields")
        if not isinstance(fields, (list, tuple)):
            raise ValueError("transform fields must be an array")
        return {key: inputs[key] for key in fields if key in inputs}
    if operation == "build_object":
        value = config.get("value")
        if not isinstance(value, Mapping):
            raise ValueError("transform value must be an object")
        return dict(value)
    raise ValueError(f"unsupported transform operation: {operation}")


def trusted_handlers(registrations: Sequence[Any]) -> LangGraphHandlerRegistry:
    """Expose only reviewed adapters, never arbitrary Orbit NodeHandlers."""

    handlers: list[BoundHandler] = []
    for registration in registrations:
        if isinstance(registration.implementation, TransformHandler):
            manifest = registration.manifest
            handlers.append(BoundHandler(
                manifest.name,
                manifest.version,
                manifest.fingerprint,
                _transform,
            ))
    return LangGraphHandlerRegistry(handlers)


def build_service(
    workflow_db_path: Path | str,
    registrations: Sequence[Any],
    *,
    state_directory: Path | str,
) -> LangGraphWorkflowService:
    """Build the isolated service and its two adapter-owned databases."""

    state = Path(state_directory)
    return LangGraphWorkflowService(
        SQLiteWorkflowVersionStore(workflow_db_path),
        trusted_handlers(registrations),
        run_db_path=state / "langgraph-runs.sqlite3",
        checkpoint_db_path=state / "langgraph-checkpoints.sqlite3",
    )
