"""Explicit parent/child Run link and propagation boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from .ids import EntityId
from .serialization import freeze_json
from .versions import Revision


MAX_SUBFLOW_DEPTH = 16


class SubflowStatus(str, Enum):
    STARTING="starting"; RUNNING="running"; SUCCEEDED="succeeded"; FAILED="failed"; CANCELLED="cancelled"; UNKNOWN="unknown"


@dataclass(frozen=True)
class PropagationPolicy:
    parent_cancel_to_child: bool = True
    child_failure: str = "fail_parent"
    child_unknown: str = "wait"

    def __post_init__(self) -> None:
        if self.child_failure not in {"fail_parent","route_error","continue"} or self.child_unknown not in {"wait","fail_parent"}: raise ValueError("invalid Subflow propagation policy")


@dataclass(frozen=True)
class SubflowLink:
    link_id: EntityId
    parent_run_id: EntityId
    child_run_id: EntityId
    workflow_id: EntityId
    workflow_version: Revision
    correlation_id: EntityId
    propagation: PropagationPolicy
    input_mapping: Mapping[str, Any]
    output_mapping: Mapping[str, Any]
    artifact_scope: tuple[EntityId, ...]
    recursion_depth: int

    def __post_init__(self) -> None:
        if self.link_id.kind!="subflow_link" or self.parent_run_id.kind!="run" or self.child_run_id.kind!="run": raise ValueError("invalid Subflow ids")
        if self.parent_run_id==self.child_run_id or not 1<=self.recursion_depth<=MAX_SUBFLOW_DEPTH: raise ValueError("invalid Subflow recursion")
        object.__setattr__(self,"input_mapping",freeze_json(self.input_mapping)); object.__setattr__(self,"output_mapping",freeze_json(self.output_mapping)); object.__setattr__(self,"artifact_scope",tuple(sorted(self.artifact_scope)))

