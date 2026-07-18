"""Immutable references encoding the WorkflowVersion -> Plan -> Run spine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .ids import EntityId
from .serialization import freeze_json
from .versions import DefinitionHash, Revision


def _expect(identifier: EntityId, kind: str) -> None:
    if identifier.kind != kind:
        raise ValueError(f"expected {kind} id, got {identifier.kind}")


@dataclass(frozen=True)
class WorkflowVersionRef:
    workflow_id: EntityId
    version: Revision
    definition_hash: DefinitionHash

    def __post_init__(self) -> None:
        _expect(self.workflow_id, "workflow")


@dataclass(frozen=True)
class WorkflowRunRef:
    run_id: EntityId
    workflow_version: WorkflowVersionRef

    def __post_init__(self) -> None:
        _expect(self.run_id, "run")


@dataclass(frozen=True)
class ExecutionPlanRef:
    plan_id: EntityId
    run_id: EntityId
    plan_version: Revision
    workflow_version: WorkflowVersionRef

    def __post_init__(self) -> None:
        _expect(self.plan_id, "plan")
        _expect(self.run_id, "run")


@dataclass(frozen=True)
class NodeRunRef:
    node_run_id: EntityId
    run_id: EntityId
    plan_version: Revision
    node_id: str

    def __post_init__(self) -> None:
        _expect(self.node_run_id, "node_run")
        _expect(self.run_id, "run")
        if not self.node_id.strip():
            raise ValueError("node_id is required")


@dataclass(frozen=True)
class AttemptRef:
    attempt_id: EntityId
    node_run_id: EntityId
    number: Revision

    def __post_init__(self) -> None:
        _expect(self.attempt_id, "attempt")
        _expect(self.node_run_id, "node_run")


@dataclass(frozen=True)
class Value:
    """An immutable, inline JSON value carried through a typed port."""

    name: str
    schema_id: str
    data: Any

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.schema_id.strip():
            raise ValueError("value name and schema_id are required")
        object.__setattr__(self, "data", freeze_json(self.data))


@dataclass(frozen=True)
class ArtifactRef:
    """Metadata reference to immutable content stored by a later data layer."""

    artifact_id: EntityId
    schema_id: str
    content_type: str
    checksum: DefinitionHash
    size_bytes: int

    def __post_init__(self) -> None:
        _expect(self.artifact_id, "artifact")
        if not self.schema_id.strip() or not self.content_type.strip():
            raise ValueError("artifact schema_id and content_type are required")
        if isinstance(self.size_bytes, bool) or self.size_bytes < 0:
            raise ValueError("artifact size_bytes must be non-negative")
