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
        registry.resolve("run")
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

    def test_same_run_stale_claim_requires_explicit_resolution(self):
        claim = self.registry.claim(self.project, run_id="run")
        claim._lock.release()
        with self.assertRaises(ProjectNeedsRecovery):
            self.registry.claim(self.project, run_id="run")
        self.registry.resolve("run")
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
    def test_unmatched_patterns_refuse_and_baseline_is_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").write_bytes(b"original\x00")
            points = FileBackupRecoveryPoints(root, root / ".orbit", min_free_bytes=0)
            for patterns in [("missing*",), ("data", "missing*")]:
                with self.assertRaises(RecoveryUnavailable):
                    points.preflight(protect=patterns)
                with self.assertRaises(RecoveryUnavailable):
                    points.create("invalid", protect=patterns)
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
