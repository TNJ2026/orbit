"""Environment provider registry: resolution matrix, delegation, schema exposure."""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orbit import environments
from orbit.environments import (
    ENVIRONMENT_PROVIDERS,
    GIT_WORKTREE,
    PROJECT_ROOT,
    EnvironmentProvider,
    environment_provider_schema,
    resolve_environment,
)
from orbit.node_handlers import workflow_node_schema
from orbit.workflow_config import _normalize_workflow_step, default_workflow_steps
from orbit.worktrees import workflow_needs_git


class ResolveEnvironmentTests(unittest.TestCase):
    """resolve_environment derivation matrix (booleans, explicit type, conflicts)."""

    def test_plain_step_defaults_to_project_root(self):
        provider, scope, cleanup = resolve_environment({"id": "a"})
        self.assertIs(provider, PROJECT_ROOT)
        self.assertEqual(scope, PROJECT_ROOT.default_scope)
        self.assertEqual(cleanup, PROJECT_ROOT.default_cleanup)

    def test_legacy_isolate_boolean_derives_git_worktree(self):
        provider, scope, cleanup = resolve_environment({"id": "a", "isolate": True})
        self.assertIs(provider, GIT_WORKTREE)
        self.assertEqual(scope, "workflow_item")
        self.assertEqual(cleanup, "on_terminal")

    def test_legacy_integrate_boolean_stays_in_project_root(self):
        provider, _scope, _cleanup = resolve_environment(
            {"id": "a", "integrate": True}
        )
        self.assertIs(provider, PROJECT_ROOT)

    def test_explicit_type_wins_over_isolate_boolean(self):
        provider, _scope, _cleanup = resolve_environment(
            {
                "id": "a",
                "isolate": True,
                "environment": {"type": "project_root"},
            }
        )
        self.assertIs(provider, PROJECT_ROOT)

    def test_explicit_git_worktree_without_isolate_boolean(self):
        provider, scope, cleanup = resolve_environment(
            {
                "id": "a",
                "environment": {
                    "type": "git.worktree",
                    "scope": "workflow_item",
                    "cleanup": "manual",
                },
            }
        )
        self.assertIs(provider, GIT_WORKTREE)
        self.assertEqual(scope, "workflow_item")
        self.assertEqual(cleanup, "manual")

    def test_structural_steps_never_isolate_even_with_declared_worktree(self):
        # Mirrors _normalize_workflow_step: integrate/decompose/approval run in
        # the main tree even when a git.worktree environment is declared.
        for flags in (
            {"integrate": True},
            {"decompose": True},
            {"approval": True},
            {"type": "approval"},
        ):
            with self.subTest(flags=flags):
                provider, _scope, _cleanup = resolve_environment(
                    {
                        "id": "a",
                        "environment": {"type": "git.worktree"},
                        **flags,
                    }
                )
                self.assertIs(provider, PROJECT_ROOT)

    def test_unknown_environment_type_raises(self):
        with self.assertRaises(ValueError):
            resolve_environment({"id": "a", "environment": {"type": "container"}})

    def test_matches_normalized_isolate_field_for_default_workflow(self):
        # Equivalence with the pre-seam runner: the provider is git.worktree
        # exactly when the normalized step's isolate boolean is set.
        for index, raw in enumerate(default_workflow_steps()):
            step = _normalize_workflow_step(raw, index)
            provider, _scope, _cleanup = resolve_environment(step)
            with self.subTest(step=step["id"]):
                self.assertEqual(provider is GIT_WORKTREE, bool(step["isolate"]))


class ProviderRegistryTests(unittest.TestCase):
    def test_registry_contains_exactly_the_builtin_providers(self):
        self.assertEqual(
            set(ENVIRONMENT_PROVIDERS), {"project_root", "git.worktree"}
        )
        self.assertIs(ENVIRONMENT_PROVIDERS["project_root"], PROJECT_ROOT)
        self.assertIs(ENVIRONMENT_PROVIDERS["git.worktree"], GIT_WORKTREE)

    def test_registry_ids_are_consistent_and_described(self):
        for key, provider in ENVIRONMENT_PROVIDERS.items():
            with self.subTest(provider=key):
                self.assertIsInstance(provider, EnvironmentProvider)
                self.assertEqual(provider.id, key)
                described = provider.describe()
                self.assertEqual(described["id"], key)
                self.assertTrue(described["description"])
                self.assertIn("default_scope", described)
                self.assertIn("default_cleanup", described)

    def test_node_schema_exposes_providers(self):
        schema = workflow_node_schema()
        self.assertEqual(schema["environments"], environment_provider_schema())
        self.assertEqual(
            [item["id"] for item in schema["environments"]],
            ["project_root", "git.worktree"],
        )


class ProjectRootProviderTests(unittest.TestCase):
    def test_acquire_returns_project_root_without_side_effects(self):
        with TemporaryDirectory() as tmp:
            env = PROJECT_ROOT.acquire(
                {"project_root": tmp, "task_id": 1, "step": {}}
            )
            self.assertEqual(env["root"], Path(tmp).resolve())
            self.assertFalse(env["meta"]["isolated"])
            # No provisioning happened: the directory stays empty.
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_release_is_a_no_op(self):
        with mock.patch.object(environments, "_remove_task_worktree") as removed:
            self.assertIsNone(
                PROJECT_ROOT.release({"project_root": "/x", "task_id": 1}, "on_terminal")
            )
            removed.assert_not_called()


class GitWorktreeProviderTests(unittest.TestCase):
    """The git.worktree provider delegates to worktrees.py (mocked; no real git)."""

    def test_acquire_delegates_to_ensure_task_worktree(self):
        wt = Path("/state/worktrees/task-7")
        with mock.patch.object(
            environments, "_ensure_task_worktree", return_value=wt
        ) as ensure:
            env = GIT_WORKTREE.acquire(
                {"project_root": "/proj", "task_id": 7, "step": {}}
            )
        ensure.assert_called_once_with("/proj", 7)
        self.assertEqual(env["root"], wt)
        self.assertTrue(env["meta"]["isolated"])

    def test_acquire_falls_back_to_project_root_when_worktree_unavailable(self):
        with TemporaryDirectory() as tmp:
            with mock.patch.object(
                environments, "_ensure_task_worktree", return_value=None
            ):
                env = GIT_WORKTREE.acquire(
                    {"project_root": tmp, "task_id": 7, "step": {}}
                )
        self.assertEqual(env["root"], Path(tmp).resolve())
        self.assertFalse(env["meta"]["isolated"])

    def test_release_delegates_unless_manual(self):
        context = {"project_root": "/proj", "task_id": 7}
        with mock.patch.object(environments, "_remove_task_worktree") as removed:
            GIT_WORKTREE.release(context, "manual")
            removed.assert_not_called()
            GIT_WORKTREE.release(context, "on_terminal")
            removed.assert_called_once_with("/proj", 7)


class WorkflowNeedsGitTests(unittest.TestCase):
    """workflow_needs_git reads resolved providers, not raw booleans."""

    def test_explicit_worktree_environment_needs_git(self):
        cfg = {"steps": [{"id": "a", "environment": {"type": "git.worktree"}}]}
        self.assertTrue(workflow_needs_git(cfg))

    def test_integrate_needs_git_even_in_project_root(self):
        cfg = {"steps": [{"id": "a", "integrate": True}]}
        self.assertTrue(workflow_needs_git(cfg))

    def test_project_root_only_workflow_does_not_need_git(self):
        cfg = {
            "steps": [
                {"id": "a", "environment": {"type": "project_root"}},
                {"id": "b"},
            ]
        }
        self.assertFalse(workflow_needs_git(cfg))


if __name__ == "__main__":
    unittest.main()
