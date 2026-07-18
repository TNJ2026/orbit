from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from orbit.__main__ import main
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.migrations import migrate_workflow_database


class WorkflowDatabaseCheckCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "check.db"
        with connect_workflow_database(self.path) as connection:
            migrate_workflow_database(connection)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def invoke(self):
        output = StringIO()
        with patch.object(
            sys, "argv", ["orbit", "workflow", "db-check", "--db", str(self.path), "--json"]
        ), redirect_stdout(output):
            main()
        return json.loads(output.getvalue())

    def test_healthy_database_returns_stable_machine_report(self) -> None:
        result = self.invoke()
        self.assertTrue(result["ok"])
        self.assertEqual(list(range(1, 10)), result["migration_versions"])
        self.assertIn("run_events", result["table_counts"])
        self.assertIn("run_events_by_run_position", result["indexes"])

    def test_corruption_returns_nonzero_without_mutating_database(self) -> None:
        with connect_workflow_database(self.path) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                "INSERT INTO workflow_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("run:bad", "workflow:missing", 1, "sha256:" + "a" * 64, "created", 0, "run:bad", "2026-07-17T00:00:00Z", "2026-07-17T00:00:00Z"),
            )
            connection.commit()
        output = StringIO()
        with patch.object(
            sys, "argv", ["orbit", "workflow", "db-check", "--db", str(self.path), "--json"]
        ), redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            main()
        self.assertEqual(4, raised.exception.code)
        result = json.loads(output.getvalue())
        self.assertFalse(result["ok"])
        self.assertTrue(any(item["code"] == "FOREIGN_KEY" for item in result["issues"]))
        with connect_workflow_database(self.path) as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0])

    def test_invalid_snapshot_is_deleted_only_with_explicit_flag(self) -> None:
        with connect_workflow_database(self.path) as connection:
            connection.execute(
                "INSERT INTO workflow_definitions VALUES ('workflow:flow', 'Flow', '2026-07-17T00:00:00Z', 'test')"
            )
            connection.execute(
                "INSERT INTO workflow_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("workflow:flow", 1, "sha256:" + "a" * 64, "1.0", "1.0", "1.0", "{}", "json", None, "sha256:" + "b" * 64, "2026-07-17T00:00:00Z", "test"),
            )
            connection.execute(
                "INSERT INTO workflow_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("run:r1", "workflow:flow", 1, "sha256:" + "a" * 64, "created", 0, "run:r1", "2026-07-17T00:00:00Z", "2026-07-17T00:00:00Z"),
            )
            connection.execute(
                "INSERT INTO run_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("snapshot:bad", "run:r1", 1, "1.0", "1.0", 0, 0, "{}", "sha256:" + "f" * 64, "2026-07-17T00:00:00Z"),
            )
            connection.commit()
        output = StringIO()
        with patch.object(
            sys, "argv", ["orbit", "workflow", "db-check", "--db", str(self.path), "--json", "--drop-invalid-snapshots"]
        ), redirect_stdout(output):
            main()
        result = json.loads(output.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual(["snapshot:bad"], result["dropped_invalid_snapshots"])
        with connect_workflow_database(self.path) as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM run_snapshots").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
