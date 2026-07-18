"""Deterministic Foreach identity, scope and aggregation contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .ids import EntityId
from .serialization import definition_hash, freeze_json
from .versions import Revision


MAX_FOREACH_ITEMS = 100_000


class ForeachFailurePolicy(str, Enum):
    FAIL_FAST = "fail_fast"
    CONTINUE = "continue"
    PARTIAL_SUCCESS = "partial_success"


class ForeachItemStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ItemScope:
    run_id: EntityId
    group_id: EntityId
    item_id: EntityId
    item_key: str
    item_index: int
    plan_version: Revision
    artifact_ids: tuple[EntityId, ...] = ()
    secret_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.group_id.kind != "foreach_group" or self.item_id.kind != "foreach_item": raise ValueError("invalid ItemScope ids")
        if self.item_index < 0 or not self.item_key: raise ValueError("invalid ItemScope key/index")
        object.__setattr__(self, "artifact_ids", tuple(sorted(self.artifact_ids)))
        object.__setattr__(self, "secret_refs", tuple(sorted(set(self.secret_refs))))


def derive_group_id(run_id: EntityId, node_id: str, source_checksum: str, plan_version: Revision) -> EntityId:
    digest=definition_hash({"run":str(run_id),"node":node_id,"source":source_checksum,"plan":plan_version.value})
    return EntityId("foreach_group",digest.value.removeprefix("sha256:"))


def derive_item_id(group_id: EntityId, item_key: str, item_index: int, source_checksum: str, plan_version: Revision) -> EntityId:
    digest=definition_hash({"group":str(group_id),"key":item_key,"index":item_index,"source":source_checksum,"plan":plan_version.value})
    return EntityId("foreach_item",digest.value.removeprefix("sha256:"))


def stable_aggregate(items: tuple[tuple[int, str, str, Any, Any], ...]) -> dict[str, Any]:
    ordered=sorted(items,key=lambda item:(item[0],item[1]))
    return freeze_json({"items":[{"index":i,"key":key,"status":status,"output":output,"error":error} for i,key,status,output,error in ordered],"partial":any(status!="succeeded" for _,_,status,_,_ in ordered)})

