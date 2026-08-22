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


