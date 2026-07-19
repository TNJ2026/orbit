"""M6: the one-time acknowledgement gate.

The property under test is narrow and important: orbit refuses to start when
pre-migration data exists, and even after the user accepts, it never opens,
copies or deletes that data.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from orbit.platform.cutover import (
    ACKNOWLEDGE_FLAG, EXIT_NEEDS_ACKNOWLEDGEMENT, CutoverRequired,
    ensure_cutover_acknowledged, marker_path, read_marker,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 19, tzinfo=timezone.utc)


class CutoverTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.state = Path(self.temp.name) / "state"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def gate(self, *, acknowledged=False):
        return ensure_cutover_acknowledged(
            acknowledged=acknowledged, project_dir=self.project,
            base_dir=self.state, now=NOW,
        )

    def plant_legacy(self, content: bytes = b"legacy sqlite bytes") -> Path:
        """Create a legacy database where the sentinel looks for one."""

        from orbit.platform.projects import project_id, project_slug

        slug = project_slug(self.project)
        digest = project_id(self.project)
        path = self.state / f"{slug}-{digest}" / "messages.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path


class FreshProjectTests(CutoverTestCase):
    def test_a_project_with_no_legacy_data_is_never_asked(self) -> None:
        self.assertIsNone(self.gate())

    def test_no_marker_is_written_when_there_was_nothing_to_acknowledge(self) -> None:
        self.gate(acknowledged=True)
        self.assertFalse(marker_path(self.project, self.state).exists())


class RefusalTests(CutoverTestCase):
    def test_legacy_data_blocks_startup(self) -> None:
        legacy = self.plant_legacy()
        with self.assertRaises(CutoverRequired) as caught:
            self.gate()
        message = str(caught.exception)
        self.assertIn(str(legacy), message)
        self.assertIn(ACKNOWLEDGE_FLAG, message)

    def test_the_refusal_offers_no_import_path(self) -> None:
        """A half-supported import is how dual state comes back."""

        self.plant_legacy()
        with self.assertRaises(CutoverRequired) as caught:
            self.gate()
        message = str(caught.exception).lower()
        for forbidden in ("import", "migrate your", "--db", "convert", "restore"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, message)

    def test_the_refusal_carries_a_distinct_exit_code(self) -> None:
        self.plant_legacy()
        with self.assertRaises(CutoverRequired) as caught:
            self.gate()
        self.assertEqual(EXIT_NEEDS_ACKNOWLEDGEMENT, caught.exception.exit_code)

    def test_refusing_leaves_the_legacy_file_untouched(self) -> None:
        legacy = self.plant_legacy()
        before = legacy.read_bytes(), legacy.stat().st_mtime_ns
        with self.assertRaises(CutoverRequired):
            self.gate()
        self.assertTrue(legacy.exists())
        self.assertEqual(before, (legacy.read_bytes(), legacy.stat().st_mtime_ns))


class AcknowledgementTests(CutoverTestCase):
    def test_acknowledging_records_what_was_abandoned(self) -> None:
        legacy = self.plant_legacy()
        marker = self.gate(acknowledged=True)
        self.assertEqual((str(legacy),), marker.acknowledged_paths)
        self.assertEqual(NOW.isoformat(), marker.acknowledged_at)

    def test_the_marker_records_paths_not_contents(self) -> None:
        self.plant_legacy(b"secret legacy content")
        self.gate(acknowledged=True)
        written = marker_path(self.project, self.state).read_text(encoding="utf-8")
        self.assertNotIn("secret legacy content", written)
        self.assertEqual(
            {"version", "acknowledged_at", "acknowledged_paths"},
            set(json.loads(written)),
        )

    def test_the_marker_is_written_private(self) -> None:
        self.plant_legacy()
        self.gate(acknowledged=True)
        mode = marker_path(self.project, self.state).stat().st_mode
        self.assertEqual(0o600, stat.S_IMODE(mode))

    def test_acknowledging_does_not_delete_the_legacy_file(self) -> None:
        """orbit abandons the data; removing it stays the user's decision."""

        legacy = self.plant_legacy()
        self.gate(acknowledged=True)
        self.assertTrue(legacy.exists())
        self.assertEqual(b"legacy sqlite bytes", legacy.read_bytes())

    def test_a_second_start_needs_no_flag(self) -> None:
        self.plant_legacy()
        self.gate(acknowledged=True)
        self.assertIsNotNone(self.gate())

    def test_a_corrupt_marker_is_treated_as_absent(self) -> None:
        self.plant_legacy()
        self.gate(acknowledged=True)
        marker_path(self.project, self.state).write_text("{ not json", encoding="utf-8")
        self.assertIsNone(read_marker(self.project, self.state))
        with self.assertRaises(CutoverRequired):
            self.gate()

    def test_a_marker_from_a_future_version_is_not_trusted(self) -> None:
        self.plant_legacy()
        path = marker_path(self.project, self.state)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 99}), encoding="utf-8")
        self.assertIsNone(read_marker(self.project, self.state))


class EveryCliIsGatedTests(unittest.TestCase):
    """The gate has to cover every command that opens the default database.

    It used to live only in `_serve`, so `orbit workflow publish` would
    happily create a fresh runtime.db for a project whose legacy data had
    never been acknowledged — the explicit confirmation bypassed by the first
    command a user is likely to run.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "home"
        self.project = Path(self.temp.name) / "project"
        (self.project / ".git").mkdir(parents=True)
        self.plant_legacy()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def plant_legacy(self) -> Path:
        from orbit.platform.projects import project_id, project_slug

        slug = project_slug(self.project)
        digest = project_id(self.project)
        path = self.home / ".orbit" / "projects" / f"{slug}-{digest}" / "messages.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"legacy")
        return path

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        """Run from inside the project, with the planted HOME.

        The timeout is short on purpose: a gated command exits immediately, so
        if the gate ever regresses `serve` fails here in seconds instead of
        blocking the suite for two minutes while a server it should never have
        started waits for connections.
        """

        return subprocess.run(
            [sys.executable, "-m", "orbit", *args],
            capture_output=True, text=True, cwd=str(self.project), timeout=20,
            env={
                "PYTHONPATH": str(ROOT / "src"),
                "PATH": "/usr/bin:/bin",
                "HOME": str(self.home),
            },
        )

    def test_every_default_database_command_refuses(self) -> None:
        # `workflow publish` is the motivating case: it is the first command a
        # user runs, and it created runtime.db behind the gate's back.
        import json as _json

        from tests.test_cli_matrix import CATALOG, VALID_DSL

        workflow = self.project / "w.json"
        workflow.write_text(_json.dumps(VALID_DSL), encoding="utf-8")
        catalog = self.project / "c.json"
        catalog.write_text(_json.dumps(CATALOG), encoding="utf-8")

        for args in (
            ("serve", "--port", "0"),
            ("db", "check"),
            ("run", "inspect", "run:x"),
            ("run", "start", "workflow:x"),
            (
                "workflow", "publish", str(workflow),
                "--catalog", str(catalog), "--expected-version", "0",
            ),
        ):
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertEqual(
                    EXIT_NEEDS_ACKNOWLEDGEMENT, result.returncode,
                    f"{args} was not gated:\n{result.stdout}{result.stderr}",
                )
                self.assertIn("pre-migration data", result.stdout)

    def test_the_refusal_names_the_flag_that_clears_it(self) -> None:
        result = self.cli("db", "check")
        self.assertIn(ACKNOWLEDGE_FLAG, result.stdout)

    def test_an_explicit_db_is_not_gated(self) -> None:
        """`--db` is already an explicit choice of which database to use.

        The gate protects the default path, where abandoning pre-migration
        data would otherwise be silent.
        """

        elsewhere = Path(self.temp.name) / "explicit.db"
        result = self.cli("db", "check", "--db", str(elsewhere))
        self.assertNotEqual(EXIT_NEEDS_ACKNOWLEDGEMENT, result.returncode)
        self.assertIn("no database at", result.stderr)

    def test_acknowledging_once_unblocks_the_other_commands(self) -> None:
        granted = self.cli("serve", ACKNOWLEDGE_FLAG, "--help")
        self.assertEqual(0, granted.returncode, granted.stderr)

        # --help exits before the gate, so grant it through a real resolution.
        from orbit.platform.cutover import ensure_cutover_acknowledged

        ensure_cutover_acknowledged(
            acknowledged=True, project_dir=self.project,
            base_dir=self.home / ".orbit" / "projects",
        )
        result = self.cli("db", "check")
        self.assertNotEqual(EXIT_NEEDS_ACKNOWLEDGEMENT, result.returncode, result.stdout)


class ServeCliTests(unittest.TestCase):
    """The gate is reachable from the command line, and advertised in help."""

    def test_serve_help_documents_the_flag(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "orbit", "serve", "--help"],
            capture_output=True, text=True, cwd=str(ROOT),
            env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(ACKNOWLEDGE_FLAG, result.stdout)
        self.assertIn("never opens", result.stdout.replace("\n", " "))


if __name__ == "__main__":
    unittest.main()
