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


class ChangeSummaryTests(GitRecoveryPointTests):
    """What a run did, as far as git can independently say (§5).

    The property that motivates the whole shape: the answer must not change
    because the Agent committed, staged or switched branch. Those are
    separate facts, and reporting one in place of another is how a run that
    committed everything comes to look like a run that changed nothing.
    """

    def test_a_modified_file_is_reported_against_the_baseline(self) -> None:
        point = self.points.create("run-1")
        (self.project / "tracked.txt").write_text("AGENT WROTE\n")

        summary = self.points.summarize(point)

        self.assertEqual(
            [("tracked.txt", "modified")],
            [(c.path, c.status) for c in summary.content],
        )
        self.assertFalse(summary.head_moved)

    def test_a_new_untracked_file_is_reported_as_added(self) -> None:
        """A plain worktree diff cannot see this, which is most of what an
        Agent creates."""

        point = self.points.create("run-1")
        (self.project / "made-by-agent.txt").write_text("new\n")

        summary = self.points.summarize(point)

        self.assertIn(
            ("made-by-agent.txt", "added"),
            [(c.path, c.status) for c in summary.content],
        )

    def test_a_deleted_file_is_reported_as_deleted(self) -> None:
        point = self.points.create("run-1")
        (self.project / "untracked.txt").unlink()

        summary = self.points.summarize(point)

        self.assertIn(
            ("untracked.txt", "deleted"),
            [(c.path, c.status) for c in summary.content],
        )

    def test_files_already_there_are_not_reported_as_this_runs_work(self) -> None:
        """The misattribution the untracked-covering baseline exists to stop."""

        point = self.points.create("run-1")

        summary = self.points.summarize(point)

        self.assertEqual((), summary.content)

    def test_committing_the_work_does_not_hide_it(self) -> None:
        """HEAD moves and the working tree goes clean; the content changed
        all the same, and a summary built on HEAD would say it did not."""

        point = self.points.create("run-1")
        (self.project / "tracked.txt").write_text("AGENT WROTE\n")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-m", "agent commit")

        summary = self.points.summarize(point)

        self.assertIn(
            ("tracked.txt", "modified"),
            [(c.path, c.status) for c in summary.content],
        )
        self.assertTrue(summary.head_moved)
        self.assertEqual("", git(self.project, "status", "--porcelain"))

    def test_switching_branch_is_reported_separately_from_content(self) -> None:
        point = self.points.create("run-1")
        git(self.project, "checkout", "-q", "-b", "agent-branch")
        (self.project / "tracked.txt").write_text("ON A BRANCH\n")

        summary = self.points.summarize(point)

        self.assertEqual("agent-branch", summary.branch_after)
        self.assertIn(
            ("tracked.txt", "modified"),
            [(c.path, c.status) for c in summary.content],
        )

    def test_staging_is_its_own_answer(self) -> None:
        point = self.points.create("run-1")
        (self.project / "tracked.txt").write_text("STAGED BY AGENT\n")
        git(self.project, "add", "tracked.txt")

        summary = self.points.summarize(point)

        self.assertIn("tracked.txt", [c.path for c in summary.staged])
        self.assertIn("tracked.txt", [c.path for c in summary.content])

    def test_the_summary_says_it_is_cumulative_and_what_it_misses(self) -> None:
        point = self.points.create("run-1")

        summary = self.points.summarize(point)

        self.assertEqual("run_cumulative", summary.scope)
        self.assertIn(
            "files git is ignoring (build output, dependencies, caches)",
            summary.uncovered,
        )

    def test_ignored_files_never_appear_in_the_summary(self) -> None:
        point = self.points.create("run-1")
        (self.project / "ignored" / "new-build-output.bin").write_text("junk\n")

        summary = self.points.summarize(point)

        self.assertEqual([], [c for c in summary.content if "ignored/" in c.path])


class RetentionTests(GitRecoveryPointTests):
    """Recovery refs live in the user's repository and pin a tree of their
    whole project, so never reclaiming them is growth in somebody else's
    .git. §6.3 puts two conditions on removing one, and both are required."""

    def test_a_live_runs_way_back_is_never_reclaimed(self) -> None:
        point = self.points.create("run-1")

        reclaimed = self.points.sweep(
            {"run-1"}, older_than_seconds=0, now=2 ** 40,
        )

        self.assertEqual((), reclaimed)
        self.assertIsNotNone(self.points.load("run-1"))

    def test_a_settled_run_inside_the_retention_period_is_kept(self) -> None:
        """A run finishing is not the moment somebody stops wanting to undo
        it — the morning after is a very common one."""

        self.points.create("run-1")

        reclaimed = self.points.sweep((), older_than_seconds=7 * 24 * 3600)

        self.assertEqual((), reclaimed)
        self.assertIsNotNone(self.points.load("run-1"))

    def test_a_settled_run_past_the_retention_period_is_reclaimed(self) -> None:
        point = self.points.create("run-1")

        reclaimed = self.points.sweep(
            (), older_than_seconds=1, now=2 ** 40,
        )

        self.assertEqual([point.ref], list(reclaimed))
        self.assertIsNone(self.points.load("run-1"))

    def test_it_reclaims_only_orbits_own_refs(self) -> None:
        self.points.create("run-1")
        before = git(self.project, "rev-parse", "HEAD")

        self.points.sweep((), older_than_seconds=1, now=2 ** 40)

        # Project history, branches and files are untouched.
        self.assertEqual(before, git(self.project, "rev-parse", "HEAD"))
        self.assertTrue((self.project / "tracked.txt").exists())
        self.assertTrue((self.project / "untracked.txt").exists())

    def test_one_runs_reclamation_leaves_anothers_alone(self) -> None:
        self.points.create("old-run")
        self.points.create("live-run")

        self.points.sweep(
            {"live-run"}, older_than_seconds=1, now=2 ** 40,
        )

        self.assertIsNone(self.points.load("old-run"))
        self.assertIsNotNone(self.points.load("live-run"))
