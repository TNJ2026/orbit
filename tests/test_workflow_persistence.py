from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.persistence import (
    BranchTokenRecord,
    CommandReceipt,
    DatabaseBusyError,
    PersistenceError,
    SnapshotRecord,
    WorkflowRunRecord,
)
from orbit.workflow.domain.states import BranchTokenStatus, WorkflowRunStatus
from orbit.workflow.domain.versions import AggregateVersion, DefinitionHash, Revision, SchemaVersion
from orbit.workflow.persistence import SQLiteReadSession, SQLiteUnitOfWork
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.migrations import migrate_workflow_database


HASH = DefinitionHash("sha256:" + "a" * 64)
NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


class WorkflowPersistenceRecordTests(unittest.TestCase):
    def test_runtime_records_validate_identity_and_freeze_json(self) -> None:
        run = WorkflowRunRecord(
            EntityId("run", "r1"),
            EntityId("workflow", "flow"),
            Revision(1),
            HASH,
            WorkflowRunStatus.CREATED,
            AggregateVersion(0),
            EntityId("run", "r1"),
            NOW,
            NOW,
        )
        self.assertEqual("run:r1", str(run.run_id))
        token = BranchTokenRecord(
            EntityId("branch_token", "t1"),
            run.run_id,
            None,
            BranchTokenStatus.ACTIVE,
            AggregateVersion(0),
            {"items": [1]},
            NOW,
            NOW,
        )
        with self.assertRaises(TypeError):
            token.scope["changed"] = True

    def test_snapshot_and_receipt_require_complete_committed_identity(self) -> None:
        snapshot = SnapshotRecord(
            EntityId("snapshot", "s1"), EntityId("run", "r1"), Revision(1),
            SchemaVersion("1.0"), SchemaVersion("1.0"), 0, AggregateVersion(0),
            {"status": "created"}, HASH, NOW,
        )
        self.assertEqual(0, snapshot.last_global_position)
        receipt = CommandReceipt(
            EntityId("run", "r1"), EntityId("run", "r1"), "start-r1", HASH,
            EntityId("command", "c1"), AggregateVersion(0),
            (EntityId("event", "e1"),), NOW,
        )
        self.assertEqual("event:e1", str(receipt.result_event_ids[0]))


class WorkflowMigrationAndUnitOfWorkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "runtime.db"
        with connect_workflow_database(self.path) as connection:
            migrate_workflow_database(connection)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_migration_three_adds_only_durable_execution_tables(self) -> None:
        with connect_workflow_database(self.path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            versions = [
                row[0]
                for row in connection.execute(
                    "SELECT version FROM workflow_schema_migrations ORDER BY version"
                )
            ]
        expected = {
            "workflow_runs", "execution_plans", "node_runs", "node_attempts",
            "run_events", "run_snapshots", "branch_tokens", "command_receipts",
        }
        self.assertTrue(expected <= tables)
        self.assertTrue({"jobs", "job_leases", "durable_timers"} <= tables)
        self.assertTrue({"planner_attempts", "planner_proposals"} <= tables)
        self.assertIn("human_tasks", tables)
        self.assertEqual(list(range(1, 17)), versions)

    def test_migration_is_repeatable_after_workflow_drafts_exist(self) -> None:
        with connect_workflow_database(self.path) as connection:
            connection.execute(
                "INSERT INTO workflow_definitions VALUES (?, ?, ?, ?)",
                ("workflow:repeat", "Repeat", "2026-07-20T00:00:00Z", "test"),
            )
            migrate_workflow_database(connection)
            migrate_workflow_database(connection)
            draft_table = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='workflow_drafts'"
            ).fetchone()[0]
            definition = connection.execute(
                "SELECT name FROM workflow_definitions WHERE workflow_id=?",
                ("workflow:repeat",),
            ).fetchone()[0]
        self.assertEqual(1, draft_table)
        self.assertEqual("Repeat", definition)

    def test_unit_of_work_requires_explicit_commit(self) -> None:
        with SQLiteUnitOfWork(self.path) as uow:
            uow.connection.execute(
                "INSERT INTO workflow_definitions VALUES (?, ?, ?, ?)",
                ("workflow:rollback", "Rollback", "2026-07-17T00:00:00Z", "test"),
            )
        with connect_workflow_database(self.path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM workflow_definitions WHERE workflow_id = 'workflow:rollback'"
            ).fetchone()[0]
        self.assertEqual(0, count)

        with SQLiteUnitOfWork(self.path) as uow:
            uow.connection.execute(
                "INSERT INTO workflow_definitions VALUES (?, ?, ?, ?)",
                ("workflow:commit", "Commit", "2026-07-17T00:00:00Z", "test"),
            )
            uow.commit()
        with SQLiteReadSession(self.path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM workflow_definitions WHERE workflow_id = 'workflow:commit'"
            ).fetchone()[0]
        self.assertEqual(1, count)

    def test_fault_before_commit_rolls_back_all_writes(self) -> None:
        def fail(point: str) -> None:
            if point == "before_commit":
                raise RuntimeError("injected")

        with self.assertRaises(RuntimeError):
            with SQLiteUnitOfWork(self.path, fault_hook=fail) as uow:
                uow.connection.execute(
                    "INSERT INTO workflow_definitions VALUES (?, ?, ?, ?)",
                    ("workflow:fault", "Fault", "2026-07-17T00:00:00Z", "test"),
                )
                uow.commit()
        with connect_workflow_database(self.path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM workflow_definitions WHERE workflow_id = 'workflow:fault'"
            ).fetchone()[0]
        self.assertEqual(0, count)

    def test_read_session_rejects_writes(self) -> None:
        with SQLiteReadSession(self.path) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute(
                    "INSERT INTO workflow_definitions VALUES ('workflow:no', 'No', 'now', 'test')"
                )

    def test_adapter_operational_errors_are_translated(self) -> None:
        for message, error in (
            ("database is locked", DatabaseBusyError),
            ("disk I/O error", PersistenceError),
        ):
            with self.subTest(message=message), self.assertRaises(error):
                with SQLiteUnitOfWork(
                    self.path,
                    fault_hook=lambda point, text=message: (
                        (_ for _ in ()).throw(sqlite3.OperationalError(text))
                        if point == "before_begin" else None
                    ),
                ):
                    pass


if __name__ == "__main__":
    unittest.main()
