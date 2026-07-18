"""Run-scoped, append-only snapshots used only as replay accelerators."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import sqlite3

from ..domain.ids import EntityId
from ..domain.persistence import SnapshotCorruptionError, SnapshotRecord
from ..domain.serialization import canonical_json, definition_hash, to_primitive
from ..domain.versions import AggregateVersion, DefinitionHash, Revision, SchemaVersion


def snapshot_checksum(snapshot: SnapshotRecord) -> DefinitionHash:
    return definition_hash(
        {
            "run_id": snapshot.run_id,
            "snapshot_sequence": snapshot.snapshot_sequence,
            "snapshot_schema_version": snapshot.snapshot_schema_version,
            "reducer_version": snapshot.reducer_version,
            "last_global_position": snapshot.last_global_position,
            "last_run_event_sequence": snapshot.last_run_event_sequence,
            "state": snapshot.state,
        }
    )


@dataclass(frozen=True)
class SnapshotLoadResult:
    snapshot: SnapshotRecord | None
    ignored: tuple[str, ...] = ()


@dataclass(frozen=True)
class SnapshotPolicy:
    every_n_events: int = 100

    def __post_init__(self) -> None:
        if self.every_n_events < 1:
            raise ValueError("every_n_events must be positive")

    def should_snapshot(self, *, events_since_last: int, status: str) -> bool:
        return events_since_last >= self.every_n_events or status in {
            "waiting",
            "waiting_for_budget",
            "succeeded",
            "failed",
            "cancelled",
            "budget_exhausted",
        }


def snapshot_record_from_row(row: sqlite3.Row) -> SnapshotRecord:
    """Decode a SQLite row through the public Snapshot adapter boundary."""

    return SnapshotRecord(
        snapshot_id=EntityId.parse(row["snapshot_id"]),
        run_id=EntityId.parse(row["run_id"]),
        snapshot_sequence=Revision(row["snapshot_sequence"]),
        snapshot_schema_version=SchemaVersion(row["snapshot_schema_version"]),
        reducer_version=SchemaVersion(row["reducer_version"]),
        last_global_position=row["last_global_position"],
        last_run_event_sequence=AggregateVersion(row["last_run_event_sequence"]),
        state=json.loads(row["state_json"]),
        checksum=DefinitionHash(row["checksum"]),
        created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
    )


class SQLiteSnapshotStore:
    def __init__(self, connection: sqlite3.Connection, *, fault_hook=None) -> None:
        self.connection = connection
        self.fault_hook = fault_hook

    def _fault(self, point: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(point)

    def append(self, snapshot: SnapshotRecord) -> None:
        if not self.connection.in_transaction:
            raise RuntimeError("snapshot append requires an active UnitOfWork")
        if snapshot.checksum != snapshot_checksum(snapshot):
            raise SnapshotCorruptionError("snapshot checksum does not match state")
        row = self.connection.execute(
            "SELECT COALESCE(MAX(global_position), 0) FROM run_events WHERE run_id = ?",
            (str(snapshot.run_id),),
        ).fetchone()
        if snapshot.last_global_position > int(row[0]):
            raise SnapshotCorruptionError("snapshot cursor is beyond the event stream")
        self._fault("before_snapshot_insert")
        self.connection.execute(
            """
            INSERT INTO run_snapshots(
                snapshot_id, run_id, snapshot_sequence, snapshot_schema_version,
                reducer_version, last_global_position, last_run_event_sequence,
                state_json, checksum, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(snapshot.snapshot_id), str(snapshot.run_id),
                snapshot.snapshot_sequence.value, snapshot.snapshot_schema_version.value,
                snapshot.reducer_version.value, snapshot.last_global_position,
                snapshot.last_run_event_sequence.value, canonical_json(snapshot.state),
                snapshot.checksum.value, to_primitive(snapshot.created_at),
            ),
        )
        self._fault("after_snapshot_insert")

    def load_latest_compatible(
        self,
        run_id: EntityId,
        *,
        snapshot_schema_version: SchemaVersion,
        reducer_version: SchemaVersion,
    ) -> SnapshotLoadResult:
        rows = self.connection.execute(
            "SELECT * FROM run_snapshots WHERE run_id = ? ORDER BY snapshot_sequence DESC",
            (str(run_id),),
        ).fetchall()
        ignored: list[str] = []
        stream_max = int(
            self.connection.execute(
                "SELECT COALESCE(MAX(global_position), 0) FROM run_events WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()[0]
        )
        for row in rows:
            candidate = snapshot_record_from_row(row)
            reason = None
            if candidate.snapshot_schema_version != snapshot_schema_version:
                reason = "incompatible snapshot schema"
            elif candidate.reducer_version != reducer_version:
                reason = "incompatible reducer version"
            elif candidate.last_global_position > stream_max:
                reason = "cursor beyond event stream"
            elif candidate.checksum != snapshot_checksum(candidate):
                reason = "checksum mismatch"
            if reason is None:
                return SnapshotLoadResult(candidate, tuple(ignored))
            ignored.append(f"{candidate.snapshot_id}: {reason}")
        return SnapshotLoadResult(None, tuple(ignored))

    def list(self, run_id: EntityId) -> tuple[SnapshotRecord, ...]:
        return tuple(
            snapshot_record_from_row(row)
            for row in self.connection.execute(
                "SELECT * FROM run_snapshots WHERE run_id = ? ORDER BY snapshot_sequence",
                (str(run_id),),
            ).fetchall()
        )

    def delete(self, snapshot_id: EntityId) -> None:
        if not self.connection.in_transaction:
            raise RuntimeError("snapshot delete requires an active UnitOfWork")
        self.connection.execute(
            "DELETE FROM run_snapshots WHERE snapshot_id = ?", (str(snapshot_id),)
        )
