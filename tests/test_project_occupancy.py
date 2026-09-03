"""One run per project directory, machine-wide.

The Gate these tests defend: when `workspace_access` hands a run the real
project directory, no second run may be writing the same files, and a run
whose Runtime died does not quietly hand the project to the next comer — its
Agent subprocesses may still be writing. Design:
docs/project-file-access-design.md §4.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from orbit.platform.project_occupancy import (
    Occupancy, ProjectBusy, ProjectClaim, ProjectIdentity, ProjectNeedsRecovery,
    ProjectOccupancyRegistry, REGISTRY_LOCK_NAME,
)


class ProjectIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_a_symlink_is_the_same_project_as_its_target(self) -> None:
        project = self.root / "project"
        project.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(project)

        self.assertTrue(
            ProjectIdentity.of(project).overlaps(ProjectIdentity.of(alias))
        )

    def test_a_parent_and_a_child_overlap(self) -> None:
        """Different inodes, one set of files: `/p` and `/p/sub` cannot both
        be held while a run writes through either."""

        project = self.root / "project"
        (project / "sub").mkdir(parents=True)

        parent = ProjectIdentity.of(project)
        child = ProjectIdentity.of(project / "sub")

        self.assertNotEqual((parent.device, parent.inode), (child.device, child.inode))
        self.assertTrue(parent.overlaps(child))
        self.assertTrue(child.overlaps(parent))

    def test_siblings_do_not_overlap(self) -> None:
        (self.root / "a").mkdir()
        (self.root / "b").mkdir()

        self.assertFalse(
            ProjectIdentity.of(self.root / "a").overlaps(
                ProjectIdentity.of(self.root / "b")
            )
        )

    def test_a_missing_directory_is_refused(self) -> None:
        with self.assertRaises(Exception):
            ProjectIdentity.of(self.root / "nope")

    def test_a_file_is_not_a_project(self) -> None:
        target = self.root / "file.txt"
        target.write_text("x\n")
        with self.assertRaises(Exception):
            ProjectIdentity.of(target)


class ClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        (self.project / "sub").mkdir(parents=True)
        self.registry = ProjectOccupancyRegistry(self.root / "occupancy")

    def test_a_project_admits_one_run(self) -> None:
        first = self.registry.claim(self.project, run_id="run-1")
        self.addCleanup(first.release)

        with self.assertRaises(ProjectBusy) as caught:
            self.registry.claim(self.project, run_id="run-2")
        self.assertIn("run-1", str(caught.exception))

    def test_releasing_lets_the_next_run_in(self) -> None:
        first = self.registry.claim(self.project, run_id="run-1")
        first.release()

        second = self.registry.claim(self.project, run_id="run-2")
        self.addCleanup(second.release)
        self.assertEqual("run-2", second.run_id)

    def test_a_child_directory_is_refused_while_the_parent_is_held(self) -> None:
        held = self.registry.claim(self.project, run_id="run-1")
        self.addCleanup(held.release)

        with self.assertRaises(ProjectBusy):
            self.registry.claim(self.project / "sub", run_id="run-2")

    def test_a_parent_directory_is_refused_while_the_child_is_held(self) -> None:
        held = self.registry.claim(self.project / "sub", run_id="run-1")
        self.addCleanup(held.release)

        with self.assertRaises(ProjectBusy):
            self.registry.claim(self.project, run_id="run-2")

    def test_a_path_alias_is_refused_like_the_path_itself(self) -> None:
        alias = self.root / "alias"
        alias.symlink_to(self.project)
        held = self.registry.claim(self.project, run_id="run-1")
        self.addCleanup(held.release)

        with self.assertRaises(ProjectBusy):
            self.registry.claim(alias, run_id="run-2")

    def test_unrelated_projects_are_held_at_the_same_time(self) -> None:
        other = self.root / "other"
        other.mkdir()

        first = self.registry.claim(self.project, run_id="run-1")
        self.addCleanup(first.release)
        second = self.registry.claim(other, run_id="run-2")
        self.addCleanup(second.release)

        self.assertEqual(
            {"run-1", "run-2"},
            {item.run_id for item in self.registry.occupancies()},
        )

    def test_the_claim_is_released_by_its_context_manager(self) -> None:
        with self.registry.claim(self.project, run_id="run-1"):
            self.assertTrue(self.registry.occupancies())
        self.assertEqual((), self.registry.occupancies())

    def test_blocked_by_names_the_holder(self) -> None:
        held = self.registry.claim(self.project, run_id="run-1")
        self.addCleanup(held.release)

        blockers = self.registry.blocked_by(self.project / "sub")

        self.assertEqual(["run-1"], [item.run_id for item in blockers])


class AbandonedClaimTests(unittest.TestCase):
    """A claim whose Runtime died blocks until somebody resolves it.

    The kernel drops the lock when the process goes, but the Agent
    subprocesses it started can outlive it. So the record is what blocks, and
    clearing it is an explicit act — never something the next `claim()` does
    on its own.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.occupancy_root = self.root / "occupancy"
        self.registry = ProjectOccupancyRegistry(self.occupancy_root)

    def abandon(self, run_id: str) -> None:
        """Leave the record a crashed Runtime would leave: no lock, no owner."""

        identity = ProjectIdentity.of(self.project)
        occupancy = Occupancy(run_id, identity, 999999, "2026-01-01T00:00:00Z")
        self.occupancy_root.mkdir(parents=True, exist_ok=True)
        path = self.occupancy_root / f"{identity.key}.abandoned.json"
        path.write_text(json.dumps(occupancy.to_primitive()), encoding="utf-8")

    def test_an_abandoned_claim_blocks_the_next_run(self) -> None:
        self.abandon("dead-run")

        with self.assertRaises(ProjectNeedsRecovery) as caught:
            self.registry.claim(self.project, run_id="run-2")
        self.assertIn("dead-run", str(caught.exception))

    def test_it_is_not_reported_as_merely_busy(self) -> None:
        """`ProjectBusy` would mean "wait"; nobody is coming back."""

        self.abandon("dead-run")
        with self.assertRaises(ProjectNeedsRecovery):
            self.registry.claim(self.project, run_id="run-2")

    def test_resolving_it_is_explicit_and_then_the_project_is_free(self) -> None:
        self.abandon("dead-run")

        cleared = self.registry.resolve("dead-run")

        self.assertEqual(("dead-run",), cleared)
        claim = self.registry.claim(self.project, run_id="run-2")
        self.addCleanup(claim.release)

    def test_a_live_claim_is_never_reported_as_abandoned(self) -> None:
        held = self.registry.claim(self.project, run_id="run-1")
        self.addCleanup(held.release)

        self.assertTrue(self.registry.holder_is_live(held.occupancy))
        with self.assertRaises(ProjectBusy):
            self.registry.claim(self.project, run_id="run-2")


CHILD = """
import sys
from orbit.platform.project_occupancy import (
    ProjectBusy, ProjectNeedsRecovery, ProjectOccupancyRegistry,
)

registry = ProjectOccupancyRegistry(sys.argv[1])
try:
    claim = registry.claim(sys.argv[2], run_id=sys.argv[3])
except ProjectBusy as exc:
    print("BUSY")
except ProjectNeedsRecovery as exc:
    print("NEEDS_RECOVERY")
else:
    print("CLAIMED")
    if len(sys.argv) > 4 and sys.argv[4] == "hold":
        # Hold it and die without releasing, the way a killed Runtime does.
        sys.stdout.flush()
        import time
        time.sleep(30)
"""


class CrossProcessTests(unittest.TestCase):
    """Real processes, because an in-process lock proves nothing here.

    §4 says so in as many words, and this project has already shipped a
    `threading.Lock` that looked right in a thread test and synchronised
    nothing across the worker-process boundary it actually had to cross.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        (self.project / "sub").mkdir(parents=True)
        self.occupancy_root = self.root / "occupancy"
        self.registry = ProjectOccupancyRegistry(self.occupancy_root)
        self.env = {
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src"),
        }

    def child(self, path, run_id, *extra):
        return subprocess.run(
            [sys.executable, "-c", CHILD, str(self.occupancy_root), str(path),
             run_id, *extra],
            capture_output=True, text=True, env=self.env, timeout=60,
        )

    def test_another_process_is_refused_while_this_one_holds(self) -> None:
        held = self.registry.claim(self.project, run_id="run-1")
        self.addCleanup(held.release)

        result = self.child(self.project, "run-2")

        self.assertEqual("BUSY", result.stdout.strip(), result.stderr[-800:])

    def test_another_process_is_refused_for_an_overlapping_directory(self) -> None:
        held = self.registry.claim(self.project, run_id="run-1")
        self.addCleanup(held.release)

        result = self.child(self.project / "sub", "run-2")

        self.assertEqual("BUSY", result.stdout.strip(), result.stderr[-800:])

    def test_a_killed_holder_leaves_a_claim_that_needs_recovery(self) -> None:
        """The lock goes with the process; the record does not, and must not."""

        holder = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(self.occupancy_root),
             str(self.project), "killed-run", "hold"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=self.env,
        )
        self.addCleanup(holder.kill)
        self.assertEqual("CLAIMED", holder.stdout.readline().strip())
        holder.kill()
        holder.wait(timeout=30)

        with self.assertRaises(ProjectNeedsRecovery) as caught:
            self.registry.claim(self.project, run_id="next-run")
        self.assertIn("killed-run", str(caught.exception))

        # And only an explicit resolution opens it again.
        self.registry.resolve("killed-run")
        claim = self.registry.claim(self.project, run_id="next-run")
        self.addCleanup(claim.release)

    def test_two_processes_racing_produce_exactly_one_winner(self) -> None:
        children = [
            subprocess.Popen(
                [sys.executable, "-c", CHILD, str(self.occupancy_root),
                 str(self.project), f"racer-{index}", "hold"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=self.env,
            )
            for index in range(6)
        ]
        for child in children:
            self.addCleanup(child.kill)
        outcomes = [child.stdout.readline().strip() for child in children]

        self.assertEqual(1, outcomes.count("CLAIMED"), outcomes)
        self.assertEqual(5, outcomes.count("BUSY"), outcomes)


if __name__ == "__main__":
    unittest.main()
