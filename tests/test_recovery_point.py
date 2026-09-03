"""A way back, taken before an Agent writes the real project.

Design: docs/project-file-access-design.md §6. The properties that matter:
what the baseline covers (untracked included, ignored excluded), that it does
not disturb the user's index, that it survives `git gc`, and that coming back
is conservative — covered paths restored, new files kept, type conflicts
stopping the restore rather than being cleared recursively.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from orbit.workspace.recovery import (
    GitRecoveryPoints, RecoveryPointError, RecoveryUnavailable,
)


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True,
    ).stdout.strip()


class GitRecoveryPointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        git(self.project, "init", "--initial-branch=main")
        git(self.project, "config", "user.email", "t@e.com")
        git(self.project, "config", "user.name", "T")
        (self.project / "tracked.txt").write_text("committed\n")
        (self.project / ".gitignore").write_text("ignored/\n")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-m", "init")
        # The three interesting kinds of "not in the last commit".
        (self.project / "tracked.txt").write_text("committed\nUNCOMMITTED\n")
        (self.project / "untracked.txt").write_text("untracked\n")
        (self.project / "ignored").mkdir()
        (self.project / "ignored" / "big.bin").write_text("dependency\n")
        self.points = GitRecoveryPoints(self.project)

    def covered_paths(self, point):
        listing = git(
            self.project, "ls-tree", "-r", "--name-only", point.worktree_tree,
        )
        return set(listing.splitlines())

    def test_the_baseline_covers_uncommitted_content(self) -> None:
        point = self.points.create("run-1")
        blob = git(
            self.project, "cat-file", "blob",
            f"{point.worktree_tree}:tracked.txt",
        )
        self.assertEqual("committed\nUNCOMMITTED", blob)

    def test_the_baseline_covers_untracked_files(self) -> None:
        """Without this, every untracked file already there is reported as
        something the run added — §5's misattribution, one axis over."""

        point = self.points.create("run-1")
        self.assertIn("untracked.txt", self.covered_paths(point))

    def test_the_baseline_excludes_ignored_files(self) -> None:
        point = self.points.create("run-1")
        self.assertNotIn("ignored/big.bin", self.covered_paths(point))
        self.assertIn(
            "files git is ignoring (build output, dependencies, caches)",
            point.uncovered,
        )

    def test_it_does_not_disturb_the_users_index(self) -> None:
        git(self.project, "add", "untracked.txt")
        before = git(self.project, "status", "--porcelain")

        self.points.create("run-1")

        self.assertEqual(before, git(self.project, "status", "--porcelain"))

    def test_the_baseline_survives_garbage_collection(self) -> None:
        """An unreferenced tree is one `git gc` away from gone, and a
        recovery point that evaporates is worse than none — it was counted
        on."""

        point = self.points.create("run-1")
        git(self.project, "gc", "--prune=now", "--aggressive")

        self.assertEqual(
            "committed\nUNCOMMITTED",
            git(self.project, "cat-file", "blob", f"{point.worktree_tree}:tracked.txt"),
        )

    def test_a_non_git_project_is_refused_rather_than_left_unprotected(self) -> None:
        plain = Path(self.temp.name) / "plain"
        plain.mkdir()
        with self.assertRaises(RecoveryUnavailable):
            GitRecoveryPoints(plain).create("run-1")

    def test_too_little_disk_refuses_rather_than_running_unprotected(self) -> None:
        points = GitRecoveryPoints(self.project, min_free_bytes=10**18)
        with self.assertRaises(RecoveryUnavailable) as caught:
            points.create("run-1")
        self.assertIn("refusing rather than running unprotected", str(caught.exception))


class RestoreTests(GitRecoveryPointTests):
    def test_the_plan_says_what_it_would_restore_and_keep(self) -> None:
        point = self.points.create("run-1")
        (self.project / "tracked.txt").write_text("AGENT REWROTE THIS\n")
        (self.project / "new-from-agent.txt").write_text("new\n")

        plan = self.points.plan_restore(point)

        self.assertIn("tracked.txt", [item.path for item in plan.restores])
        self.assertIn("new-from-agent.txt", [item.path for item in plan.keeps])
        self.assertFalse(plan.blocked)

    def test_restoring_puts_covered_content_back(self) -> None:
        point = self.points.create("run-1")
        (self.project / "tracked.txt").write_text("AGENT REWROTE THIS\n")

        restored = self.points.restore(point, self.points.plan_restore(point))

        self.assertIn("tracked.txt", restored)
        self.assertEqual(
            "committed\nUNCOMMITTED\n", (self.project / "tracked.txt").read_text(),
        )

    def test_restoring_keeps_what_the_run_added(self) -> None:
        """§6.3: no pre-run inventory means a restore adds back what it
        covered and deletes nothing — the directory afterwards is not the
        directory before."""

        point = self.points.create("run-1")
        (self.project / "new-from-agent.txt").write_text("new\n")

        self.points.restore(point, self.points.plan_restore(point))

        self.assertTrue((self.project / "new-from-agent.txt").exists())

    def test_a_deleted_file_comes_back(self) -> None:
        point = self.points.create("run-1")
        (self.project / "untracked.txt").unlink()

        self.points.restore(point, self.points.plan_restore(point))

        self.assertEqual("untracked\n", (self.project / "untracked.txt").read_text())

    def test_a_type_conflict_blocks_the_restore(self) -> None:
        """A directory standing where a file was is not resolved by deleting
        it recursively; §6.3 says stop and ask."""

        point = self.points.create("run-1")
        (self.project / "tracked.txt").unlink()
        (self.project / "tracked.txt").mkdir()
        (self.project / "tracked.txt" / "surprise.txt").write_text("!\n")

        plan = self.points.plan_restore(point)

        self.assertTrue(plan.blocked)
        self.assertEqual(["tracked.txt"], [item.path for item in plan.conflicts])
        with self.assertRaises(RecoveryPointError):
            self.points.restore(point, plan)
        self.assertTrue((self.project / "tracked.txt" / "surprise.txt").exists())

    def test_forgetting_drops_the_ref(self) -> None:
        point = self.points.create("run-1")
        self.points.forget(point)
        result = subprocess.run(
            ["git", "-C", str(self.project), "rev-parse", "--verify", "-q", point.ref],
            capture_output=True, text=True,
        )
        self.assertNotEqual(0, result.returncode)


if __name__ == "__main__":
    unittest.main()
