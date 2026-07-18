"""Stable contracts for durable jobs, leases, timers, and worker fencing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from types import MappingProxyType
from typing import Any

from .ids import EntityId
from .serialization import freeze_json
from .states import JobStatus, LeaseStatus, TimerStatus
from .versions import AggregateVersion, Revision, SchemaVersion


MAX_JOB_LEASE_TTL = timedelta(minutes=5)


def _kind(identifier: EntityId | None, expected: str, field: str) -> None:
    if identifier is not None and identifier.kind != expected:
        raise ValueError(f"{field} must be a {expected} id")


def _aware(value: datetime | None, field: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field} must be timezone-aware")


class ExecutionSafety(str, Enum):
    REPLAY_SAFE = "replay_safe"
    UNKNOWN_ON_LEASE_LOSS = "unknown_on_lease_loss"


class TimerPurpose(str, Enum):
    JOB_BACKOFF = "job_backoff"
    NODE_TIMEOUT = "node_timeout"
    LEASE_RECOVERY = "lease_recovery"
    JOIN_DEADLINE = "join_deadline"
    PLANNER_TIMEOUT = "planner_timeout"
    HUMAN_REMINDER = "human_reminder"
    HUMAN_ESCALATION = "human_escalation"
    RUN_DEADLINE = "run_deadline"


@dataclass(frozen=True)
class JobScanCursor:
    priority: int
    available_at: datetime
    created_at: datetime
    job_id: EntityId

    def __post_init__(self) -> None:
        _kind(self.job_id, "job", "job_id")
        _aware(self.available_at, "available_at")
        _aware(self.created_at, "created_at")


@dataclass(frozen=True)
class LeaseScanCursor:
    expires_at: datetime
    lease_id: EntityId

    def __post_init__(self) -> None:
        _kind(self.lease_id, "lease", "lease_id")
        _aware(self.expires_at, "expires_at")


@dataclass(frozen=True)
class TimerScanCursor:
    due_at: datetime
    created_at: datetime
    timer_id: EntityId

    def __post_init__(self) -> None:
        _kind(self.timer_id, "timer", "timer_id")
        _aware(self.due_at, "due_at")
        _aware(self.created_at, "created_at")


DURABLE_COMMAND_TYPES = frozenset(
    {
        "claim_job", "start_job", "release_job", "defer_job",
        "complete_job", "fail_job", "expire_lease", "cancel_job",
        "schedule_timer", "claim_timer", "fire_timer",
        "expire_timer_lease", "cancel_timer", "materialize_job",
        "report_unknown_job_result",
    }
)

DURABLE_EVENT_VERSIONS = MappingProxyType(
    {
        "job_created": 1,
        "job_transitioned": 1,
        "job_attempt_assigned": 1,
        "lease_created": 1,
        "lease_transitioned": 1,
        "timer_created": 1,
        "timer_transitioned": 1,
        "timer_fired": 1,
        "attempt_usage_recorded": 1,
    }
)


@dataclass(frozen=True)
class JobRecord:
    job_id: EntityId
    run_id: EntityId
    node_run_id: EntityId
    current_attempt_id: EntityId | None
    job_kind: str
    execution_safety: ExecutionSafety
    status: JobStatus
    priority: int
    available_at: datetime
    delivery_count: int
    max_delivery_attempts: int
    aggregate_version: AggregateVersion
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _kind(self.job_id, "job", "job_id")
        _kind(self.run_id, "run", "run_id")
        _kind(self.node_run_id, "node_run", "node_run_id")
        _kind(self.current_attempt_id, "attempt", "current_attempt_id")
        if not self.job_kind.strip():
            raise ValueError("job_kind is required")
        if isinstance(self.priority, bool):
            raise ValueError("priority must be an integer")
        if self.delivery_count < 0:
            raise ValueError("delivery_count must be non-negative")
        if self.max_delivery_attempts < 1:
            raise ValueError("max_delivery_attempts must be positive")
        if self.delivery_count > self.max_delivery_attempts:
            raise ValueError("delivery_count exceeds max_delivery_attempts")
        _aware(self.available_at, "available_at")
        _aware(self.created_at, "created_at")
        _aware(self.updated_at, "updated_at")


@dataclass(frozen=True)
class LeaseRecord:
    lease_id: EntityId
    job_id: EntityId
    attempt_id: EntityId
    worker_id: str
    token_hash: str
    token_hash_version: SchemaVersion
    fencing_token: Revision
    status: LeaseStatus
    acquired_at: datetime
    expires_at: datetime
    released_at: datetime | None
    aggregate_version: AggregateVersion
    renewal_revision: int

    def __post_init__(self) -> None:
        _kind(self.lease_id, "lease", "lease_id")
        _kind(self.job_id, "job", "job_id")
        _kind(self.attempt_id, "attempt", "attempt_id")
        if not self.worker_id.strip() or not self.token_hash.strip():
            raise ValueError("worker_id and token_hash are required")
        if self.renewal_revision < 0:
            raise ValueError("renewal_revision must be non-negative")
        for field in ("acquired_at", "expires_at", "released_at"):
            _aware(getattr(self, field), field)
        if self.expires_at <= self.acquired_at:
            raise ValueError("expires_at must be after acquired_at")
        if self.status is LeaseStatus.ACTIVE and self.released_at is not None:
            raise ValueError("active lease cannot have released_at")
        if self.status is not LeaseStatus.ACTIVE and self.released_at is None:
            raise ValueError("terminal lease requires released_at")


@dataclass(frozen=True)
class DurableTimerRecord:
    timer_id: EntityId
    run_id: EntityId
    purpose: TimerPurpose
    dedupe_key: str
    target_type: str
    target_id: EntityId
    payload_schema_version: SchemaVersion
    payload: Any
    status: TimerStatus
    due_at: datetime
    fired_at: datetime | None
    lease_owner: str | None
    lease_token_hash: str | None
    lease_fencing_token: int
    lease_expires_at: datetime | None
    aggregate_version: AggregateVersion
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _kind(self.timer_id, "timer", "timer_id")
        _kind(self.run_id, "run", "run_id")
        if not self.dedupe_key.strip() or not self.target_type.strip():
            raise ValueError("dedupe_key and target_type are required")
        if self.lease_fencing_token < 0:
            raise ValueError("lease_fencing_token must be non-negative")
        for field in ("due_at", "fired_at", "lease_expires_at", "created_at", "updated_at"):
            _aware(getattr(self, field), field)
        leased = self.status is TimerStatus.LEASED
        lease_values = (self.lease_owner, self.lease_token_hash, self.lease_expires_at)
        if leased and any(value is None for value in lease_values):
            raise ValueError("leased timer requires complete lease metadata")
        if not leased and any(value is not None for value in lease_values):
            raise ValueError("non-leased timer cannot retain lease metadata")
        if self.status is TimerStatus.FIRED and self.fired_at is None:
            raise ValueError("fired timer requires fired_at")
        if self.status is not TimerStatus.FIRED and self.fired_at is not None:
            raise ValueError("non-fired timer cannot have fired_at")
        object.__setattr__(self, "payload", freeze_json(self.payload))
