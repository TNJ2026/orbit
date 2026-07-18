"""Use cases shared by CLI and future HTTP/UI adapters."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from ..catalogs import (
    ExtensionManifest,
    HandlerManifest,
    InMemoryExtensionRegistry,
    InMemoryHandlerCatalog,
    InMemorySchemaCatalog,
)
from ..domain.definitions import CompiledWorkflow
from ..domain.durable_execution import ExecutionSafety
from ..domain.handlers import ResourceProfile
from ..dsl import compile_source
from ..persistence import SQLiteWorkflowVersionStore, WorkflowVersionRecord


@dataclass(frozen=True)
class WorkflowCatalogs:
    handlers: InMemoryHandlerCatalog
    schemas: InMemorySchemaCatalog
    extensions: InMemoryExtensionRegistry


def load_catalogs(path: Path | str) -> WorkflowCatalogs:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) - {"handlers", "schemas", "extensions"}:
        raise ValueError("catalog must contain only handlers, schemas, and extensions")
    handlers = []
    for item in value.get("handlers", []):
        handlers.append(
            HandlerManifest(
                item["name"], item["version"], tuple(item["node_kinds"]),
                item.get("inputs", {}), item.get("outputs", {}),
                item.get("config_schema", {"type": "object"}),
                ExecutionSafety(item["execution_safety"]),
                ResourceProfile(**item["resource_profile"]),
                item["result_schema_id"],
                tuple(item.get("capabilities", [])),
                tuple(item.get("required_secrets", [])),
                bool(item.get("supports_cancel", False)),
                bool(item.get("supports_recover", False)),
            )
        )
    extensions = []
    for item in value.get("extensions", []):
        extensions.append(
            ExtensionManifest(
                item["extension_id"], item["extension_version"], item["config_schema"],
                item.get("draft", True), item.get("executable", False),
            )
        )
    schemas: dict[str, dict[str, Any]] = value.get("schemas", {})
    return WorkflowCatalogs(
        InMemoryHandlerCatalog(handlers),
        InMemorySchemaCatalog(schemas),
        InMemoryExtensionRegistry(extensions),
    )


class WorkflowDefinitionService:
    def __init__(
        self,
        catalogs: WorkflowCatalogs,
        store: SQLiteWorkflowVersionStore | None = None,
    ) -> None:
        self.catalogs = catalogs
        self.store = store

    def compile_workflow(
        self,
        source: str,
        *,
        source_name: str,
        source_format: str | None = None,
    ) -> CompiledWorkflow:
        return compile_source(
            source,
            self.catalogs.handlers,
            self.catalogs.schemas,
            source_name=source_name,
            source_format=source_format,
            extensions=self.catalogs.extensions,
        )

    def validate_workflow(
        self,
        source: str,
        *,
        source_name: str,
        source_format: str | None = None,
    ) -> CompiledWorkflow:
        return self.compile_workflow(
            source, source_name=source_name, source_format=source_format
        )

    def publish_workflow(
        self,
        source: str,
        *,
        source_name: str,
        source_format: str,
        expected_latest_version: int,
        actor: str,
    ) -> WorkflowVersionRecord:
        if self.store is None:
            raise RuntimeError("publish requires a WorkflowVersion store")
        compiled = self.compile_workflow(
            source, source_name=source_name, source_format=source_format
        )
        return self.store.publish(
            compiled,
            expected_latest_version=expected_latest_version,
            source_format=source_format,
            source_text=source,
            actor=actor,
        )

    def get_workflow_version(self, workflow_id: str, version: int) -> WorkflowVersionRecord | None:
        if self.store is None:
            raise RuntimeError("get requires a WorkflowVersion store")
        return self.store.get(workflow_id, version)
