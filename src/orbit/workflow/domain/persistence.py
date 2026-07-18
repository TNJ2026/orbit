"""Stable persistence records and errors for the deterministic runtime."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .envelopes import EventEnvelope
from .ids import EntityId
from .serialization import freeze_json
from .states import AttemptStatus, BranchTokenStatus, NodeRunStatus, WorkflowRunStatus
from .versions import AggregateVersion, DefinitionHash, Revision, SchemaVersion


def _kind(identifier: EntityId, expected: str) -> None:
    if identifier.kind != expected:
        raise ValueError(f"expected {expected} id, got {identifier.kind}")


def _aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


class PersistenceError(RuntimeError):
    """Base class for stable persistence failures."""


class ConcurrencyConflictError(PersistenceError):
    def __init__(self, aggregate_id: EntityId, expected: int, actual: int) -> None:
        self.aggregate_id = aggregate_id
        self.expected = expected
        self.actual = actual
        super().__init__(f"{aggregate_id} expected version {expected}, actual {actual}")


class IdempotencyConflictError(PersistenceError):
    pass


class DuplicateEventIdError(PersistenceError):
    pass


class EventSequenceError(PersistenceError):
    pass


class UnsupportedEventVersionError(PersistenceError):
    pass


class SnapshotCorruptionError(PersistenceError):
    pass


class IntegrityViolationError(PersistenceError):
    pass


class DatabaseBusyError(PersistenceError):
    pass


class RepositoryNotFoundError(PersistenceError):
    pass


class RepositoryAlreadyExistsError(PersistenceError):
    pass


PERSISTENCE_ERROR_REGISTRY = {
    "CONCURRENCY_CONFLICT": ConcurrencyConflictError,
    "IDEMPOTENCY_CONFLICT": IdempotencyConflictError,
    "DUPLICATE_EVENT_ID": DuplicateEventIdError,
    "EVENT_SEQUENCE": EventSequenceError,
    "UNSUPPORTED_EVENT_VERSION": UnsupportedEventVersionError,
    "SNAPSHOT_CORRUPTION": SnapshotCorruptionError,
    "INTEGRITY_VIOLATION": IntegrityViolationError,
    "DATABASE_BUSY": DatabaseBusyError,
    "REPOSITORY_NOT_FOUND": RepositoryNotFoundError,
    "REPOSITORY_ALREADY_EXISTS": RepositoryAlreadyExistsError,
}


@dataclass(frozen=True)
class StoredEvent:
    run_id: EntityId
    global_position: int
    envelope: EventEnvelope

    def __post_init__(self) -> None:
        _kind(self.run_id, "run")
        if isinstance(self.global_position, bool) or self.global_position < 1:
            raise ValueError("global_position must be a positive integer")


@dataclass(frozen=True)
class CommandReceipt:
    run_id: EntityId
    aggregate_id: EntityId
    idempotency_key: str
    command_fingerprint: DefinitionHash
    command_id: EntityId
    expected_version: AggregateVersion
    result_event_ids: tuple[EntityId, ...]
    committed_at: datetime

    def __post_init__(self) -> None:
        _kind(self.run_id, "run")
        _kind(self.command_id, "command")
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        if not self.result_event_ids:
            raise ValueError("a committed receipt requires result event ids")
        if any(item.kind != "event" for item in self.result_event_ids):
            raise ValueError("receipt results must be event ids")
        _aware(self.committed_at, "committed_at")
        object.__setattr__(self, "result_event_ids", tuple(self.result_event_ids))


@dataclass(frozen=True)
class SnapshotRecord:
    snapshot_id: EntityId
    run_id: EntityId
    snapshot_sequence: Revision
    snapshot_schema_version: SchemaVersion
    reducer_version: SchemaVersion
    last_global_position: int
    last_run_event_sequence: AggregateVersion
    state: Any
    checksum: DefinitionHash
    created_at: datetime

    def __post_init__(self) -> None:
        _kind(self.snapshot_id, "snapshot")
        _kind(self.run_id, "run")
        if isinstance(self.last_global_position, bool) or self.last_global_position < 0:
            raise ValueError("last_global_position must be non-negative")
        _aware(self.created_at, "created_at")
        object.__setattr__(self, "state", freeze_json(self.state))


@dataclass(frozen=True)
class WorkflowRunRecord:
    run_id: EntityId
    workflow_id: EntityId
    workflow_version: Revision
    definition_hash: DefinitionHash
    status: WorkflowRunStatus
    aggregate_version: AggregateVersion
    correlation_id: EntityId
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _kind(self.run_id, "run")
        _kind(self.workflow_id, "workflow")
        _kind(self.correlation_id, "run")
        _aware(self.created_at, "created_at")
        _aware(self.updated_at, "updated_at")


@dataclass(frozen=True)
class ExecutionPlanRecord:
    plan_id: EntityId
    run_id: EntityId
    plan_version: Revision
    workflow_id: EntityId
    workflow_version: Revision
    plan_schema_version: SchemaVersion
    plan: Any
    definition_hash: DefinitionHash
    created_event_id: EntityId
    created_at: datetime

    def __post_init__(self) -> None:
        _kind(self.plan_id, "plan")
        _kind(self.run_id, "run")
        _kind(self.workflow_id, "workflow")
        _kind(self.created_event_id, "event")
        _aware(self.created_at, "created_at")
        object.__setattr__(self, "plan", freeze_json(self.plan))


@dataclass(frozen=True)
class NodeRunRecord:
    node_run_id: EntityId
    run_id: EntityId
    node_id: str
    source_plan_version: Revision
    status: NodeRunStatus
    aggregate_version: AggregateVersion
    created_at: datetime
    updated_at: datetime
    generation: int = 1
    activation_key: str = "legacy"

    def __post_init__(self) -> None:
        _kind(self.node_run_id, "node_run")
        _kind(self.run_id, "run")
        if not self.node_id.strip():
            raise ValueError("node_id is required")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 1:
            raise ValueError("generation must be positive")
        if not self.activation_key.strip():
            raise ValueError("activation_key is required")
        _aware(self.created_at, "created_at")
        _aware(self.updated_at, "updated_at")


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: EntityId
    node_run_id: EntityId
    attempt_number: Revision
    status: AttemptStatus
    aggregate_version: AggregateVersion
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _kind(self.attempt_id, "attempt")
        _kind(self.node_run_id, "node_run")
        _aware(self.created_at, "created_at")
        _aware(self.updated_at, "updated_at")


@dataclass(frozen=True)
class BranchTokenRecord:
    token_id: EntityId
    run_id: EntityId
    source_node_run_id: EntityId | None
    status: BranchTokenStatus
    aggregate_version: AggregateVersion
    scope: Any
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _kind(self.token_id, "branch_token")
        _kind(self.run_id, "run")
        if self.source_node_run_id is not None:
            _kind(self.source_node_run_id, "node_run")
        _aware(self.created_at, "created_at")
        _aware(self.updated_at, "updated_at")
        object.__setattr__(self, "scope", freeze_json(self.scope))
