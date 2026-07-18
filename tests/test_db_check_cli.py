"""M1A: the top-level `orbit db check` command.

The nested `orbit workflow db-check` stays as a hidden alias until M6, so both
spellings are exercised here to prove they run the same implementation.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.migrations import migrate_workflow_database


ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "orbit", *args],
        capture_output=True, text=True, cwd=str(cwd or ROOT),
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
    )


class DbCheckCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "runtime.db"
        connection = connect_workflow_database(self.db)
        migrate_workflow_database(connection)
        connection.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_healthy_database_exits_zero_with_human_output(self) -> None:
        result = run_cli("db", "check", "--db", str(self.db))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("ok:", result.stdout)

    def test_json_output_is_machine_readable(self) -> None:
        result = run_cli("db", "check", "--db", str(self.db), "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertIn("migration_versions", payload)
        self.assertIn("table_counts", payload)

    def test_top_level_and_legacy_alias_agree(self) -> None:
        top = run_cli("db", "check", "--db", str(self.db), "--json")
        alias = run_cli("workflow", "db-check", "--db", str(self.db), "--json")
        self.assertEqual(top.returncode, alias.returncode)
        self.assertEqual(json.loads(top.stdout), json.loads(alias.stdout))

    def test_help_advertises_db_check_but_not_the_alias(self) -> None:
        top = run_cli("--help")
        self.assertEqual(0, top.returncode, top.stderr)
        self.assertIn("db", top.stdout)

        workflow = run_cli("workflow", "--help")
        self.assertEqual(0, workflow.returncode, workflow.stderr)
        # The nested spelling still works but must no longer be advertised.
        self.assertNotIn("db-check", workflow.stdout)

    def test_corrupt_database_exits_four(self) -> None:
        connection = connect_workflow_database(self.db)
        connection.execute(
            "INSERT INTO workflow_definitions(workflow_id, name, created_at, created_by)"
            " VALUES ('workflow:x', 'x', '2026-07-18T00:00:00Z', 'test')"
        )
        connection.execute(
            "INSERT INTO workflow_versions(workflow_id, version, definition_hash,"
            " dsl_version, ir_version, compiler_version, canonical_ir_json,"
            " source_format, source_text, catalog_fingerprint, created_at, created_by)"
            " VALUES ('workflow:x', 1, 'sha256:" + "a" * 64 + "', '1.0', '1.0', '1.0',"
            " '{}', 'json', NULL, 'sha256:" + "b" * 64 + "',"
            " '2026-07-18T00:00:00Z', 'test')"
        )
        connection.execute(
            "INSERT INTO workflow_runs(run_id, workflow_id, workflow_version,"
            " definition_hash, status, aggregate_version, correlation_id,"
            " created_at, updated_at)"
            " VALUES ('run:x', 'workflow:x', 1, 'sha256:" + "c" * 64 + "',"
            " 'running', 5, 'run:x', '2026-07-18T00:00:00Z', '2026-07-18T00:00:00Z')"
        )
        connection.commit()
        connection.close()

        result = run_cli("db", "check", "--db", str(self.db))
        self.assertEqual(4, result.returncode, result.stdout + result.stderr)
        self.assertTrue(result.stdout.strip(), "failures must be reported")

    def test_missing_database_fails_cleanly(self) -> None:
        missing = Path(self.temp.name) / "absent.db"
        result = run_cli("db", "check", "--db", str(missing))
        self.assertNotEqual(0, result.returncode)


if __name__ == "__main__":
    unittest.main()
