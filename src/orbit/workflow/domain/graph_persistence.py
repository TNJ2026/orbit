"""Projection records introduced by static Graph Migration v5."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from .graph import JoinPolicy
from .ids import EntityId
from .serialization import freeze_json
from .versions import AggregateVersion


class JoinGroupStatus(str, Enum):
    WAITING = "waiting"
    OPEN = "open"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")


@dataclass(frozen=True)
class JoinGroupRecord:
    join_group_id: EntityId
    run_id: EntityId
    node_id: str
    generation: int
    policy: JoinPolicy
    participant_edge_ids: tuple[str, ...]
    status: JoinGroupStatus
    decision: Any
    aggregate_version: AggregateVersion
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.join_group_id.kind != "join_group" or self.run_id.kind != "run":
            raise ValueError("invalid JoinGroup identifier")
        if not self.node_id or self.generation < 1:
            raise ValueError("invalid JoinGroup node generation")
        if len(set(self.participant_edge_ids)) != len(self.participant_edge_ids):
            raise ValueError("JoinGroup participants must be unique")
        _aware(self.created_at); _aware(self.updated_at)
        object.__setattr__(self, "participant_edge_ids", tuple(self.participant_edge_ids))
        object.__setattr__(self, "decision", freeze_json(self.decision))


@dataclass(frozen=True)
class ControlCounterRecord:
    counter_id: EntityId
    run_id: EntityId
    policy_id: str
    scope_key: str
    value: int
    limit: int
    aggregate_version: AggregateVersion
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.counter_id.kind != "control_counter" or self.run_id.kind != "run":
            raise ValueError("invalid ControlCounter identifier")
        if not self.policy_id or not self.scope_key or self.value < 0 or self.limit < 1:
            raise ValueError("invalid ControlCounter")
        if self.value > self.limit:
            raise ValueError("ControlCounter cannot exceed its hard limit")
        _aware(self.updated_at)
