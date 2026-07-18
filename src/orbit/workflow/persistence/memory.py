"""Small in-memory adapters for fast persistence contract tests."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime

from ..domain.concurrency import CommandDecision, CommandDisposition, command_fingerprint
from ..domain.data import ArtifactStatus
from ..domain.envelopes import EventEnvelope
from ..domain.durable_execution import (
    DurableTimerRecord, JobRecord, JobScanCursor, LeaseRecord, LeaseScanCursor,
    TimerScanCursor,
)
from ..domain.ids import EntityId
from ..domain.graph_persistence import ControlCounterRecord, JoinGroupRecord, JoinGroupStatus
from ..domain.persistence import (
    AttemptRecord,
    BranchTokenRecord,
    CommandReceipt,
    ConcurrencyConflictError,
    DuplicateEventIdError,
    EventSequenceError,
    ExecutionPlanRecord,
    IntegrityViolationError,
    IdempotencyConflictError,
    NodeRunRecord,
    RepositoryAlreadyExistsError,
    SnapshotRecord,
    StoredEvent,
    WorkflowRunRecord,
)
from ..domain.serialization import definition_hash
from ..domain.states import BranchTokenStatus
from ..domain.versions import AggregateVersion, SchemaVersion
from .snapshots import SnapshotLoadResult, snapshot_checksum


class MemoryEventStore:
    def __init__(self) -> None:
        self._events: list[StoredEvent] = []

    def stream_head(self, aggregate_id: EntityId) -> AggregateVersion:
        return AggregateVersion(
            max(
                (item.envelope.sequence.value for item in self._events if item.envelope.aggregate_id == aggregate_id),
                default=0,
            )
        )

    def append(self, run_id, aggregate_id, expected_version, events: Iterable[EventEnvelope]):
        values = tuple(events)
        if not values:
            raise ValueError("event append requires at least one event")
        actual = self.stream_head(aggregate_id)
        if actual != expected_version:
            raise ConcurrencyConflictError(aggregate_id, expected_version.value, actual.value)
        known = {item.envelope.event_id for item in self._events}
        for offset, event in enumerate(values, 1):
            if event.event_id in known:
                raise DuplicateEventIdError(str(event.event_id))
            if event.aggregate_id != aggregate_id or event.sequence.value != expected_version.value + offset:
                raise EventSequenceError("event sequence or aggregate mismatch")
            known.add(event.event_id)
        stored = []
        base = len(self._events)
        for offset, event in enumerate(values, 1):
            item = StoredEvent(run_id, base + offset, event)
            self._events.append(item)
            stored.append(item)
        return tuple(stored)

    def read_stream(self, aggregate_id, *, after_sequence=0, to_sequence=None, limit=1000):
        return tuple(
            item for item in self._events
            if item.envelope.aggregate_id == aggregate_id
            and item.envelope.sequence.value > after_sequence
            and (to_sequence is None or item.envelope.sequence.value <= to_sequence)
        )[:limit]

    def read_run(self, run_id, *, after_global_position=0, limit=1000):
        return tuple(
            item for item in self._events
            if item.run_id == run_id and item.global_position > after_global_position
        )[:limit]

    def read_all(self, *, after_global_position=0, limit=1000):
        return tuple(item for item in self._events if item.global_position > after_global_position)[:limit]


class MemorySnapshotStore:
    def __init__(self) -> None:
        self._snapshots: list[SnapshotRecord] = []

    def append(self, snapshot: SnapshotRecord) -> None:
        if snapshot.checksum != snapshot_checksum(snapshot):
            raise ValueError("snapshot checksum mismatch")
        self._snapshots.append(snapshot)

    def load_latest_compatible(
        self, run_id, *, snapshot_schema_version: SchemaVersion, reducer_version: SchemaVersion
    ) -> SnapshotLoadResult:
        ignored = []
        candidates = sorted(
            (item for item in self._snapshots if item.run_id == run_id),
            key=lambda item: item.snapshot_sequence.value,
            reverse=True,
        )
        for item in candidates:
            if item.snapshot_schema_version == snapshot_schema_version and item.reducer_version == reducer_version:
                if item.checksum == snapshot_checksum(item):
                    return SnapshotLoadResult(item, tuple(ignored))
                ignored.append(f"{item.snapshot_id}: checksum mismatch")
            else:
                ignored.append(f"{item.snapshot_id}: incompatible version")
        return SnapshotLoadResult(None, tuple(ignored))

    def delete(self, snapshot_id: EntityId) -> None:
        self._snapshots = [item for item in self._snapshots if item.snapshot_id != snapshot_id]

    def list(self, run_id: EntityId) -> tuple[SnapshotRecord, ...]:
        return tuple(item for item in self._snapshots if item.run_id == run_id)


class MemoryCommandReceiptStore:
    def __init__(self, events: MemoryEventStore) -> None:
        self.events = events
        self._receipts: dict[tuple[EntityId, str], CommandReceipt] = {}

    def get(self, aggregate_id: EntityId, idempotency_key: str):
        return self._receipts.get((aggregate_id, idempotency_key))

    def decide(self, command):
        receipt = self.get(command.aggregate_id, command.idempotency_key)
        if receipt is None:
            return None
        if receipt.command_fingerprint != command_fingerprint(command):
            raise IdempotencyConflictError(
                "idempotency key was already used for a different command"
            )
        return CommandDecision(
            CommandDisposition.REPLAY_PRIOR_RESULT, receipt.result_event_ids
        )

    def record(
        self, run_id: EntityId, command, result_event_ids: tuple[EntityId, ...],
        committed_at: datetime,
    ) -> CommandReceipt:
        known = {
            item.envelope.event_id: item.envelope.causation_id
            for item in self.events._events
        }
        if not result_event_ids or any(item not in known for item in result_event_ids):
            raise ValueError("receipt references missing events")
        if any(known[item] != command.command_id for item in result_event_ids):
            raise ValueError("receipt events must be caused by the command")
        receipt = CommandReceipt(
            run_id, command.aggregate_id, command.idempotency_key,
            command_fingerprint(command), command.command_id,
            command.expected_version, result_event_ids, committed_at,
        )
        key = (command.aggregate_id, command.idempotency_key)
        if key in self._receipts:
            raise IdempotencyConflictError("duplicate in-memory command receipt")
        self._receipts[key] = receipt
        return receipt


class _MemoryProjectionRepository:
    def __init__(self, events: MemoryEventStore) -> None:
        self.events = events

    def _update(self, records, identifier, record, expected) -> None:
        prior = records.get(identifier)
        actual = -1 if prior is None else prior.aggregate_version.value
        if actual != expected.value:
            raise ConcurrencyConflictError(identifier, expected.value, actual)
        if self.events.stream_head(identifier) != record.aggregate_version:
            raise IntegrityViolationError("projection version does not match stream head")
        records[identifier] = record


class MemoryWorkflowRunRepository(_MemoryProjectionRepository):
    def __init__(self, events: MemoryEventStore) -> None:
        super().__init__(events)
        self.records: dict[EntityId, WorkflowRunRecord] = {}

    def create(self, record: WorkflowRunRecord) -> None:
        if record.aggregate_version != AggregateVersion(0):
            raise ValueError("new projection must start at version 0")
        if record.run_id in self.records:
            raise RepositoryAlreadyExistsError(str(record.run_id))
        self.records[record.run_id] = record

    def get(self, run_id): return self.records.get(run_id)
    def update(self, record, expected): self._update(self.records, record.run_id, record, expected)
    def list_non_terminal(self, *, after_run_id="", limit=100):
        terminal = {"succeeded", "failed", "cancelled"}
        return tuple(
            item for item in sorted(self.records.values(), key=lambda value: str(value.run_id))
            if str(item.run_id) > after_run_id and item.status.value not in terminal
        )[:limit]


class MemoryExecutionPlanRepository(_MemoryProjectionRepository):
    def __init__(self, events: MemoryEventStore) -> None:
        super().__init__(events)
        self.records: dict[tuple[EntityId, int], ExecutionPlanRecord] = {}

    def append(self, record: ExecutionPlanRecord) -> None:
        if definition_hash(record.plan) != record.definition_hash:
            raise IntegrityViolationError("ExecutionPlan definition hash mismatch")
        if not any(item.envelope.event_id == record.created_event_id and item.run_id == record.run_id for item in self.events._events):
            raise IntegrityViolationError("ExecutionPlan creation event is missing")
        key = (record.run_id, record.plan_version.value)
        if key in self.records:
            raise RepositoryAlreadyExistsError(str(record.plan_id))
        self.records[key] = record

    def get(self, run_id, version): return self.records.get((run_id, version.value))
    def list_versions(self, run_id):
        return tuple(value for key, value in sorted(self.records.items(), key=lambda item: item[0][1]) if key[0] == run_id)


class MemoryNodeRunRepository(_MemoryProjectionRepository):
    def __init__(self, events):
        super().__init__(events); self.records: dict[EntityId, NodeRunRecord] = {}
    def create(self, record): self._create(record.node_run_id, record)
    def _create(self, key, record):
        if record.aggregate_version != AggregateVersion(0): raise ValueError("new projection must start at version 0")
        if key in self.records: raise RepositoryAlreadyExistsError(str(key))
        self.records[key] = record
    def get(self, key): return self.records.get(key)
    def list_by_run(self, run_id): return tuple(item for item in self.records.values() if item.run_id == run_id)
    def update(self, record, expected): self._update(self.records, record.node_run_id, record, expected)


class MemoryAttemptRepository(MemoryNodeRunRepository):
    def create(self, record): self._create(record.attempt_id, record)
    def list_by_node_run(self, node_run_id): return tuple(item for item in self.records.values() if item.node_run_id == node_run_id)
    def update(self, record, expected): self._update(self.records, record.attempt_id, record, expected)


class MemoryBranchTokenRepository(MemoryNodeRunRepository):
    def create(self, record): self._create(record.token_id, record)
    def list_by_run(self, run_id, *, active_only=False):
        return tuple(item for item in self.records.values() if item.run_id == run_id and (not active_only or item.status == BranchTokenStatus.ACTIVE))
    def update(self, record, expected): self._update(self.records, record.token_id, record, expected)


class MemoryJoinGroupRepository(MemoryNodeRunRepository):
    def create(self, record): self._create(record.join_group_id, record)
    def list_by_run(self, run_id, *, waiting_only=False):
        return tuple(sorted(
            (item for item in self.records.values() if item.run_id == run_id and (not waiting_only or item.status is JoinGroupStatus.WAITING)),
            key=lambda item: str(item.join_group_id),
        ))
    def update(self, record, expected): self._update(self.records, record.join_group_id, record, expected)


class MemoryControlCounterRepository(MemoryNodeRunRepository):
    def create(self, record): self._create(record.counter_id, record)
    def list_by_run(self, run_id):
        return tuple(sorted((item for item in self.records.values() if item.run_id == run_id), key=lambda item: str(item.counter_id)))
    def increment(self, identifier, expected, now):
        record = self.records.get(identifier)
        if record is None: raise ValueError("ControlCounter was not found")
        if record.aggregate_version != expected:
            raise ConcurrencyConflictError(identifier, expected.value, record.aggregate_version.value)
        if record.value >= record.limit: raise ValueError("ControlCounter hard limit exhausted")
        updated = replace(record, value=record.value + 1, aggregate_version=record.aggregate_version.next(), updated_at=now)
        self.records[identifier] = updated
        return updated


class MemoryJobRepository(_MemoryProjectionRepository):
    def __init__(self, events):
        super().__init__(events)
        self.records: dict[EntityId, JobRecord] = {}
        self.active_keys: set[tuple[EntityId, str]] = set()

    def create(self, record):
        if record.aggregate_version != AggregateVersion(0):
            raise ValueError("new Job projection must start at version 0")
        if record.job_id in self.records:
            raise RepositoryAlreadyExistsError(str(record.job_id))
        active = {"ready", "leased", "running", "retry_wait"}
        key = (record.node_run_id, record.job_kind)
        if key in self.active_keys:
            raise RepositoryAlreadyExistsError(str(record.node_run_id))
        self.records[record.job_id] = record
        if record.status.value in active: self.active_keys.add(key)

    def get(self, key): return self.records.get(key)
    def update(self, record, expected):
        prior = self.records.get(record.job_id)
        self._update(self.records, record.job_id, record, expected)
        active = {"ready", "leased", "running", "retry_wait"}
        key = (record.node_run_id, record.job_kind)
        if prior is not None and prior.status.value in active: self.active_keys.discard(key)
        if record.status.value in active: self.active_keys.add(key)
    def list_by_run(self, run_id):
        return tuple(sorted(
            (item for item in self.records.values() if item.run_id == run_id),
            key=lambda item: (item.created_at, str(item.job_id)),
        ))
    def list_claimable(self, now, *, after: JobScanCursor | None = None, limit=100):
        if limit < 1 or limit > 1000: raise ValueError("limit must be between 1 and 1000")
        values = sorted(
            (item for item in self.records.values() if item.status.value == "ready" and item.available_at <= now),
            key=lambda item: (-item.priority, item.available_at, item.created_at, str(item.job_id)),
        )
        if after is not None:
            cursor = (-after.priority, after.available_at, after.created_at, str(after.job_id))
            values = [item for item in values if (-item.priority, item.available_at, item.created_at, str(item.job_id)) > cursor]
        return tuple(values[:limit])


class MemoryLeaseRepository(_MemoryProjectionRepository):
    def __init__(self, events):
        super().__init__(events)
        self.records: dict[EntityId, LeaseRecord] = {}

    def create(self, record):
        if record.aggregate_version != AggregateVersion(0): raise ValueError("new Lease projection must start at version 0")
        if record.lease_id in self.records or any(item.job_id == record.job_id and item.status.value == "active" for item in self.records.values()):
            raise RepositoryAlreadyExistsError(str(record.lease_id))
        if any(item.job_id == record.job_id and item.fencing_token == record.fencing_token for item in self.records.values()):
            raise RepositoryAlreadyExistsError(str(record.fencing_token.value))
        self.records[record.lease_id] = record

    def get(self, key): return self.records.get(key)
    def get_active_for_job(self, job_id):
        return next((item for item in self.records.values() if item.job_id == job_id and item.status.value == "active"), None)
    def list_by_job(self, job_id):
        return tuple(sorted(
            (item for item in self.records.values() if item.job_id == job_id),
            key=lambda item: item.fencing_token.value,
        ))
    def update(self, record, expected): self._update(self.records, record.lease_id, record, expected)
    def renew(self, lease_id, *, token_hash, fencing_token, expected_revision, expires_at):
        current = self.get(lease_id)
        actual = -1 if current is None else current.renewal_revision
        if current is None or current.status.value != "active" or current.token_hash != token_hash or current.fencing_token.value != fencing_token or actual != expected_revision or expires_at <= current.expires_at:
            raise ConcurrencyConflictError(lease_id, expected_revision, actual)
        renewed = replace(current, expires_at=expires_at, renewal_revision=actual + 1)
        self.records[lease_id] = renewed
        return renewed
    def list_expired(self, now, *, after: LeaseScanCursor | None = None, limit=100):
        if limit < 1 or limit > 1000: raise ValueError("limit must be between 1 and 1000")
        values = sorted(
            (item for item in self.records.values() if item.status.value == "active" and item.expires_at <= now),
            key=lambda item: (item.expires_at, str(item.lease_id)),
        )
        if after is not None:
            cursor = (after.expires_at, str(after.lease_id))
            values = [item for item in values if (item.expires_at, str(item.lease_id)) > cursor]
        return tuple(values[:limit])


class MemoryTimerRepository(_MemoryProjectionRepository):
    def __init__(self, events):
        super().__init__(events)
        self.records: dict[EntityId, DurableTimerRecord] = {}
        self.dedupe_keys: set[tuple[EntityId, object, str]] = set()

    def create(self, record):
        if record.aggregate_version != AggregateVersion(0): raise ValueError("new Timer projection must start at version 0")
        key = (record.run_id, record.purpose, record.dedupe_key)
        if record.timer_id in self.records or key in self.dedupe_keys:
            raise RepositoryAlreadyExistsError(str(record.timer_id))
        self.records[record.timer_id] = record
        self.dedupe_keys.add(key)
    def get(self, key): return self.records.get(key)
    def get_by_dedupe(self, run_id, purpose, dedupe_key):
        purpose_value = getattr(purpose, "value", purpose)
        return next((item for item in self.records.values() if item.run_id == run_id and item.purpose.value == purpose_value and item.dedupe_key == dedupe_key), None)
    def update(self, record, expected): self._update(self.records, record.timer_id, record, expected)
    def list_due(self, now, *, after: TimerScanCursor | None = None, limit=100):
        if limit < 1 or limit > 1000: raise ValueError("limit must be between 1 and 1000")
        values = sorted(
            (item for item in self.records.values() if item.status.value == "scheduled" and item.due_at <= now),
            key=lambda item: (item.due_at, item.created_at, str(item.timer_id)),
        )
        if after is not None:
            cursor = (after.due_at, after.created_at, str(after.timer_id))
            values = [item for item in values if (item.due_at, item.created_at, str(item.timer_id)) > cursor]
        return tuple(values[:limit])
    def list_by_run(self, run_id):
        return tuple(sorted(
            (item for item in self.records.values() if item.run_id == run_id),
            key=lambda item: (item.created_at, str(item.timer_id)),
        ))
    def list_expired_leases(self, now, *, limit=100):
        if limit < 1 or limit > 1000: raise ValueError("limit must be between 1 and 1000")
        return tuple(sorted(
            (
                item for item in self.records.values()
                if item.status.value == "leased" and item.lease_expires_at <= now
            ),
            key=lambda item: (item.lease_expires_at, str(item.timer_id)),
        )[:limit])


class MemoryValueRepository:
    def __init__(self): self.records = {}
    def insert(self, record):
        owner_key = (record.owner_kind, record.owner_id, record.port_id)
        if record.value_id in self.records or any(
            (item.owner_kind, item.owner_id, item.port_id) == owner_key
            for item in self.records.values()
        ):
            raise RepositoryAlreadyExistsError(str(record.value_id))
        self.records[record.value_id] = record
    def get(self, value_id): return self.records.get(value_id)
    def get_by_owner_port(self, owner_kind, owner_id, port_id):
        kind = getattr(owner_kind, "value", owner_kind)
        return next((item for item in self.records.values() if item.owner_kind.value == kind and item.owner_id == owner_id and item.port_id == port_id), None)
    def list_by_owner(self, owner_kind, owner_id):
        kind = getattr(owner_kind, "value", owner_kind)
        return tuple(sorted(
            (item for item in self.records.values() if item.owner_kind.value == kind and item.owner_id == owner_id),
            key=lambda item: item.port_id,
        ))


class MemoryValueLinkRepository:
    def __init__(self): self.records = {}
    def insert(self, record):
        key = (record.source_value_id, record.target_value_id, record.link_type)
        if record.link_id in self.records or any(
            (item.source_value_id, item.target_value_id, item.link_type) == key
            for item in self.records.values()
        ):
            raise RepositoryAlreadyExistsError(str(record.link_id))
        self.records[record.link_id] = record
    def list_for_value(self, value_id, *, direction="both"):
        if direction not in {"upstream", "downstream", "both"}: raise ValueError("invalid lineage direction")
        return tuple(sorted((
            item for item in self.records.values()
            if (direction in {"upstream", "both"} and item.target_value_id == value_id)
            or (direction in {"downstream", "both"} and item.source_value_id == value_id)
        ), key=lambda item: str(item.link_id)))


class MemoryArtifactRepository:
    def __init__(self): self.records = {}
    def stage(self, record):
        if record.status is not ArtifactStatus.STAGED: raise ValueError("stage requires staged Artifact")
        if record.artifact_id in self.records: raise RepositoryAlreadyExistsError(str(record.artifact_id))
        self.records[record.artifact_id] = record
    def get(self, artifact_id, *, committed_only=False):
        item = self.records.get(artifact_id)
        return None if item is None or (committed_only and item.status is not ArtifactStatus.COMMITTED) else item
    def commit(self, record):
        prior = self.records.get(record.artifact_id)
        if prior is None or prior.status is not ArtifactStatus.STAGED or record.status is not ArtifactStatus.COMMITTED:
            raise IntegrityViolationError("Artifact is not staged")
        immutable = (
            "run_id", "workflow_id", "producer_type", "producer_id", "producer_node_run_id",
            "output_port_id", "schema_id", "content_type", "checksum", "size_bytes",
            "blob_key", "visibility", "scope_id", "created_at",
        )
        if any(getattr(prior, field) != getattr(record, field) for field in immutable):
            raise IntegrityViolationError("committed Artifact differs from staged")
        self.records[record.artifact_id] = record
    def abandon(self, artifact_id):
        prior = self.records.get(artifact_id)
        if prior is None or prior.status is not ArtifactStatus.STAGED: raise IntegrityViolationError("only staged Artifact can be abandoned")
        self.records[artifact_id] = replace(prior, status=ArtifactStatus.ABANDONED)
    def list_by_run(self, run_id, *, status=None):
        return tuple(sorted((item for item in self.records.values() if item.run_id == run_id and (status is None or item.status.value == getattr(status, "value", status))), key=lambda item: str(item.artifact_id)))
    def list_staged_before(self, before, *, limit=100):
        return tuple(sorted((item for item in self.records.values() if item.status is ArtifactStatus.STAGED and item.created_at < before), key=lambda item: (item.created_at, str(item.artifact_id))))[:limit]
    def committed_blob_keys(self): return frozenset(item.blob_key for item in self.records.values() if item.status is ArtifactStatus.COMMITTED)
    def retained_blob_keys(self): return frozenset(item.blob_key for item in self.records.values() if item.status in {ArtifactStatus.STAGED, ArtifactStatus.COMMITTED})
    def list_all(self, *, limit=1000, after_artifact_id=""):
        return tuple(sorted((item for item in self.records.values() if str(item.artifact_id) > after_artifact_id), key=lambda item: str(item.artifact_id)))[:limit]


class MemoryArtifactLinkRepository:
    def __init__(self, artifacts): self.artifacts = artifacts; self.records = {}
    def insert(self, record):
        artifact = self.artifacts.get(record.artifact_id)
        if artifact is None or artifact.workflow_id != record.workflow_id: raise IntegrityViolationError("Artifact Link crosses Workflow boundary")
        key = (record.artifact_id, record.link_type, record.target_id)
        if record.link_id in self.records or any((item.artifact_id, item.link_type, item.target_id) == key for item in self.records.values()): raise RepositoryAlreadyExistsError(str(record.link_id))
        self.records[record.link_id] = record
    def list_for_artifact(self, artifact_id, *, link_type=None):
        return tuple(sorted((item for item in self.records.values() if item.artifact_id == artifact_id and (link_type is None or item.link_type.value == getattr(link_type, "value", link_type))), key=lambda item: str(item.link_id)))
    def list_for_target(self, target_id): return tuple(sorted((item for item in self.records.values() if item.target_id == target_id), key=lambda item: str(item.link_id)))


class MemoryPlannerAttemptRepository:
    def __init__(self): self.records = {}
    def create(self, record):
        if record.attempt_id in self.records: raise RepositoryAlreadyExistsError(str(record.attempt_id))
        if any(item.run_id == record.run_id and item.attempt_number == record.attempt_number for item in self.records.values()): raise RepositoryAlreadyExistsError(str(record.attempt_id))
        self.records[record.attempt_id] = record
    def get(self, identifier): return self.records.get(identifier)
    def list_by_run(self, run_id): return tuple(sorted((item for item in self.records.values() if item.run_id == run_id), key=lambda item: (item.attempt_number.value, str(item.attempt_id))))
    def list_claimable(self, *, limit=100):
        from ..domain.planner import PlannerAttemptStatus
        return tuple(sorted((item for item in self.records.values() if item.status is PlannerAttemptStatus.REQUESTED), key=lambda item: (item.created_at, str(item.attempt_id)))[:limit])
    def list_expired(self, now, *, limit=100):
        from ..domain.planner import PlannerAttemptStatus
        return tuple(sorted((item for item in self.records.values() if item.status is PlannerAttemptStatus.RUNNING and item.lease_expires_at <= now), key=lambda item: (item.lease_expires_at, str(item.attempt_id)))[:limit])
    def list_ready_to_parse(self, *, limit=100):
        from ..domain.planner import PlannerAttemptStatus
        return tuple(sorted((item for item in self.records.values() if item.status is PlannerAttemptStatus.RESPONSE_RECEIVED), key=lambda item: (item.updated_at, str(item.attempt_id)))[:limit])
    def update(self, record, expected):
        prior = self.records.get(record.attempt_id)
        if prior is None or prior.aggregate_version != expected: raise ConcurrencyConflictError(record.attempt_id, expected.value, -1 if prior is None else prior.aggregate_version.value)
        self.records[record.attempt_id] = record


class MemoryPlannerProposalRepository:
    def __init__(self): self.records = {}
    def create(self, record):
        key = record.proposal.proposal_id
        if key in self.records or any(item.attempt_id == record.attempt_id or (item.proposal.run_id == record.proposal.run_id and item.proposal.content_hash == record.proposal.content_hash) for item in self.records.values()): raise RepositoryAlreadyExistsError(str(key))
        self.records[key] = record
    def get(self, identifier): return self.records.get(identifier)
    def list_by_run(self, run_id): return tuple(sorted((item for item in self.records.values() if item.proposal.run_id == run_id), key=lambda item: (item.created_at, str(item.proposal.proposal_id))))
    def find_by_hash(self, run_id, content_hash): return next((item for item in self.records.values() if item.proposal.run_id == run_id and item.proposal.content_hash == content_hash), None)


class MemoryRuntimeDatabase:
    """Committed state shared by transactional in-memory Units of Work."""

    def __init__(self) -> None:
        self.events = MemoryEventStore()
        self.receipts = MemoryCommandReceiptStore(self.events)
        self.runs = MemoryWorkflowRunRepository(self.events)
        self.plans = MemoryExecutionPlanRepository(self.events)
        self.node_runs = MemoryNodeRunRepository(self.events)
        self.attempts = MemoryAttemptRepository(self.events)
        self.tokens = MemoryBranchTokenRepository(self.events)
        self.joins = MemoryJoinGroupRepository(self.events)
        self.counters = MemoryControlCounterRepository(self.events)
        self.snapshots = MemorySnapshotStore()
        self.jobs = MemoryJobRepository(self.events)
        self.leases = MemoryLeaseRepository(self.events)
        self.timers = MemoryTimerRepository(self.events)
        self.values = MemoryValueRepository()
        self.value_links = MemoryValueLinkRepository()
        self.artifacts = MemoryArtifactRepository()
        self.artifact_links = MemoryArtifactLinkRepository(self.artifacts)
        self.planner_attempts = MemoryPlannerAttemptRepository()
        self.planner_proposals = MemoryPlannerProposalRepository()

    def clone(self) -> MemoryRuntimeDatabase:
        copy = MemoryRuntimeDatabase()
        copy.events._events = list(self.events._events)
        copy.receipts._receipts = dict(self.receipts._receipts)
        copy.runs.records = dict(self.runs.records)
        copy.plans.records = dict(self.plans.records)
        copy.node_runs.records = dict(self.node_runs.records)
        copy.attempts.records = dict(self.attempts.records)
        copy.tokens.records = dict(self.tokens.records)
        copy.joins.records = dict(self.joins.records)
        copy.counters.records = dict(self.counters.records)
        copy.snapshots._snapshots = list(self.snapshots._snapshots)
        copy.jobs.records = dict(self.jobs.records)
        copy.jobs.active_keys = set(self.jobs.active_keys)
        copy.leases.records = dict(self.leases.records)
        copy.timers.records = dict(self.timers.records)
        copy.timers.dedupe_keys = set(self.timers.dedupe_keys)
        copy.values.records = dict(self.values.records)
        copy.value_links.records = dict(self.value_links.records)
        copy.artifacts.records = dict(self.artifacts.records)
        copy.artifact_links.records = dict(self.artifact_links.records)
        copy.planner_attempts.records = dict(self.planner_attempts.records)
        copy.planner_proposals.records = dict(self.planner_proposals.records)
        return copy


class MemoryUnitOfWork:
    """Copy-on-write UoW used to run the same Kernel contract without SQLite."""

    _NAMES = (
        "events", "receipts", "runs", "plans", "node_runs", "attempts",
        "tokens", "snapshots", "jobs", "leases", "timers", "values",
        "value_links", "artifacts", "artifact_links", "joins", "counters",
        "planner_attempts", "planner_proposals",
    )

    def __init__(self, database: MemoryRuntimeDatabase) -> None:
        self.database = database
        self.committed = False
        self._working = None

    def __enter__(self):
        if self._working is not None:
            raise RuntimeError("UnitOfWork cannot be re-entered")
        self._working = self.database.clone()
        for name in self._NAMES:
            setattr(self, name, getattr(self._working, name))
        return self

    def commit(self) -> None:
        if self._working is None or self.committed:
            raise RuntimeError("UnitOfWork is not active or already committed")
        for name in self._NAMES:
            setattr(self.database, name, getattr(self._working, name))
        self.committed = True

    def rollback(self) -> None:
        self.committed = False

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._working = None
        for name in self._NAMES:
            setattr(self, name, None)
