from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
import socket

from orbit.workflow.domain.envelopes import EventEnvelope
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.persistence import SnapshotRecord, UnsupportedEventVersionError, WorkflowRunRecord, ConcurrencyConflictError
from orbit.workflow.domain.persistence_ports import EventStorePort, SnapshotStorePort, WorkflowRunRepositoryPort
from orbit.workflow.domain.states import WorkflowRunStatus
from orbit.workflow.domain.upcasting import UpcasterRegistry, with_payload
from orbit.workflow.domain.versions import AggregateVersion, DefinitionHash, Revision, SchemaVersion
from orbit.workflow.persistence import (
    EventVersionCatalog,
    MemoryEventStore,
    MemorySnapshotStore,
    MemoryWorkflowRunRepository,
    SQLiteEventStore,
    SQLiteReadSession,
    SQLiteSnapshotStore,
    SQLiteUnitOfWork,
    SnapshotPolicy,
    UpcastingEventReader,
    check_database,
    rehydrate_aggregate,
    rehydrate_run_view,
    snapshot_checksum,
)
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.migrations import migrate_workflow_database
from orbit.workflow.testing import SideEffectDetected, side_effect_guard


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


def event(number: int, *, version: int = 1) -> EventEnvelope:
    return EventEnvelope(
        EntityId("event", f"e{number}"), "run_progressed", Revision(version),
        EntityId("run", "r1"), Revision(number), EntityId("run", "r1"),
        EntityId("command", f"c{number}"), NOW, {"value": number},
    )


def reader(target: int = 2) -> UpcastingEventReader:
    registry = UpcasterRegistry()
    registry.register(
        "run_progressed", 1,
        lambda item: with_payload(
            item, {**dict(item.payload), "format": "v2"}, version=2
        ),
    )
    registry.seal()
    return UpcastingEventReader(EventVersionCatalog({"run_progressed": target}), registry)


class AdapterContractTests(unittest.TestCase):
    def test_memory_event_and_snapshot_adapters_satisfy_ports(self) -> None:
        events = MemoryEventStore()
        snapshots = MemorySnapshotStore()
        self.assertIsInstance(events, EventStorePort)
        self.assertIsInstance(snapshots, SnapshotStorePort)
        run_id = EntityId("run", "r1")
        events.append(run_id, run_id, AggregateVersion(0), (event(1),))
        self.assertEqual(1, events.stream_head(run_id).value)
        runs = MemoryWorkflowRunRepository(events)
        record = WorkflowRunRecord(
            run_id, EntityId("workflow", "flow"), Revision(1),
            DefinitionHash("sha256:" + "a" * 64), WorkflowRunStatus.CREATED,
            AggregateVersion(0), run_id, NOW, NOW,
        )
        runs.create(record)
        self.assertIsInstance(runs, WorkflowRunRepositoryPort)
        runs.update(
            replace(record, status=WorkflowRunStatus.RUNNING, aggregate_version=AggregateVersion(1)),
            AggregateVersion(0),
        )
        with self.assertRaises(ConcurrencyConflictError):
            runs.update(record, AggregateVersion(0))

    def test_registry_is_immutable_after_bootstrap(self) -> None:
        registry = UpcasterRegistry()
        registry.seal()
        with self.assertRaises(RuntimeError):
            registry.register("run_progressed", 1, lambda value: value)

    def test_upcaster_external_call_is_detected_by_replay_guard(self) -> None:
        registry = UpcasterRegistry()

        def impure(item):
            socket.socket()
            return with_payload(item, dict(item.payload), version=2)

        registry.register("run_progressed", 1, impure)
        registry.seal()
        events = MemoryEventStore()
        run_id = EntityId("run", "r1")
        events.append(run_id, run_id, AggregateVersion(0), (event(1),))
        with self.assertRaises(SideEffectDetected), side_effect_guard():
            UpcastingEventReader(
                EventVersionCatalog({"run_progressed": 2}), registry
            ).read(events.read_run(run_id)[0])


class RehydrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.db"
        with connect_workflow_database(self.path) as connection:
            migrate_workflow_database(connection)
            connection.execute(
                "INSERT INTO workflow_definitions VALUES (?, ?, ?, ?)",
                ("workflow:flow", "Flow", "2026-07-17T00:00:00Z", "test"),
            )
            connection.execute(
                "INSERT INTO workflow_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("workflow:flow", 1, "sha256:" + "a" * 64, "1.0", "1.0", "1.0", "{}", "json", None, "sha256:" + "b" * 64, "2026-07-17T00:00:00Z", "test"),
            )
            connection.execute(
                """INSERT INTO workflow_runs (
                       run_id, workflow_id, workflow_version, definition_hash,
                       status, aggregate_version, correlation_id, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("run:r1", "workflow:flow", 1, "sha256:" + "a" * 64, "created", 0, "run:r1", "2026-07-17T00:00:00Z", "2026-07-17T00:00:00Z"),
            )
            connection.commit()
        self.run_id = EntityId("run", "r1")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _append(self) -> None:
        with SQLiteUnitOfWork(self.path) as uow:
            uow.events.append(self.run_id, self.run_id, AggregateVersion(0), (event(1), event(2)))
            uow.connection.execute(
                "UPDATE workflow_runs SET aggregate_version = 2 WHERE run_id = ?",
                (str(self.run_id),),
            )
            uow.commit()

    def test_raw_rows_remain_v1_while_replay_uses_v2(self) -> None:
        self._append()
        with SQLiteReadSession(self.path) as connection:
            store = SQLiteEventStore(connection)
            raw = store.read_stream(self.run_id)
            state = rehydrate_aggregate(
                store, self.run_id, [],
                lambda current, item: current + [dict(item.payload)], reader(),
            )
        self.assertEqual([1, 1], [item.envelope.event_version.value for item in raw])
        self.assertEqual(["v2", "v2"], [item["format"] for item in state])

    def test_future_event_version_fails_explicitly(self) -> None:
        stored = MemoryEventStore()
        stored.append(self.run_id, self.run_id, AggregateVersion(0), (event(1, version=2),))
        with self.assertRaises(UnsupportedEventVersionError):
            reader(target=1).read(stored.read_run(self.run_id)[0])

    def test_snapshot_plus_tail_equals_full_replay_and_corrupt_latest_falls_back(self) -> None:
        self._append()
        placeholder = SnapshotRecord(
            EntityId("snapshot", "s1"), self.run_id, Revision(1),
            SchemaVersion("1.0"), SchemaVersion("1.0"), 1, AggregateVersion(1),
            {"values": [1]}, DefinitionHash("sha256:" + "0" * 64), NOW,
        )
        snapshot = replace(placeholder, checksum=snapshot_checksum(placeholder))
        with SQLiteUnitOfWork(self.path) as uow:
            uow.snapshots.append(snapshot)
            uow.commit()

        def reduce(state, stored):
            return {"values": [*state["values"], stored.envelope.payload["value"]]}

        with SQLiteReadSession(self.path) as connection:
            report = rehydrate_run_view(
                SQLiteEventStore(connection),
                SQLiteSnapshotStore(connection),
                self.run_id, {"values": []}, reduce, reader(),
                snapshot_schema_version=SchemaVersion("1.0"), reducer_version=SchemaVersion("1.0"),
            )
        self.assertEqual({"values": [1, 2]}, report.state)
        self.assertEqual(EntityId("snapshot", "s1"), report.snapshot_id)
        self.assertEqual(1, report.event_count)

        with connect_workflow_database(self.path) as connection:
            connection.execute("DROP TRIGGER run_snapshots_no_update")
            connection.execute("UPDATE run_snapshots SET checksum = ?", ("sha256:" + "f" * 64,))
            connection.commit()
        with SQLiteReadSession(self.path) as connection:
            report = rehydrate_run_view(
                SQLiteEventStore(connection),
                SQLiteSnapshotStore(connection),
                self.run_id, {"values": []}, reduce, reader(),
                snapshot_schema_version=SchemaVersion("1.0"), reducer_version=SchemaVersion("1.0"),
            )
        self.assertIsNone(report.snapshot_id)
        self.assertEqual({"values": [1, 2]}, report.state)
        self.assertTrue(report.snapshot_diagnostics)

    def test_integrity_report_and_snapshot_policy(self) -> None:
        self._append()
        report = check_database(self.path)
        self.assertTrue(report.ok, report.issues)
        self.assertTrue(SnapshotPolicy(2).should_snapshot(events_since_last=2, status="running"))
        self.assertTrue(SnapshotPolicy(100).should_snapshot(events_since_last=1, status="waiting"))

    def test_snapshot_faults_before_and_after_insert_roll_back(self) -> None:
        self._append()
        placeholder = SnapshotRecord(
            EntityId("snapshot", "fault"), self.run_id, Revision(1),
            SchemaVersion("1.0"), SchemaVersion("1.0"), 2, AggregateVersion(2),
            {"values": [1, 2]}, DefinitionHash("sha256:" + "0" * 64), NOW,
        )
        snapshot = replace(placeholder, checksum=snapshot_checksum(placeholder))
        for point in ("before_snapshot_insert", "after_snapshot_insert"):
            with self.subTest(point=point), self.assertRaises(RuntimeError):
                with SQLiteUnitOfWork(
                    self.path,
                    fault_hook=lambda current, expected=point: (
                        (_ for _ in ()).throw(RuntimeError("injected"))
                        if current == expected else None
                    ),
                ) as uow:
                    uow.snapshots.append(snapshot)
                    uow.commit()
            with SQLiteReadSession(self.path) as connection:
                self.assertEqual(
                    0, connection.execute("SELECT COUNT(*) FROM run_snapshots").fetchone()[0]
                )
                self.assertEqual(
                    2, connection.execute("SELECT COUNT(*) FROM run_events").fetchone()[0]
                )

    def test_integrity_checker_locates_gap_drift_receipt_and_snapshot_damage(self) -> None:
        self._append()
        with connect_workflow_database(self.path) as connection:
            connection.execute("DROP TRIGGER run_events_no_delete")
            connection.execute("DELETE FROM run_events WHERE event_id = 'event:e1'")
            connection.execute("UPDATE workflow_runs SET aggregate_version = 1 WHERE run_id = 'run:r1'")
            connection.execute(
                "INSERT INTO command_receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("run:r1", "run:r1", "missing", "sha256:" + "a" * 64, "command:missing", 0, '["event:missing"]', NOW.isoformat()),
            )
            connection.execute(
                "INSERT INTO run_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("snapshot:bad", "run:r1", 1, "1.0", "1.0", 999, 2, '{}', "sha256:" + "f" * 64, NOW.isoformat()),
            )
            connection.commit()
        codes = {item.code for item in check_database(self.path).issues}
        self.assertTrue(
            {"EVENT_SEQUENCE_GAP", "PROJECTION_VERSION_MISMATCH", "RECEIPT_EVENT_MISSING", "SNAPSHOT_CORRUPT"} <= codes
        )


if __name__ == "__main__":
    unittest.main()
