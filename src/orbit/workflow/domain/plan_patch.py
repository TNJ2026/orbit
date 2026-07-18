"""Stable dynamic-plan contracts shared by Steps 10 and 11."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from .ids import EntityId
from .serialization import definition_hash, freeze_json, to_primitive
from .versions import DefinitionHash, Revision, SchemaVersion


PLAN_PATCH_SCHEMA_VERSION = SchemaVersion("1.0")
MAX_PATCH_NODES = 64
MAX_PATCH_EDGES = 256
MAX_DYNAMIC_WIDTH = 32
MAX_DYNAMIC_DEPTH = 16


class PatchOperationKind(str, Enum):
    ADD_NODE = "add_node"
    ADD_EDGE = "add_edge"
    REMOVE_PENDING_NODE = "remove_pending_node"
    REMOVE_PENDING_EDGE = "remove_pending_edge"
    REPLACE_PENDING_NODE = "replace_pending_node"


@dataclass(frozen=True)
class AgenticRegion:
    region_id: str
    mutable_node_ids: tuple[str, ...]
    boundary_node_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.region_id.strip():
            raise ValueError("AgenticRegion id is required")
        if len(set(self.mutable_node_ids)) != len(self.mutable_node_ids):
            raise ValueError("AgenticRegion nodes must be unique")
        object.__setattr__(self, "mutable_node_ids", tuple(sorted(self.mutable_node_ids)))
        object.__setattr__(self, "boundary_node_ids", tuple(sorted(self.boundary_node_ids)))


@dataclass(frozen=True)
class PatchOperation:
    kind: PatchOperationKind
    target_id: str
    value: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.target_id.strip():
            raise ValueError("Patch operation target is required")
        if self.kind in {PatchOperationKind.ADD_NODE, PatchOperationKind.ADD_EDGE, PatchOperationKind.REPLACE_PENDING_NODE} and self.value is None:
            raise ValueError(f"{self.kind.value} requires value")
        if self.kind in {PatchOperationKind.REMOVE_PENDING_NODE, PatchOperationKind.REMOVE_PENDING_EDGE} and self.value is not None:
            raise ValueError(f"{self.kind.value} cannot carry value")
        if self.value is not None:
            object.__setattr__(self, "value", freeze_json(self.value))


@dataclass(frozen=True)
class PlanPatch:
    patch_id: EntityId
    proposal_id: EntityId
    run_id: EntityId
    base_plan_version: Revision
    reason: str
    operations: tuple[PatchOperation, ...]
    content_hash: DefinitionHash | None = None
    schema_version: SchemaVersion = PLAN_PATCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.patch_id.kind != "plan_patch" or self.proposal_id.kind != "proposal" or self.run_id.kind != "run":
            raise ValueError("invalid PlanPatch identifier kind")
        if not self.reason.strip() or not self.operations:
            raise ValueError("PlanPatch reason and operations are required")
        if len(self.operations) > MAX_PATCH_NODES + MAX_PATCH_EDGES:
            raise ValueError("PlanPatch exceeds operation limit")
        object.__setattr__(self, "operations", tuple(self.operations))
        primitive = {
            "schema_version": self.schema_version.value,
            "patch_id": str(self.patch_id), "proposal_id": str(self.proposal_id),
            "run_id": str(self.run_id), "base_plan_version": self.base_plan_version.value,
            "reason": self.reason, "operations": to_primitive(self.operations),
        }
        calculated = definition_hash(primitive)
        if self.content_hash is not None and self.content_hash != calculated:
            raise ValueError("PlanPatch content hash mismatch")
        object.__setattr__(self, "content_hash", calculated)


@dataclass(frozen=True)
class DynamicDagLimits:
    max_nodes_per_patch: int = MAX_PATCH_NODES
    max_edges_per_patch: int = MAX_PATCH_EDGES
    max_width: int = MAX_DYNAMIC_WIDTH
    max_depth: int = MAX_DYNAMIC_DEPTH
    max_iterations: int = 64

    def __post_init__(self) -> None:
        if any(isinstance(value, bool) or value < 1 for value in (
            self.max_nodes_per_patch, self.max_edges_per_patch, self.max_width,
            self.max_depth, self.max_iterations,
        )):
            raise ValueError("dynamic DAG limits must be positive")

