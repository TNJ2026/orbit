"""SQLite adapters for durable workflow definitions."""

from .migrations import migrate_workflow_database
from .workflow_versions import (
    PublishConflictError,
    SQLiteWorkflowVersionStore,
    WorkflowVersionRecord,
)
from .uow import SQLiteReadSession, SQLiteUnitOfWork
from .event_store import SQLiteEventStore
from .durable import (
    SQLiteJobRepository, SQLiteLeaseRepository, SQLiteTimerRepository,
    job_record_from_row, lease_record_from_row, timer_record_from_row,
)
from .integrity import IntegrityIssue, IntegrityReport, check_database
from .data import (
    SQLiteArtifactLinkRepository, SQLiteArtifactRepository,
    SQLiteValueLinkRepository, SQLiteValueRepository,
    artifact_from_row, artifact_link_from_row, value_link_from_row,
    value_record_from_row,
)
from .memory import (
    MemoryAttemptRepository, MemoryBranchTokenRepository, MemoryEventStore,
    MemoryCommandReceiptStore, MemoryExecutionPlanRepository, MemoryJobRepository,
    MemoryLeaseRepository, MemoryNodeRunRepository,
    MemoryRuntimeDatabase, MemorySnapshotStore, MemoryUnitOfWork,
    MemoryTimerRepository, MemoryWorkflowRunRepository,
    MemoryValueRepository, MemoryValueLinkRepository,
    MemoryArtifactRepository, MemoryArtifactLinkRepository,
)
from .receipts import SQLiteCommandReceiptStore
from .repositories import (
    SQLiteAttemptRepository,
    SQLiteBranchTokenRepository,
    SQLiteExecutionPlanRepository,
    SQLiteNodeRunRepository,
    SQLiteWorkflowRunRepository,
)
from .rehydration import ReducerRegistry, RehydrationReport, rehydrate_aggregate, rehydrate_run_view
from .snapshots import (
    SQLiteSnapshotStore,
    SnapshotLoadResult,
    SnapshotPolicy,
    snapshot_checksum,
    snapshot_record_from_row,
)
from .upcasters import EventVersionCatalog, UpcastingEventReader

__all__ = [
    "PublishConflictError",
    "SQLiteWorkflowVersionStore",
    "SQLiteReadSession",
    "SQLiteEventStore",
    "SQLiteJobRepository",
    "SQLiteLeaseRepository",
    "SQLiteTimerRepository",
    "job_record_from_row",
    "lease_record_from_row",
    "timer_record_from_row",
    "SQLiteSnapshotStore",
    "SQLiteCommandReceiptStore",
    "SQLiteAttemptRepository",
    "SQLiteBranchTokenRepository",
    "SQLiteExecutionPlanRepository",
    "SQLiteNodeRunRepository",
    "SQLiteWorkflowRunRepository",
    "SQLiteUnitOfWork",
    "SQLiteValueRepository",
    "SQLiteValueLinkRepository",
    "SQLiteArtifactRepository",
    "SQLiteArtifactLinkRepository",
    "value_record_from_row",
    "value_link_from_row",
    "artifact_from_row",
    "artifact_link_from_row",
    "SnapshotLoadResult",
    "SnapshotPolicy",
    "EventVersionCatalog",
    "UpcastingEventReader",
    "RehydrationReport",
    "ReducerRegistry",
    "rehydrate_aggregate",
    "rehydrate_run_view",
    "snapshot_checksum",
    "snapshot_record_from_row",
    "IntegrityIssue",
    "IntegrityReport",
    "check_database",
    "MemoryEventStore",
    "MemorySnapshotStore",
    "MemoryCommandReceiptStore",
    "MemoryRuntimeDatabase",
    "MemoryUnitOfWork",
    "MemoryJobRepository",
    "MemoryLeaseRepository",
    "MemoryTimerRepository",
    "MemoryWorkflowRunRepository",
    "MemoryExecutionPlanRepository",
    "MemoryNodeRunRepository",
    "MemoryAttemptRepository",
    "MemoryBranchTokenRepository",
    "MemoryValueRepository",
    "MemoryValueLinkRepository",
    "MemoryArtifactRepository",
    "MemoryArtifactLinkRepository",
    "WorkflowVersionRecord",
    "migrate_workflow_database",
]
