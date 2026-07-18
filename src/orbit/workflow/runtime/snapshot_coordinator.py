"""Level-triggered Snapshot policy with cursor-based post-commit deduplication."""

from __future__ import annotations

from dataclasses import replace

from ..domain.ids import EntityId
from ..domain.persistence import SnapshotRecord
from ..domain.versions import DefinitionHash, Revision, SchemaVersion
from ..persistence.rehydration import rehydrate_run_view
from ..persistence.snapshots import SnapshotPolicy, snapshot_checksum
from .event_reader import runtime_event_reader
from .events import derived_id
from .reducers import reduce_run_view


SNAPSHOT_SCHEMA_VERSION = SchemaVersion("3.0")
RUNTIME_REDUCER_VERSION = SchemaVersion("3.0")


class SnapshotCoordinator:
    def __init__(
        self, uow_factory, *, policy: SnapshotPolicy | None = None,
        event_reader=None,
    ) -> None:
        self.uow_factory = uow_factory
        self.policy = policy or SnapshotPolicy()
        self.event_reader = event_reader or runtime_event_reader()

    def consider(self, run_id: EntityId) -> EntityId | None:
        with self.uow_factory() as read:
            run = read.runs.get(run_id)
            if run is None:
                raise ValueError("WorkflowRun was not found for snapshot")
            report = rehydrate_run_view(
                read.events, read.snapshots, run_id,
                {"run_status": None, "nodes": {}, "attempts": {}, "outputs": {}, "jobs": {}, "leases": {}, "timers": {}, "usage": {}},
                reduce_run_view, self.event_reader,
                snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
                reducer_version=RUNTIME_REDUCER_VERSION,
            )
            if report.final_global_position == 0 or report.event_count == 0:
                return None
            if not self.policy.should_snapshot(
                events_since_last=report.event_count,
                status=run.status.value,
            ):
                return None
            head = report.final_global_position
            state = report.state
            existing = read.snapshots.list(run_id)
            sequence = 1 if not existing else existing[-1].snapshot_sequence.value + 1
            run_sequence = read.events.stream_head(run_id)
        snapshot_id = derived_id("snapshot", run_id, head, state["run_status"])
        placeholder = SnapshotRecord(
            snapshot_id, run_id, Revision(sequence), SNAPSHOT_SCHEMA_VERSION,
            RUNTIME_REDUCER_VERSION, head, run_sequence, state,
            DefinitionHash("sha256:" + "0" * 64), run.updated_at,
        )
        snapshot = replace(placeholder, checksum=snapshot_checksum(placeholder))
        with self.uow_factory() as write:
            latest = write.snapshots.load_latest_compatible(
                run_id, snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
                reducer_version=RUNTIME_REDUCER_VERSION,
            ).snapshot
            if latest is not None and latest.last_global_position >= head:
                return None
            write.snapshots.append(snapshot)
            write.commit()
        return snapshot_id
