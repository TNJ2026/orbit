"""Regression coverage for project access recovery and durable observations."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_recovery_point import git
from orbit.platform.project_occupancy import (
    ProjectOccupancyRegistry, ProjectNeedsRecovery, ProjectBusy,
)
from orbit.workflow.langgraph_runtime.project_access import ProjectAccessCoordinator, ProjectAccessNeed
from orbit.workspace.recovery import GitRecoveryPoints, FileBackupRecoveryPoints, RecoveryUnavailable


class RecoveryRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        git(self.project, "init", "--initial-branch=main")
        git(self.project, "config", "user.name", "Test")
        git(self.project, "config", "user.email", "test@example.org")
        (self.project / "seed").write_text("seed")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-m", "initial")
        self.points = GitRecoveryPoints(self.project, min_free_bytes=0)

    def test_missing_project_settles_and_failure_survives_service_restart(self):
        from tests.test_web_composition import publish_linear_workflow, transform_registration
        from orbit.workflow.langgraph_runtime import build_service
        db = self.root / "runtime.db"
        publish_linear_workflow(db)
        service = build_service(db, [transform_registration()],
                                state_directory=self.root / "state")
        run = service.start("workflow:linear", {"value": 1},
                            idempotency_key="start", actor="local")
        registry = ProjectOccupancyRegistry(self.root / "registry")
        access = ProjectAccessCoordinator(self.project, registry=registry,
            write_granted=True, recovery_points=self.points)
        service.project_access = access
        access.acquire(run.run_id, ProjectAccessNeed(required=True, write=True))
        self.project.rename(self.root / "moved")
        settled = service._settle(run.run_id, "completed", result=run.result)
        self.assertEqual("completed", settled.status)
        self.assertFalse(access.held_by(run.run_id))
        reopened = build_service(db, [transform_registration()],
                                 state_directory=self.root / "state")
        self.assertEqual("unavailable", reopened.project_summary(run.run_id)["kind"])

    def test_release_after_project_disappears(self):
        registry = ProjectOccupancyRegistry(self.root / "registry")
        c = ProjectAccessCoordinator(self.project, registry=registry,
            write_granted=True, recovery_points=self.points)
        c.acquire("run", ProjectAccessNeed(required=True, write=True))
        self.project.rename(self.root / "moved")
        c.release("run", "completed")
        self.assertFalse(c.held_by("run"))
        self.assertEqual((), registry.occupancies())
        self.assertEqual("unavailable", c.summarize("run")["kind"])

    def test_summary_write_failure_does_not_hold_project(self):
        registry = ProjectOccupancyRegistry(self.root / "registry")
        c = ProjectAccessCoordinator(self.project, registry=registry,
            write_granted=True, recovery_points=self.points)
        c.acquire("run", ProjectAccessNeed(required=True, write=True))
        with patch("orbit.workspace.recovery._write_json", side_effect=OSError("disk full")):
            c.release("run", "completed")
        self.assertFalse(c.held_by("run"))
        self.assertIn("disk full", c.summarize("run")["error"])

    def test_restore_preserves_binary_crlf_unicode_and_mode(self):
        names = ["报告.txt", "tab\tname", "line\nname", "cr\r\nname", "binary"]
        payload = b"\xff\x00data\r\n"
        for name in names:
            (self.project / name).write_bytes(payload)
        (self.project / "binary").chmod(0o755)
        point = self.points.create("run")
        for name in names:
            (self.project / name).write_bytes(b"changed")
        changed = {item.path for item in self.points.summarize(point).content}
        self.assertTrue(set(names).issubset(changed))
        self.points.restore(point, self.points.plan_restore(point))
        for name in names:
            self.assertEqual(payload, (self.project / name).read_bytes())
        self.assertTrue((self.project / "binary").stat().st_mode & 0o111)

    def test_leaf_symlink_is_replaced_without_writing_outside(self):
        point = self.points.create("run")
        outside = self.root / "outside"
        outside.write_text("untouched")
        (self.project / "seed").unlink()
        (self.project / "seed").symlink_to(outside)
        self.points.restore(point, self.points.plan_restore(point))
        self.assertEqual("untouched", outside.read_text())
        self.assertFalse((self.project / "seed").is_symlink())

    def test_symlink_baseline_and_parent_conflict(self):
        (self.project / "link").symlink_to("seed")
        (self.project / "dir").mkdir()
        (self.project / "dir" / "file").write_text("before")
        point = self.points.create("run")
        (self.project / "link").unlink()
        (self.project / "link").write_text("changed")
        self.points.restore(point, self.points.plan_restore(point))
        self.assertEqual("seed", os.readlink(self.project / "link"))
        (self.project / "dir" / "file").unlink()
        (self.project / "dir").rmdir()
        (self.project / "dir").symlink_to(self.root)
        self.assertTrue(self.points.plan_restore(point).blocked)

    def test_recreate_reuses_baseline_and_index_metadata(self):
        git(self.project, "add", "-A")
        original = self.points.create("run")
        (self.project / "seed").write_text("changed")
        reopened = GitRecoveryPoints(self.project, min_free_bytes=0)
        self.assertEqual(original, reopened.create("run"))
        git(self.project, "gc", "--prune=now")
        self.assertEqual(original.index_tree, reopened.load("run").index_tree)

    def test_summary_is_frozen_before_project_release(self):
        registry = ProjectOccupancyRegistry(self.root / "registry")
        c = ProjectAccessCoordinator(self.project, registry=registry,
            write_granted=True, recovery_points=self.points)
        c.acquire("run", ProjectAccessNeed(required=True, write=True))
        (self.project / "seed").write_text("first run")
        c.release("run", "completed")
        summary = c.summarize("run")
        (self.project / "later").write_text("another run")
        reopened = ProjectAccessCoordinator(self.project, registry=registry,
            recovery_points=GitRecoveryPoints(self.project, min_free_bytes=0))
        self.assertEqual(summary, reopened.summarize("run"))

    def test_restart_requires_resolution_and_keeps_original_point(self):
        registry = ProjectOccupancyRegistry(self.root / "registry")
        need = ProjectAccessNeed(required=True, write=True)
        def coordinator():
            return ProjectAccessCoordinator(self.project, registry=registry,
                write_granted=True, recovery_points=GitRecoveryPoints(self.project, min_free_bytes=0))
        first = coordinator()
        first.acquire("run", need)
        original = self.points.load("run")
        (self.project / "seed").write_text("partially executed")
        first.abandon("run")
        restarted = coordinator()
        with self.assertRaises(ProjectNeedsRecovery):
            restarted.acquire("run", need)
        [(_held, token)] = registry.inspect(self.project)[0]
        registry.resolve("run", expected_claim=token)
        restarted.acquire("run", need)
        try:
            self.assertEqual(original, self.points.load("run"))
        finally:
            restarted.release("run", "completed")


class OccupancyRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        (self.project / "child").mkdir(parents=True)
        self.registry = ProjectOccupancyRegistry(self.root / "registry")

    def test_corrupt_record_is_diagnosable_and_repair_checks_owner(self):
        claim = self.registry.claim(self.project, run_id="run")
        record = next((self.root / "registry").glob("*.json"))
        record.write_text("{")
        _, errors = self.registry.inspect(self.project)
        self.assertEqual(record.name, errors[0]["record_id"])
        token = errors[0]["claim_token"]
        with self.assertRaises(ProjectBusy):
            self.registry.resolve("run", expected_claim=token)
        claim._lock.release()
        with self.assertRaises(ProjectNeedsRecovery):
            self.registry.resolve_record(record.name, expected_claim=token)
        self.registry.resolve_record(
            record.name, expected_claim=token, processes_stopped=True,
        )
        self.assertFalse(record.exists())
        self.assertEqual("{", next((self.root / "registry" / "quarantine").iterdir()).read_text())
        self.registry.claim(self.project, run_id="new").release()

    def test_resolve_run_only_repairs_its_own_corrupt_record(self):
        claim = self.registry.claim(self.project, run_id="run")
        record = next((self.root / "registry").glob("*.json"))
        record.write_text("{")
        claim._lock.release()
        token = self.registry.inspect(self.project)[1][0]["claim_token"]
        with self.assertRaises(ProjectNeedsRecovery):
            self.registry.resolve("different", expected_claim=token)
        self.assertTrue(record.exists())
        # Naming the run proves nothing extra about a record nobody can read
        # — the match is on the file name either way — so this door asks for
        # the same promise `resolve_record` asks for.
        with self.assertRaises(ProjectNeedsRecovery):
            self.registry.resolve("run", expected_claim=token)
        self.assertTrue(record.exists())
        self.assertEqual(("run",), self.registry.resolve(
            "run", expected_claim=token, processes_stopped=True,
        ))

    def test_inspect_still_answers_when_the_project_is_gone(self):
        # A vanished directory is one of the ways a run gets stuck holding it,
        # so this is exactly when the page has to work.
        claim = self.registry.claim(self.project, run_id="run")
        claim._lock.release()
        (self.project / "child").rmdir()
        self.project.rename(self.root / "moved")
        self.assertEqual(
            ["run"],
            [item.run_id for item, _token in self.registry.inspect(self.project)[0]],
        )

    def test_inspect_identifies_the_project_the_way_the_gate_does(self):
        # Two spellings of one directory: the claim gate matches on device and
        # inode, and the page that explains the refusal has to agree with it.
        real = self.root / "Cased"
        real.mkdir()
        alias = self.root / "cased"
        if not alias.exists():             # a case-sensitive volume
            alias = self.root / "alias"
            alias.symlink_to(real)
        claim = self.registry.claim(real, run_id="run")
        self.addCleanup(claim.release)
        self.assertEqual(
            [item.run_id for item in self.registry.blocked_by(alias)],
            [item.run_id for item, _token in self.registry.inspect(alias)[0]],
        )

    def test_a_confirmation_does_not_carry_to_a_later_generation(self):
        """One run can hold this project more than once.

        Between reading the page and answering it, the record may have become
        a *later* hold by the same run — whose Agent processes the person
        confirming has never seen. Their promise was about the first one.
        """

        first = self.registry.claim(self.project, run_id="run")
        first._lock.release()
        [(_seen, stale)] = self.registry.inspect(self.project)[0]

        self.registry.resolve("run", expected_claim=stale, processes_stopped=True)
        second = self.registry.claim(self.project, run_id="run")
        second._lock.release()
        [(_now, live)] = self.registry.inspect(self.project)[0]
        self.assertNotEqual(stale, live)

        with self.assertRaisesRegex(ProjectNeedsRecovery, "changed since"):
            self.registry.resolve(
                "run", expected_claim=stale, processes_stopped=True,
            )
        self.assertEqual(
            ["run"],
            [item.run_id for item, _token in self.registry.inspect(self.project)[0]],
        )
        self.registry.resolve("run", expected_claim=live, processes_stopped=True)
        self.assertEqual((), self.registry.inspect(self.project)[0])

    def test_a_corrupt_record_rewritten_underneath_is_a_different_claim(self):
        claim = self.registry.claim(self.project, run_id="run")
        record = next((self.root / "registry").glob("*.json"))
        record.write_text("{")
        claim._lock.release()
        stale = self.registry.inspect(self.project)[1][0]["claim_token"]
        record.write_text("{{")            # a later generation, still broken
        with self.assertRaisesRegex(ProjectNeedsRecovery, "changed since"):
            self.registry.resolve_record(
                record.name, expected_claim=stale, processes_stopped=True,
            )
        self.assertTrue(record.exists())

    def test_same_run_stale_claim_requires_explicit_resolution(self):
        claim = self.registry.claim(self.project, run_id="run")
        claim._lock.release()
        with self.assertRaises(ProjectNeedsRecovery):
            self.registry.claim(self.project, run_id="run")
        [(_held, token)] = self.registry.inspect(self.project)[0]
        self.registry.resolve("run", expected_claim=token)
        self.registry.claim(self.project, run_id="run").release()

    def test_corrupt_parent_record_blocks_nested_claim(self):
        claim = self.registry.claim(self.project, run_id="run")
        record = next((self.root / "registry").glob("*.json"))
        original = record.read_text()
        try:
            record.write_text("{")
            with self.assertRaises(ProjectNeedsRecovery):
                self.registry.claim(self.project / "child", run_id="other")
        finally:
            record.write_text(original)
            claim.release()

    def test_failed_atomic_rewrite_preserves_record(self):
        claim = self.registry.claim(self.project, run_id="run")
        self.addCleanup(claim.release)
        record = next((self.root / "registry").glob("*.json"))
        original = record.read_bytes()
        with patch("orbit.platform.project_occupancy.os.replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                claim.record_recovery({"kind": "git"})
        self.assertEqual(original, record.read_bytes())


class FileBackupRegressions(unittest.TestCase):
    def points(self, root):
        (root / "data").write_bytes(b"original\x00")
        return FileBackupRecoveryPoints(root, root / ".orbit", min_free_bytes=0)

    def test_protect_matching_nothing_at_all_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            points = self.points(Path(tmp))
            with self.assertRaises(RecoveryUnavailable):
                points.preflight(protect=("missing*",))
            with self.assertRaises(RecoveryUnavailable):
                points.create("invalid", protect=("missing*", "also-missing"))

    def test_one_pattern_matching_nothing_is_reported_not_refused(self):
        # Protecting the file a run is about to write is a reasonable thing
        # for a workflow to say. It is a gap in the way back, so it goes where
        # §6.2 puts every other gap — into `uncovered` — rather than refusing
        # a run that has a way back for everything else it named.
        with tempfile.TemporaryDirectory() as tmp:
            points = self.points(Path(tmp))
            points.preflight(protect=("data", "report.md"))
            point = points.create("run", protect=("data", "report.md"))
            self.assertEqual(("data",), point.covered)
            self.assertTrue(
                any("report.md" in item for item in point.uncovered), point,
            )

    def test_unmatched_patterns_refuse_and_baseline_is_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            points = self.points(root)
            point = points.create("run", protect=("data", "data"))
            self.assertEqual(("data",), point.covered)
            (root / "data").write_bytes(b"changed")
            self.assertEqual(point, points.create("run", protect=("data",)))
            points.restore(point, points.plan_restore(point))
            self.assertEqual(b"original\x00", (root / "data").read_bytes())


class RunWideGrantRegressions(unittest.TestCase):
    def test_implicit_agent_receives_grant_and_requires_capability(self):
        from dataclasses import replace
        from tests.test_workflow_langgraph_runtime import node, edge, workflow, binding
        from orbit.workflow.domain.definitions import IRPolicy
        from orbit.workflow.langgraph_runtime import compile_workflow, LangGraphHandlerRegistry
        first = replace(node("agent.first", inputs=("value",), outputs=("value",)),
                        policies=("access",))
        second = node("agent.second", inputs=("value",), outputs=("value",))
        config = {"mode": "read_write", "isolation": "none"}
        ir = workflow((first, second), (edge("next", first.id, second.id),),
            entry=(first.id,), terminals=(second.id,), result=(second.id, "value"),
            policies=(IRPolicy("access", "workspace_access", config),))
        observed = []
        def invoke(values, config, context):
            observed.append(context.workspace_access)
            return values
        caps = frozenset({"workspace.project.read", "workspace.project.write"})
        registry = LangGraphHandlerRegistry([
            binding(first.id, invoke, capabilities=caps),
            binding(second.id, invoke, capabilities=caps),
        ])
        compile_workflow(ir, registry).invoke({"value": 1})
        self.assertEqual([config, config], observed)
        with self.assertRaisesRegex(ValueError, "agent.second"):
            compile_workflow(ir, LangGraphHandlerRegistry([
                binding(first.id, invoke, capabilities=caps), binding(second.id, invoke),
            ]))

    def test_the_missing_capability_is_named_read_apart_from_write(self):
        """Which switch to turn on is the whole of what the author can act on.

        Reading a developer's files and editing them are separate grants, so
        one message covering both would send somebody to the wrong flag.
        """

        from dataclasses import replace
        from tests.test_workflow_langgraph_runtime import node, edge, workflow, binding
        from orbit.workflow.domain.definitions import IRPolicy
        from orbit.workflow.langgraph_runtime import compile_workflow, LangGraphHandlerRegistry
        first = replace(node("agent.first", inputs=("value",), outputs=("value",)),
                        policies=("access",))
        second = node("agent.second", inputs=("value",), outputs=("value",))
        def invoke(values, config, context):
            return values

        def compiled(mode, capabilities):
            ir = workflow((first, second), (edge("next", first.id, second.id),),
                entry=(first.id,), terminals=(second.id,), result=(second.id, "value"),
                policies=(IRPolicy("access", "workspace_access",
                                   {"mode": mode, "isolation": "none"}),))
            return compile_workflow(ir, LangGraphHandlerRegistry([
                binding(first.id, invoke, capabilities=capabilities),
                binding(second.id, invoke, capabilities=capabilities),
            ]))

        with self.assertRaisesRegex(ValueError, "works in the project directory"):
            compiled("read_only", frozenset())
        with self.assertRaisesRegex(ValueError, "asks to write the project"):
            compiled("read_write", frozenset({"workspace.project.read"}))
