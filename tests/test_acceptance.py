"""What a node must show for its work (§5).

The premise these defend: a zero exit code is not evidence. An Agent that was
blocked, or decided the task was unnecessary, or explained at length why it
could not proceed, exits zero and is recorded a success — and the node after
it starts on the assumption that the work was done.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from orbit.workspace.acceptance import (
    AcceptanceUnmet, CHECKS, evaluate,
)


class AcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)

    def test_nothing_declared_passes(self) -> None:
        self.assertTrue(evaluate({}, self.project).passed)

    def test_files_exist_catches_the_agent_that_did_nothing(self) -> None:
        result = evaluate({"files_exist": ["report.md"]}, self.project)

        self.assertFalse(result.passed)
        self.assertEqual("files_exist", result.failures[0].check)
        self.assertIn("does not exist", result.failures[0].detail)

    def test_files_exist_passes_once_the_file_is_there(self) -> None:
        (self.project / "report.md").write_text("done\n")
        self.assertTrue(evaluate({"files_exist": ["report.md"]}, self.project).passed)

    def test_an_empty_file_is_not_work_done(self) -> None:
        (self.project / "report.md").write_text("")

        result = evaluate({"files_non_empty": ["report.md"]}, self.project)

        self.assertFalse(result.passed)
        self.assertIn("is empty", result.failures[0].detail)

    def test_a_directory_is_not_a_file(self) -> None:
        (self.project / "report.md").mkdir()
        result = evaluate({"files_non_empty": ["report.md"]}, self.project)
        self.assertFalse(result.passed)

    def test_json_valid_catches_a_truncated_write(self) -> None:
        (self.project / "data.json").write_text('{"half": ')

        result = evaluate({"json_valid": ["data.json"]}, self.project)

        self.assertFalse(result.passed)
        self.assertIn("not valid JSON", result.failures[0].detail)

    def test_json_valid_passes_on_real_json(self) -> None:
        (self.project / "data.json").write_text(json.dumps({"ok": True}))
        self.assertTrue(evaluate({"json_valid": ["data.json"]}, self.project).passed)

    def test_files_changed_asks_about_this_run_not_the_directory(self) -> None:
        """A file that was already correct before the run is not evidence
        that this node did anything."""

        (self.project / "notes.md").write_text("was already right\n")

        untouched = evaluate({"files_changed": ["notes.md"]}, self.project)
        touched = evaluate(
            {"files_changed": ["notes.md"]}, self.project,
            changed_paths=["notes.md"],
        )

        self.assertFalse(untouched.passed)
        self.assertIn("was not changed by this run", untouched.failures[0].detail)
        self.assertTrue(touched.passed)

    def test_every_failure_is_reported_not_just_the_first(self) -> None:
        result = evaluate(
            {"files_exist": ["a.md", "b.md"], "json_valid": ["c.json"]},
            self.project,
        )

        self.assertEqual(3, len(result.failures))
        self.assertEqual(
            {"a.md", "b.md", "c.json"}, {item.path for item in result.failures},
        )

    def test_the_result_records_what_it_looked_at(self) -> None:
        (self.project / "a.md").write_text("x\n")
        result = evaluate({"files_exist": ["a.md"]}, self.project)
        self.assertEqual(("files_exist:a.md",), result.checked)

    def test_unmet_reads_as_one_sentence(self) -> None:
        result = evaluate({"files_exist": ["gone.md"]}, self.project)
        error = AcceptanceUnmet(result.failures)
        self.assertIn("files_exist gone.md: does not exist", str(error))

    def test_there_is_no_command_check(self) -> None:
        """A workflow may select a reviewed command, never describe one. An
        acceptance that took a command would be that hole, in the middle of
        the one mode that hands an Agent the real project."""

        self.assertNotIn("run", CHECKS)
        self.assertNotIn("command", CHECKS)
        self.assertNotIn("shell", CHECKS)


if __name__ == "__main__":
    unittest.main()
