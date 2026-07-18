"""M3.D: `orbit run start` and `orbit run inspect`.

The CLI shares RunApplicationService with the HTTP API; these tests pin the
behaviour a user sees at the terminal — exit codes, idempotency, and the fact
that a bad workflow id is a message rather than a traceback.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.migrations import migrate_workflow_database
from tests.test_db_check_cli import run_cli
from tests.test_web_composition import publish_linear_workflow


class RunCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "runtime.db"
        connection = connect_workflow_database(self.db)
        migrate_workflow_database(connection)
        connection.close()
        publish_linear_workflow(self.db)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def start(self, *extra: str):
        return run_cli(
            "run", "start", "workflow:linear", "--db", str(self.db),
            "--input", json.dumps({"value": 1}), *extra,
        )

    def test_start_prints_the_run_id(self) -> None:
        result = self.start()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("started run:", result.stdout)

    def test_json_output_is_machine_readable(self) -> None:
        result = self.start("--json")
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("workflow:linear", payload["workflow_id"])
        self.assertEqual(1, payload["workflow_version"])
        self.assertFalse(payload["replayed"])

    def test_an_explicit_key_makes_a_rerun_idempotent(self) -> None:
        first = json.loads(self.start("--json", "--idempotency-key", "nightly").stdout)
        second = json.loads(self.start("--json", "--idempotency-key", "nightly").stdout)
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertTrue(second["replayed"])

    def test_without_a_key_each_invocation_is_a_new_run(self) -> None:
        first = json.loads(self.start("--json").stdout)
        second = json.loads(self.start("--json").stdout)
        self.assertNotEqual(first["run_id"], second["run_id"])

    def test_unknown_workflow_fails_with_a_message_not_a_traceback(self) -> None:
        result = run_cli("run", "start", "workflow:nope", "--db", str(self.db))
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("workflow version not found", result.stderr)

    def test_inspect_answers_why_the_run_is_here(self) -> None:
        run_id = json.loads(self.start("--json").stdout)["run_id"]
        result = run_cli("run", "inspect", run_id, "--db", str(self.db))
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(run_id, payload["summary"]["run_id"])
        self.assertIn("responsibilities", payload)
        self.assertIn("recent_errors", payload)

    def test_inspect_of_an_unknown_run_fails_cleanly(self) -> None:
        result = run_cli("run", "inspect", "run:missing", "--db", str(self.db))
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
