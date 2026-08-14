"""M7 gate 5: the installed CLI surface, exercised item by item.

For every command: its help, a success, a failure, and — where offered —
machine-readable JSON. Plus the negative half that matters after a cutover:
every retired command must fail as an unknown argument rather than quietly
doing something.

These run the CLI as a subprocess against a temporary database, so what is
tested is the entry point a user actually gets, not an imported function.
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
RETIRED_COMMANDS = ("start", "up", "init", "config", "runner")


def cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "orbit", *args],
        capture_output=True, text=True, cwd=str(cwd or ROOT), timeout=120,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
    )


# The DSL and catalog are the ones the DSL tests already keep valid, so this
# file exercises the CLI rather than re-deriving a workflow schema that would
# drift the moment the DSL changes.
from tests.test_workflow_dsl import VALID_DSL  # noqa: E402

WORKFLOW_ID = f"workflow:{VALID_DSL['metadata']['id']}"

CATALOG = {
    "handlers": [
        {
            "name": "collect",
            "version": "1.2.0",
            "node_kinds": ["action"],
            "inputs": {},
            "outputs": {"request": "example://request/1.0"},
            "config_schema": {"type": "object", "additionalProperties": False},
            "execution_safety": "replay_safe",
            "resource_profile": {
                "max_input_tokens": 0, "max_output_tokens": 0, "max_tool_calls": 0,
                "max_duration_seconds": 60, "max_cost_microunits": 0,
                "cost_class": "free",
            },
            "result_schema_id": "example://request/1.0",
        }
    ],
    "schemas": {"example://request/1.0": {"type": "object"}},
    "extensions": [],
}


class CliMatrixTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self.temp.name)
        self.db = self.dir / "runtime.db"
        connection = connect_workflow_database(self.db)
        migrate_workflow_database(connection)
        connection.close()

        self.workflow = self.dir / "workflow.json"
        self.workflow.write_text(json.dumps(VALID_DSL), encoding="utf-8")
        self.catalog = self.dir / "catalog.json"
        self.catalog.write_text(json.dumps(CATALOG), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def publish(self, *extra: str) -> subprocess.CompletedProcess:
        return cli(
            "workflow", "publish", str(self.workflow),
            "--catalog", str(self.catalog), "--db", str(self.db),
            "--expected-version", "0", *extra,
        )


class HelpTests(unittest.TestCase):
    def test_every_command_has_help(self) -> None:
        for args in (
            ("--help",), ("serve", "--help"), ("workflow", "--help"),
            ("workflow", "validate", "--help"), ("workflow", "publish", "--help"),
            ("run", "--help"), ("run", "list", "--help"),
            ("run", "inspect", "--help"),
        ):
            with self.subTest(args=args):
                result = cli(*args)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertTrue(result.stdout.strip())

    def test_version_prints_and_exits(self) -> None:
        result = cli("--version")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("orbit", result.stdout)

    def test_no_subcommand_is_an_error(self) -> None:
        self.assertNotEqual(0, cli().returncode)


class RetiredCommandTests(unittest.TestCase):
    """After the cutover these must fail loudly, not do something surprising."""

    def test_retired_commands_are_rejected(self) -> None:
        for command in RETIRED_COMMANDS:
            with self.subTest(command=command):
                result = cli(command)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("invalid choice", result.stderr)

    def test_the_nested_db_check_alias_is_rejected(self) -> None:
        result = cli("workflow", "db-check")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("invalid choice", result.stderr)


class WorkflowCommandTests(CliMatrixTestCase):
    def test_validate_succeeds_and_reports_json(self) -> None:
        result = cli(
            "workflow", "validate", str(self.workflow),
            "--catalog", str(self.catalog), "--json",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["valid"])
        self.assertEqual(WORKFLOW_ID, payload["workflow_id"])
        self.assertTrue(payload["definition_hash"].startswith("sha256:"))

    def test_validate_reports_diagnostics_and_exits_two(self) -> None:
        broken = self.dir / "broken.json"
        broken.write_text(json.dumps({"dsl_version": "9.0"}), encoding="utf-8")

        result = cli(
            "workflow", "validate", str(broken),
            "--catalog", str(self.catalog), "--json",
        )
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["valid"])
        self.assertTrue(payload["diagnostics"])

    def test_compile_writes_canonical_ir(self) -> None:
        output = self.dir / "ir.json"
        result = cli(
            "workflow", "compile", str(self.workflow),
            "--catalog", str(self.catalog), "--output", str(output),
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(WORKFLOW_ID, json.loads(output.read_text())["workflow_id"])

    def test_publish_succeeds_and_is_idempotent_for_identical_content(self) -> None:
        first = self.publish("--json")
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(1, json.loads(first.stdout)["version"])

        # Republishing the same definition is a no-op that returns the same
        # version, so a re-run of a deploy script is not an error.
        again = self.publish("--json")
        self.assertEqual(0, again.returncode, again.stderr)
        self.assertEqual(json.loads(first.stdout), json.loads(again.stdout))

    def test_publishing_changed_content_at_a_stale_version_conflicts(self) -> None:
        self.assertEqual(0, self.publish().returncode)

        changed = dict(VALID_DSL)
        changed["metadata"] = {**VALID_DSL["metadata"], "name": "Renamed"}
        self.workflow.write_text(json.dumps(changed), encoding="utf-8")

        result = self.publish("--json")
        self.assertEqual(3, result.returncode, result.stdout + result.stderr)
        self.assertIn("CONFLICT", result.stdout.upper())


class JsonOutputTests(CliMatrixTestCase):
    def test_machine_output_is_a_single_parseable_object(self) -> None:
        """Anything with --json must be pipeable into jq without filtering."""

        self.publish()
        for args in (
            ("workflow", "validate", str(self.workflow), "--catalog", str(self.catalog)),
        ):
            with self.subTest(args=args[0:2]):
                result = cli(*args, "--json")
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIsInstance(json.loads(result.stdout), dict)


if __name__ == "__main__":
    unittest.main()


class RunCommandTests(CliMatrixTestCase):
    """`orbit run` reads what a server wrote, without being one."""

    def seed(self):
        from tests.test_web_composition import (
            publish_human_workflow, publish_linear_workflow,
            transform_registration,
        )
        from orbit.workflow.langgraph_runtime import build_service

        publish_linear_workflow(self.db)
        publish_human_workflow(self.db)
        # Wired the way `orbit serve` wires it: state beside the database, so
        # the CLI and the server describe one engine rather than two.
        engine = build_service(
            self.db, [transform_registration()], state_directory=self.db.parent,
        )
        engine.start(
            "workflow:linear", {"value": 1}, idempotency_key="done",
            actor="local", goal="Summarise the quarter",
        )
        return engine.start(
            "workflow:human", {"value": 1}, idempotency_key="live",
            actor="local", goal="Review the draft",
        )

    def test_list_names_runs_by_the_goal_they_were_given(self) -> None:
        self.seed()
        result = cli("run", "list", "--db", str(self.db))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Summarise the quarter", result.stdout)
        self.assertIn("Review the draft", result.stdout)
        self.assertIn("interrupted", result.stdout)

    def test_inspect_shows_how_far_through_its_steps_a_run_got(self) -> None:
        waiting = self.seed()
        result = cli("run", "inspect", waiting.run_id, "--db", str(self.db))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("waiting", result.stdout)
        self.assertIn("not_reached", result.stdout)
        self.assertIn("succeeded", result.stdout)

    def test_machine_output_is_one_object_per_command(self) -> None:
        waiting = self.seed()
        listed = cli("run", "list", "--db", str(self.db), "--json")
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertIsInstance(json.loads(listed.stdout), list)
        inspected = cli(
            "run", "inspect", waiting.run_id, "--db", str(self.db), "--json",
        )
        payload = json.loads(inspected.stdout)
        self.assertEqual("interrupted", payload["status"])
        self.assertEqual("Review the draft", payload["goal"])
        self.assertTrue(payload["steps"])

    def test_an_unknown_run_is_refused_rather_than_traced(self) -> None:
        self.seed()
        result = cli(
            "run", "inspect", "langgraph_run:nope", "--db", str(self.db),
        )
        self.assertEqual(1, result.returncode)
        self.assertIn("orbit run:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_an_empty_runtime_says_so(self) -> None:
        result = cli("run", "list", "--db", str(self.db))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("no runs", result.stdout)

    def test_there_is_no_start_here(self) -> None:
        """A run executes inside the process that starts it.

        A CLI that started one would rebuild the whole Handler wiring a server
        has — discovery, workspaces, secrets — and still behave differently
        from the server that normally runs them. Starting is the UI's, or
        `start_run` over `orbit mcp`.
        """

        result = cli("run", "start", "workflow:linear", "--db", str(self.db))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("invalid choice", result.stderr)
