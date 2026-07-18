"""M1B: git workspaces.

Replaces the worktree half of tests/test_worktree.py (WorktreeLifecycleTests,
GitProvisioningTests, WorktreeSweepTests) and adds the traversal, symlink,
dirty-tree, repeat-acquire and crash-cleanup coverage the plan asks for.
No import of orbit.server or orbit.store.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

from orbit.workspace.git import (
    GitWorkspaceProvider,
    WorkspaceError,
    WorkspaceUnavailable,
    git_available,
    is_git_repo,
    workspace_slug,
)


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=False,
    )


@unittest.skipUnless(git_available(), "git is not installed")
class GitWorkspaceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / ".orbit"
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "test")
        (self.root / "README.md").write_text("base\n", encoding="utf-8")
        git(self.root, "add", "README.md")
        git(self.root, "commit", "-q", "-m", "base")
        self.provider = GitWorkspaceProvider(self.root, self.state)

    def tearDown(self) -> None:
        self.temp.cleanup()


class SlugTests(unittest.TestCase):
    def test_slug_is_filesystem_safe(self) -> None:
        slug = workspace_slug("run:a3f2/node#1")
        self.assertNotIn("/", slug)
        self.assertNotIn(":", slug)
        self.assertNotIn("#", slug)

    def test_similar_refs_do_not_collide(self) -> None:
        self.assertNotEqual(workspace_slug("run:a/b"), workspace_slug("run:a-b"))

    def test_slug_is_stable(self) -> None:
        self.assertEqual(workspace_slug("run:x"), workspace_slug("run:x"))

    def test_empty_ref_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            workspace_slug("  ")


class LifecycleTests(GitWorkspaceTestCase):
    def test_acquire_creates_a_worktree_on_a_branch(self) -> None:
        lease = self.provider.acquire("run:one")
        self.assertTrue(lease.path.is_dir())
        self.assertTrue((lease.path / "README.md").exists())
        self.assertTrue(lease.branch.startswith("orbit/ws-"))
        branches = git(self.root, "branch", "--list", lease.branch).stdout
        self.assertIn(lease.branch, branches)

    def test_acquire_is_idempotent(self) -> None:
        first = self.provider.acquire("run:one")
        (first.path / "work.txt").write_text("in progress\n", encoding="utf-8")
        second = self.provider.acquire("run:one")
        self.assertEqual(first.path, second.path)
        # Re-attaching must not wipe work in flight.
        self.assertTrue((second.path / "work.txt").exists())

    def test_release_removes_worktree_and_branch(self) -> None:
        lease = self.provider.acquire("run:one")
        self.provider.release("run:one")
        self.assertFalse(lease.path.exists())
        self.assertNotIn(lease.branch, git(self.root, "branch", "--list").stdout)

    def test_release_is_idempotent(self) -> None:
        self.provider.acquire("run:one")
        self.provider.release("run:one")
        self.provider.release("run:one")  # must not raise

    def test_release_can_keep_the_branch(self) -> None:
        lease = self.provider.acquire("run:one")
        self.provider.release("run:one", delete_branch=False)
        self.assertIn(lease.branch, git(self.root, "branch", "--list").stdout)

    def test_two_refs_get_isolated_worktrees(self) -> None:
        one = self.provider.acquire("run:one")
        two = self.provider.acquire("run:two")
        self.assertNotEqual(one.path, two.path)
        (one.path / "only-one.txt").write_text("x", encoding="utf-8")
        self.assertFalse((two.path / "only-one.txt").exists())


class UnavailableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_non_repository_is_unavailable_not_an_error(self) -> None:
        provider = GitWorkspaceProvider(self.root, self.root / ".orbit")
        with self.assertRaises(WorkspaceUnavailable):
            provider.acquire("run:one")

    @unittest.skipUnless(git_available(), "git is not installed")
    def test_repository_without_commits_is_unavailable(self) -> None:
        git(self.root, "init", "-q")
        provider = GitWorkspaceProvider(self.root, self.root / ".orbit")
        with self.assertRaises(WorkspaceUnavailable):
            provider.acquire("run:one")

    def test_is_git_repo_is_false_for_a_plain_directory(self) -> None:
        self.assertFalse(is_git_repo(self.root))


class PathSafetyTests(GitWorkspaceTestCase):
    def test_traversal_ref_cannot_escape_the_root(self) -> None:
        for ref in ("../../etc", "..", "../outside", "a/../../../tmp"):
            with self.subTest(ref=ref):
                path = self.provider._resolved_path(ref)
                self.assertIn(
                    self.provider.worktrees_root.resolve(),
                    [path.parent.resolve(), *path.parents],
                )

    def test_symlinked_worktrees_root_is_rejected(self) -> None:
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        self.state.mkdir(parents=True, exist_ok=True)
        link = self.state / "worktrees"
        link.symlink_to(outside, target_is_directory=True)
        provider = GitWorkspaceProvider(self.root, self.state)
        with self.assertRaises(WorkspaceError):
            provider.acquire("run:one")

    def test_resolved_path_stays_inside_for_normal_refs(self) -> None:
        path = self.provider._resolved_path("run:one")
        self.assertEqual(self.provider.worktrees_root, path.parent)


class DirtyTreeTests(GitWorkspaceTestCase):
    def test_dirty_workspace_is_detected(self) -> None:
        lease = self.provider.acquire("run:one")
        self.assertFalse(self.provider.is_dirty("run:one"))
        (lease.path / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")
        self.assertTrue(self.provider.is_dirty("run:one"))

    def test_release_discards_a_dirty_workspace(self) -> None:
        lease = self.provider.acquire("run:one")
        (lease.path / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")
        self.provider.release("run:one")
        self.assertFalse(lease.path.exists())

    def test_missing_workspace_is_not_dirty(self) -> None:
        self.assertFalse(self.provider.is_dirty("run:never-created"))


class CrashRecoveryTests(GitWorkspaceTestCase):
    def test_stale_registration_is_pruned_and_reacquired(self) -> None:
        lease = self.provider.acquire("run:one")
        # Simulate a SIGKILLed run: the directory is gone but git still has the
        # registration, which would make a fresh `worktree add` fail.
        shutil.rmtree(lease.path)
        again = self.provider.acquire("run:one")
        self.assertTrue(again.path.is_dir())

    def test_unregistered_leftover_directory_is_cleared(self) -> None:
        path = self.provider._resolved_path("run:one")
        path.mkdir(parents=True)
        (path / "junk.txt").write_text("left over\n", encoding="utf-8")
        lease = self.provider.acquire("run:one")
        self.assertTrue(lease.path.is_dir())
        self.assertFalse((lease.path / "junk.txt").exists())


class SweepTests(GitWorkspaceTestCase):
    def test_sweep_reclaims_only_dead_refs(self) -> None:
        live = self.provider.acquire("run:live")
        dead = self.provider.acquire("run:dead")
        reclaimed = self.provider.sweep({"run:live"})
        self.assertEqual((workspace_slug("run:dead"),), reclaimed)
        self.assertTrue(live.path.is_dir())
        self.assertFalse(dead.path.exists())

    def test_sweep_with_no_live_refs_reclaims_everything(self) -> None:
        self.provider.acquire("run:one")
        self.provider.acquire("run:two")
        self.assertEqual(2, len(self.provider.sweep(set())))
        self.assertEqual((), self.provider.list_workspaces())

    def test_sweep_is_idempotent(self) -> None:
        self.provider.acquire("run:one")
        self.provider.sweep(set())
        self.assertEqual((), self.provider.sweep(set()))

    def test_sweep_without_worktrees_root_is_safe(self) -> None:
        self.assertEqual((), self.provider.sweep({"run:one"}))


class ConcurrencyTests(GitWorkspaceTestCase):
    def test_concurrent_acquire_of_the_same_ref_yields_one_worktree(self) -> None:
        results: list[object] = []
        errors: list[BaseException] = []

        def acquire() -> None:
            try:
                results.append(self.provider.acquire("run:shared").path)
            except BaseException as exc:  # noqa: BLE001 - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=acquire) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual([], errors)
        self.assertEqual(4, len(results))
        self.assertEqual(1, len(set(results)), "same ref must map to one worktree")

    def test_concurrent_acquire_of_different_refs_all_succeed(self) -> None:
        paths: list[Path] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def acquire(index: int) -> None:
            try:
                lease = self.provider.acquire(f"run:{index}")
                with lock:
                    paths.append(lease.path)
            except BaseException as exc:  # noqa: BLE001 - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=acquire, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual([], errors)
        self.assertEqual(4, len(set(paths)))


class GitignoreTests(GitWorkspaceTestCase):
    def test_state_dir_is_added_to_gitignore(self) -> None:
        self.provider.ensure_state_dir_ignored()
        content = (self.root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".orbit/", content)

    def test_ensure_is_idempotent(self) -> None:
        self.provider.ensure_state_dir_ignored()
        self.provider.ensure_state_dir_ignored()
        content = (self.root / ".gitignore").read_text(encoding="utf-8")
        self.assertEqual(1, content.count(".orbit/"))

    def test_worktree_does_not_dirty_the_main_tree(self) -> None:
        self.provider.ensure_state_dir_ignored()
        git(self.root, "add", ".gitignore")
        git(self.root, "commit", "-q", "-m", "ignore state dir")
        self.provider.acquire("run:one")
        status = git(self.root, "status", "--porcelain").stdout
        self.assertEqual("", status.strip(), "worktree leaked into the main tree")


class BoundaryTests(unittest.TestCase):
    def test_workspace_module_does_not_import_engine_or_domain(self) -> None:
        import ast
        from orbit.workspace import git as git_module

        tree = ast.parse(Path(git_module.__file__).read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if any(part in {"server", "store", "workflow"} for part in name.split(".")):
                    offenders.append(f"{node.lineno}:{name}")
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
