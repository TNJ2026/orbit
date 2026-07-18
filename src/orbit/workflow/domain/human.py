"""Single HumanTask model used by approval, input and collaboration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
from typing import Any, Mapping

from .ids import EntityId
from .serialization import freeze_json
from .versions import AggregateVersion


class HumanTaskKind(str, Enum):
    APPROVAL = "approval"
    INPUT = "input"
    BUDGET = "budget"
    RECOVERY = "recovery"


class HumanTaskStatus(str, Enum):
    WAITING = "waiting"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class QuorumKind(str, Enum):
    ANY = "any"
    ALL = "all"
    N_OF_M = "n_of_m"


@dataclass(frozen=True)
class HumanSubmission:
    actor: str
    decision: str
    value: Any
    submitted_at: datetime

    def __post_init__(self) -> None:
        if not self.actor.strip() or self.decision not in {"approve", "reject", "provide_input", "withdraw"}:
            raise ValueError("invalid Human submission")
        if self.submitted_at.tzinfo is None:
            raise ValueError("submission time must be timezone-aware")
        object.__setattr__(self, "value", freeze_json(self.value))


@dataclass(frozen=True)
class HumanTask:
    task_id: EntityId
    run_id: EntityId
    kind: HumanTaskKind
    status: HumanTaskStatus
    request_hash: str
    submission_token_hash: str
    payload: Mapping[str, Any]
    assignee: str | None = None
    role: str | None = None
    form_schema: Mapping[str, Any] | None = None
    participants: tuple[str, ...] = ()
    quorum: QuorumKind = QuorumKind.ANY
    quorum_count: int = 1
    deadline_at: datetime | None = None
    reminder_interval_seconds: int | None = None
    escalation_policy: Mapping[str, Any] | None = None
    version: AggregateVersion = AggregateVersion(0)

    def __post_init__(self) -> None:
        if self.task_id.kind != "human_task" or self.run_id.kind != "run":
            raise ValueError("invalid HumanTask id kind")
        if not self.request_hash or not self.submission_token_hash:
            raise ValueError("HumanTask hashes are required")
        if len(set(self.participants)) != len(self.participants):
            raise ValueError("HumanTask participants must be unique")
        if self.quorum_count < 1 or (self.participants and self.quorum_count > len(self.participants)):
            raise ValueError("invalid HumanTask quorum")
        object.__setattr__(self, "payload", freeze_json(self.payload))
        object.__setattr__(self, "form_schema", None if self.form_schema is None else freeze_json(self.form_schema))
        object.__setattr__(self, "participants", tuple(sorted(self.participants)))
        object.__setattr__(self, "escalation_policy", None if self.escalation_policy is None else freeze_json(self.escalation_policy))


def submission_token_hash(token: str) -> str:
    if not token.strip():
        raise ValueError("submission token is required")
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()

