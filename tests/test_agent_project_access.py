"""Explicit, opt-in project-file access for `agent.*` nodes.

The Gate these tests defend: `--agent-project-access` is the only switch that
lets an Agent CLI see real project files, and every failure on that path — no
grant configured, an unusable Workspace, a quota exceeded — is a deterministic
rejection of the attempt. None of them may fall back to the ordinary empty
scratch directory (a node that asked for its project would silently get an
empty one again — the exact bug this feature exists to end) and none of them
may fall back further, to the Runtime's own real working tree.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from orbit.workflow.catalogs.agent_discovery import (
    AgentCliSpec, AgentInvocation, DiscoveredAgent,
)
from orbit.workflow.domain.handlers import HandlerValidationError
from orbit.workflow.handlers.agent import TrustedCliAgentClient
from orbit.workspace import (
    FileAllowlistGrant, GitWorkspaceProvider, GitWorktreeGrant, QuotaExceeded,
    WorkspaceError, WorkspaceUnavailable,
)


def context(
    *, run_id="run-1", node_id="node-1", workspace_access=None,
    attempt_id="attempt-1",
):
    request = SimpleNamespace(
        attempt_id=attempt_id, run_id=run_id, node_id=node_id,
        workspace_access=workspace_access,
    )
    return SimpleNamespace(request=request, output=None, record_execution=None)


class Cli:
    """A CLI standing in for a real Agent: lists every file under its cwd."""

    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        script = Path(self.temp.name) / "body.py"
        script.write_text(
            "import json, os\n"
            "files = []\n"
            "for root, dirs, names in os.walk('.'):\n"
            "    dirs[:] = [d for d in dirs if d not in ('.git',)]\n"
            "    for name in names:\n"
            "        files.append(os.path.relpath(os.path.join(root, name), '.'))\n"
            "print(json.dumps({'output': {'files': sorted(files)}}))\n"
        )
        self.path = Path(self.temp.name) / "fake-cli"
        self.path.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n'
        )
        self.path.chmod(0o755)

    def cleanup(self) -> None:
        self.temp.cleanup()


class NoGrantConfiguredTests(unittest.TestCase):
    """The compiler's own gate is supposed to prevent this; the client backstops it."""

    def setUp(self) -> None:
        self.cli = Cli()
        self.addCleanup(self.cli.cleanup)

    def test_a_granted_node_with_no_grant_configured_fails_outright(self) -> None:
        client = TrustedCliAgentClient((str(self.cli.path),))
        with self.assertRaises(HandlerValidationError) as caught:
            client.execute(
                SimpleNamespace(input={}, config={}, idempotency_key="k"),
                context(workspace_access={"mode": "read_only"}),
            )
        self.assertIn("--agent-project-access", str(caught.exception))

    def test_it_never_falls_back_to_the_scratch_directory(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            client = TrustedCliAgentClient(
                (str(self.cli.path),), workspace_root=scratch,
            )
            with self.assertRaises(HandlerValidationError):
                client.execute(
                    SimpleNamespace(input={}, config={}, idempotency_key="k"),
                    context(workspace_access={"mode": "read_only"}),
                )
            # No scratch directory was created for a run that never took the
            # scratch-dir branch.
            self.assertEqual([], list(Path(scratch).iterdir()))

    def test_a_node_that_never_asked_is_unaffected(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            client = TrustedCliAgentClient(
                (str(self.cli.path),), workspace_root=scratch,
            )
            response = client.execute(
                SimpleNamespace(input={}, config={}, idempotency_key="k"),
                context(workspace_access=None),
            )
            self.assertEqual([], response.output["files"])


class GitProjectAccessTests(unittest.TestCase):
    """A git project root is delivered as a disposable worktree."""

    def setUp(self) -> None:
        self.cli = Cli()
        self.addCleanup(self.cli.cleanup)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        for argv in (
            ("git", "init", "--initial-branch=main"),
            ("git", "config", "user.email", "test@example.com"),
            ("git", "config", "user.name", "Test"),
        ):
            subprocess.run(argv, cwd=self.root, capture_output=True, check=True)
        (self.root / "README.md").write_text("hello\n")
        subprocess.run(("git", "add", "-A"), cwd=self.root, capture_output=True, check=True)
        subprocess.run(
            ("git", "commit", "-m", "init"), cwd=self.root, capture_output=True, check=True,
        )
        self.provider = GitWorkspaceProvider(self.root, Path(self.temp.name) / "state")
        self.grant = GitWorktreeGrant(self.provider)

    def test_the_agent_sees_the_real_tracked_files(self) -> None:
        client = TrustedCliAgentClient(
            (str(self.cli.path),), project_workspace=self.grant,
        )
        response = client.execute(
            SimpleNamespace(input={}, config={}, idempotency_key="k"),
            context(workspace_access={"mode": "read_only"}),
        )
        self.assertIn("README.md", response.output["files"])
        # Never the real project root itself.
        self.assertFalse((self.root / ".orbit-marker").exists())

    def test_files_in_the_policy_config_are_ignored_for_git(self) -> None:
        """A worktree already gives the whole tree; a narrower `files` list
        some node happened to declare changes nothing about it."""

        client = TrustedCliAgentClient(
            (str(self.cli.path),), project_workspace=self.grant,
        )
        response = client.execute(
            SimpleNamespace(input={}, config={}, idempotency_key="k"),
            context(workspace_access={"mode": "read_only", "files": ["nope.txt"]}),
        )
        self.assertIn("README.md", response.output["files"])


class FileAllowlistProjectAccessTests(unittest.TestCase):
    """A non-git project root is delivered as a copy of only named files."""

    def setUp(self) -> None:
        self.cli = Cli()
        self.addCleanup(self.cli.cleanup)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        (self.root / "a.txt").write_text("visible\n")
        (self.root / "secret.txt").write_text("not requested\n")
        self.state_dir = Path(self.temp.name) / "state"

    def test_only_the_declared_file_reaches_the_agent(self) -> None:
        grant = FileAllowlistGrant(self.root, self.state_dir)
        client = TrustedCliAgentClient(
            (str(self.cli.path),), project_workspace=grant,
        )
        response = client.execute(
            SimpleNamespace(input={}, config={}, idempotency_key="k"),
            context(workspace_access={"mode": "read_only", "files": ["a.txt"]}),
        )
        self.assertEqual(["a.txt"], response.output["files"])

    def test_a_files_glob_can_reach_further_than_one_name(self) -> None:
        (self.root / "docs").mkdir()
        (self.root / "docs" / "guide.md").write_text("doc\n")
        grant = FileAllowlistGrant(self.root, self.state_dir)
        client = TrustedCliAgentClient(
            (str(self.cli.path),), project_workspace=grant,
        )
        response = client.execute(
            SimpleNamespace(input={}, config={}, idempotency_key="k"),
            context(workspace_access={
                "mode": "read_only", "files": ["a.txt", "docs/**/*.md"],
            }),
        )
        self.assertEqual(
            ["a.txt", "docs/guide.md"], sorted(response.output["files"]),
        )

    def test_no_files_declared_fails_outright_rather_than_copying_nothing(self) -> None:
        grant = FileAllowlistGrant(self.root, self.state_dir)
        client = TrustedCliAgentClient(
            (str(self.cli.path),), project_workspace=grant,
        )
        with self.assertRaises(HandlerValidationError):
            client.execute(
                SimpleNamespace(input={}, config={}, idempotency_key="k"),
                context(workspace_access={"mode": "read_only"}),
            )

    def test_exceeding_the_size_cap_fails_outright(self) -> None:
        (self.root / "big.bin").write_bytes(b"0" * 4096)
        grant = FileAllowlistGrant(self.root, self.state_dir, max_bytes=1024)
        client = TrustedCliAgentClient(
            (str(self.cli.path),), project_workspace=grant,
        )
        with self.assertRaises(HandlerValidationError) as caught:
            client.execute(
                SimpleNamespace(input={}, config={}, idempotency_key="k"),
                context(workspace_access={"mode": "read_only", "files": ["big.bin"]}),
            )
        self.assertIn("workspace access could not be provisioned", str(caught.exception))

    def test_insufficient_disk_headroom_fails_outright(self) -> None:
        grant = FileAllowlistGrant(
            self.root, self.state_dir,
            # No real disk has this much free, so any non-empty grant trips it.
            min_free_bytes=10**18,
        )
        client = TrustedCliAgentClient(
            (str(self.cli.path),), project_workspace=grant,
        )
        with self.assertRaises(HandlerValidationError):
            client.execute(
                SimpleNamespace(input={}, config={}, idempotency_key="k"),
                context(workspace_access={"mode": "read_only", "files": ["a.txt"]}),
            )

    def test_a_symlink_escaping_the_project_root_is_refused_not_skipped(self) -> None:
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("not part of the project\n")
        (self.root / "link.txt").symlink_to(outside)
        grant = FileAllowlistGrant(self.root, self.state_dir)
        client = TrustedCliAgentClient(
            (str(self.cli.path),), project_workspace=grant,
        )
        with self.assertRaises(HandlerValidationError):
            client.execute(
                SimpleNamespace(input={}, config={}, idempotency_key="k"),
                context(workspace_access={"mode": "read_only", "files": ["link.txt"]}),
            )

    def test_a_retry_reuses_what_the_first_attempt_copied(self) -> None:
        grant = FileAllowlistGrant(self.root, self.state_dir)
        first = grant.acquire("run-1:node-1", files=["a.txt"])
        (first / "extra.txt").write_text("left behind by the first attempt\n")
        second = grant.acquire("run-1:node-1", files=["a.txt"])
        self.assertEqual(first, second)
        self.assertTrue((second / "extra.txt").exists())


class AcquireFailureNeverFallsBackTests(unittest.TestCase):
    """A grant that is configured but fails at acquire time still must not
    fall back to the scratch directory — the failure is this attempt's own."""

    def setUp(self) -> None:
        self.cli = Cli()
        self.addCleanup(self.cli.cleanup)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_a_git_grant_over_a_non_git_root_fails_outright(self) -> None:
        not_a_repo = Path(self.temp.name) / "not-a-repo"
        not_a_repo.mkdir()
        provider = GitWorkspaceProvider(not_a_repo, Path(self.temp.name) / "state")
        grant = GitWorktreeGrant(provider)
        scratch = Path(self.temp.name) / "scratch"
        scratch.mkdir()
        client = TrustedCliAgentClient(
            (str(self.cli.path),), project_workspace=grant, workspace_root=scratch,
        )
        with self.assertRaises(HandlerValidationError):
            client.execute(
                SimpleNamespace(input={}, config={}, idempotency_key="k"),
                context(workspace_access={"mode": "read_only"}),
            )
        self.assertEqual([], list(scratch.iterdir()))


class SweepTests(unittest.TestCase):
    """The cleanup loop's own primitive: reclaim what a settled run left."""

    def test_git_worktree_sweep_reclaims_only_the_dead_run(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "project"
        root.mkdir()
        for argv in (
            ("git", "init", "--initial-branch=main"),
            ("git", "config", "user.email", "test@example.com"),
            ("git", "config", "user.name", "Test"),
        ):
            subprocess.run(argv, cwd=root, capture_output=True, check=True)
        (root / "README.md").write_text("hello\n")
        subprocess.run(("git", "add", "-A"), cwd=root, capture_output=True, check=True)
        subprocess.run(
            ("git", "commit", "-m", "init"), cwd=root, capture_output=True, check=True,
        )
        provider = GitWorkspaceProvider(root, Path(temp.name) / "state")
        grant = GitWorktreeGrant(provider)

        dead = grant.acquire("dead-run:node-1")
        alive = grant.acquire("alive-run:node-1")
        self.assertTrue(dead.exists())
        self.assertTrue(alive.exists())

        reclaimed = grant.sweep({"alive-run:node-1"})

        self.assertIn(provider.branch_name("dead-run:node-1")[len("orbit/ws-"):], reclaimed)
        self.assertFalse(dead.exists())
        self.assertTrue(alive.exists())

    def test_file_allowlist_sweep_reclaims_only_the_dead_run(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "project"
        root.mkdir()
        (root / "a.txt").write_text("x\n")
        grant = FileAllowlistGrant(root, Path(temp.name) / "state")

        dead = grant.acquire("dead-run:node-1", files=["a.txt"])
        alive = grant.acquire("alive-run:node-1", files=["a.txt"])
        self.assertTrue(dead.exists())
        self.assertTrue(alive.exists())

        grant.sweep({"alive-run:node-1"})

        self.assertFalse(dead.exists())
        self.assertTrue(alive.exists())


CLAUDE = AgentCliSpec("claude", "claude", invocation=AgentInvocation(prompt_flag="-p"))


class CreateAppGitDetectionTests(unittest.TestCase):
    """Which grant `--agent-project-access` builds, and when it refuses to.

    Exercised at the `create_app` layer rather than by calling the detection
    logic directly, because the bug this pins is specifically about the
    *order* those checks run in: `is_git_repo()` itself shells out to git, so
    asking it before asking whether git exists at all answers "not a git
    repository" whenever git is merely missing — silently handing a real git
    project the weaker file-allowlist copy instead of refusing outright.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "runtime.db"
        # A real executable: the registry's preflight refuses a CLI that
        # cannot be found, so the fixture satisfies that rather than mocking it.
        self.executable = shutil.which("true") or "/usr/bin/true"
        self.agent = DiscoveredAgent(CLAUDE, self.executable, "2.1.3")

    def build_app(self, project_root: Path):
        from orbit.web.app import create_app
        from orbit.web.builtin_handlers import BUILTIN_SCHEMAS

        with patch(
            "orbit.workflow.catalogs.agent_discovery.discover_agent_clis_cached",
            return_value=(self.agent,),
        ):
            return create_app(
                self.db, schemas=BUILTIN_SCHEMAS, discover_agents=True,
                agent_project_access=True, workspace_path=project_root,
            )

    def project_workspace_of(self, app):
        composition = app.state.runtime
        for entry in composition.handler_registry.entries():
            if entry.manifest.name == "agent.claude":
                return entry.implementation.client.project_workspace
        raise AssertionError("agent.claude was not registered")

    def git_repo(self) -> Path:
        root = Path(self.temp.name) / "project"
        root.mkdir()
        for argv in (
            ("git", "init", "--initial-branch=main"),
            ("git", "config", "user.email", "test@example.com"),
            ("git", "config", "user.name", "Test"),
        ):
            subprocess.run(argv, cwd=root, capture_output=True, check=True)
        (root / "README.md").write_text("hello\n")
        subprocess.run(("git", "add", "-A"), cwd=root, capture_output=True, check=True)
        subprocess.run(
            ("git", "commit", "-m", "init"), cwd=root, capture_output=True, check=True,
        )
        return root

    def test_a_real_git_project_gets_a_git_worktree_grant(self) -> None:
        app = self.build_app(self.git_repo())
        self.assertIsInstance(self.project_workspace_of(app), GitWorktreeGrant)

    def test_a_non_git_project_gets_a_file_allowlist_grant(self) -> None:
        root = Path(self.temp.name) / "project"
        root.mkdir()
        app = self.build_app(root)
        self.assertIsInstance(self.project_workspace_of(app), FileAllowlistGrant)

    def test_git_missing_refuses_startup_rather_than_silently_downgrading(self) -> None:
        root = self.git_repo()
        with patch("orbit.workspace.git_available", return_value=False):
            with self.assertRaises(ValueError) as caught:
                self.build_app(root)
        self.assertIn("--agent-project-access", str(caught.exception))
        self.assertIn("git", str(caught.exception))

    def test_an_unborn_head_refuses_startup(self) -> None:
        root = Path(self.temp.name) / "project"
        root.mkdir()
        subprocess.run(
            ("git", "init", "--initial-branch=main"), cwd=root,
            capture_output=True, check=True,
        )
        with self.assertRaises(ValueError) as caught:
            self.build_app(root)
        self.assertIn("commit", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
