"""Stable structural ports used by the runtime persistence boundary."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable

from .envelopes import CommandEnvelope, EventEnvelope
from .ids import EntityId
from .persistence import (
    AttemptRecord, BranchTokenRecord, ExecutionPlanRecord, NodeRunRecord,
    SnapshotRecord, StoredEvent, WorkflowRunRecord,
)
from .versions import AggregateVersion, SchemaVersion


@runtime_checkable
class EventStorePort(Protocol):
    def stream_head(self, aggregate_id: EntityId) -> AggregateVersion: ...

    def append(
        self,
        run_id: EntityId,
        aggregate_id: EntityId,
        expected_version: AggregateVersion,
        events: Iterable[EventEnvelope],
    ) -> tuple[StoredEvent, ...]: ...

    def read_stream(
        self,
        aggregate_id: EntityId,
        *,
        after_sequence: int = 0,
        to_sequence: int | None = None,
        limit: int = 1000,
    ) -> tuple[StoredEvent, ...]: ...

    def read_run(
        self,
        run_id: EntityId,
        *,
        after_global_position: int = 0,
        limit: int = 1000,
    ) -> tuple[StoredEvent, ...]: ...


@runtime_checkable
class SnapshotStorePort(Protocol):
    def append(self, snapshot: SnapshotRecord) -> None: ...

    def load_latest_compatible(
        self,
        run_id: EntityId,
        *,
        snapshot_schema_version: SchemaVersion,
        reducer_version: SchemaVersion,
    ) -> Any: ...


@runtime_checkable
class UnitOfWorkPort(Protocol):
    events: EventStorePort
    snapshots: SnapshotStorePort
    receipts: Any
    runs: Any
    plans: Any
    node_runs: Any
    attempts: Any
    tokens: Any
    jobs: Any
    leases: Any
    timers: Any
    values: Any
    value_links: Any
    artifacts: Any
    artifact_links: Any
    joins: Any
    counters: Any
    planner_attempts: Any
    planner_proposals: Any

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


@runtime_checkable
class WorkflowRunRepositoryPort(Protocol):
    def create(self, record: WorkflowRunRecord) -> None: ...
    def get(self, run_id: EntityId) -> WorkflowRunRecord | None: ...
    def update(self, record: WorkflowRunRecord, expected: AggregateVersion) -> None: ...
    def list_non_terminal(self, *, after_run_id: str = "", limit: int = 100) -> tuple[WorkflowRunRecord, ...]: ...


@runtime_checkable
class ExecutionPlanRepositoryPort(Protocol):
    def append(self, record: ExecutionPlanRecord) -> None: ...
    def get(self, run_id: EntityId, version: Any) -> ExecutionPlanRecord | None: ...
    def list_versions(self, run_id: EntityId) -> tuple[ExecutionPlanRecord, ...]: ...


@runtime_checkable
class NodeRunRepositoryPort(Protocol):
    def create(self, record: NodeRunRecord) -> None: ...
    def get(self, node_run_id: EntityId) -> NodeRunRecord | None: ...
    def list_by_run(self, run_id: EntityId) -> tuple[NodeRunRecord, ...]: ...
    def update(self, record: NodeRunRecord, expected: AggregateVersion) -> None: ...


@runtime_checkable
class AttemptRepositoryPort(Protocol):
    def create(self, record: AttemptRecord) -> None: ...
    def get(self, attempt_id: EntityId) -> AttemptRecord | None: ...
    def list_by_node_run(self, node_run_id: EntityId) -> tuple[AttemptRecord, ...]: ...
    def update(self, record: AttemptRecord, expected: AggregateVersion) -> None: ...


@runtime_checkable
class BranchTokenRepositoryPort(Protocol):
    def create(self, record: BranchTokenRecord) -> None: ...
    def get(self, token_id: EntityId) -> BranchTokenRecord | None: ...
    def list_by_run(self, run_id: EntityId, *, active_only: bool = False) -> tuple[BranchTokenRecord, ...]: ...
    def update(self, record: BranchTokenRecord, expected: AggregateVersion) -> None: ...


@runtime_checkable
class CommandReceiptStorePort(Protocol):
    def decide(self, command: CommandEnvelope) -> Any: ...
    def record(self, run_id: EntityId, command: CommandEnvelope, result_event_ids: tuple[EntityId, ...], committed_at: Any) -> Any: ...


@runtime_checkable
class WorkflowVersionReaderPort(Protocol):
    def get(self, workflow_id: str, version: int) -> Any: ...
