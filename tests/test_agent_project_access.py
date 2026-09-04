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
import threading
import time
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
from orbit.workspace.git import workspace_slug


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

    def test_agent_nodes_in_one_run_share_one_worktree(self) -> None:
        client = TrustedCliAgentClient(
            (str(self.cli.path),), project_workspace=self.grant,
        )
        first = client._workspace(context(  # noqa: SLF001
            run_id="run-shared", node_id="one",
            workspace_access={"mode": "read_write", "isolation": "worktree"},
        ))
        (first / "from-first.txt").write_text("shared\n")
        second = client._workspace(context(  # noqa: SLF001
            run_id="run-shared", node_id="two",
            workspace_access={"mode": "read_write", "isolation": "worktree"},
        ))

        self.assertEqual(first.resolve(), second.resolve())
        self.assertEqual("shared\n", (second / "from-first.txt").read_text())

    def test_dirty_source_checkout_is_refused(self) -> None:
        (self.root / "untracked.txt").write_text("not in HEAD\n")
        with self.assertRaisesRegex(
            WorkspaceError, "commit or stash.*before starting",
        ):
            self.grant.acquire("run-dirty")


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

    def test_sweep_leaves_a_fresh_dead_looking_destination_alone(self) -> None:
        """The race this grace period exists to close: a destination that
        `sweep()`'s caller does not (yet) know is live must survive one pass
        anyway, because it might only look dead due to a liveness snapshot
        taken a moment before this ref actually became live."""

        grant = FileAllowlistGrant(self.root, self.state_dir)
        destination = grant.acquire("run-1:node-1", files=["a.txt"])

        grant.sweep(())  # nothing declared live — this is the fresh reuse case

        self.assertTrue(destination.exists())

    def test_sweep_reclaims_a_destination_once_it_outlives_the_grace_period(self) -> None:
        grant = FileAllowlistGrant(self.root, self.state_dir, min_age_seconds=0.05)
        destination = grant.acquire("run-1:node-1", files=["a.txt"])
        time.sleep(0.1)

        grant.sweep(())

        self.assertFalse(destination.exists())

    def test_sweep_leaves_a_fresh_staging_copy_alone(self) -> None:
        """The bug `ac131ff` introduced: unconditionally deleting everything
        under the staging root would just as happily delete a copy another
        process is mid-way through writing — staging entries never appear in
        `live_refs` at all, so only age can tell the two apart."""

        grant = FileAllowlistGrant(self.root, self.state_dir)
        in_progress = grant._staging_root / "some-other-process-mid-copy"
        in_progress.mkdir(parents=True)
        (in_progress / "partial.bin").write_bytes(b"still being written")

        grant.sweep(())

        self.assertTrue(in_progress.exists())

    def test_sweep_reclaims_an_old_orphaned_staging_copy(self) -> None:
        grant = FileAllowlistGrant(self.root, self.state_dir, min_age_seconds=0.05)
        orphan = grant._staging_root / "orphan"
        orphan.mkdir(parents=True)
        (orphan / "large.bin").write_bytes(b"orphaned")
        time.sleep(0.1)

        grant.sweep(())

        self.assertFalse(orphan.exists())

    def test_files_matching_nothing_fails_outright_rather_than_an_empty_grant(self) -> None:
        grant = FileAllowlistGrant(self.root, self.state_dir)
        with self.assertRaises(WorkspaceError):
            grant.acquire("run-1:node-1", files=["nope-*.md"])

    def test_a_copy_that_fails_partway_never_looks_complete_on_retry(self) -> None:
        """The bug this pins: a destination that only exists because a copy
        got partway through must never be mistaken, on the next attempt, for
        one that finished — that would hand the Agent an incomplete or stale
        view of exactly the files it was told it would get."""

        (self.root / "b.txt").write_text("also visible\n")
        grant = FileAllowlistGrant(self.root, self.state_dir)

        real_copy2 = shutil.copy2
        calls = {"count": 0}

        def flaky_copy2(source, target, *args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("disk went away mid-copy")
            return real_copy2(source, target, *args, **kwargs)

        with patch("shutil.copy2", side_effect=flaky_copy2):
            with self.assertRaises(WorkspaceError):
                grant.acquire("run-1:node-1", files=["a.txt", "b.txt"])

        # The failed attempt must not have left a directory a retry would
        # mistake for a completed one.
        destination = grant._destination("run-1:node-1")
        self.assertFalse(destination.exists())

        # A clean retry, without the injected failure, succeeds completely.
        result = grant.acquire("run-1:node-1", files=["a.txt", "b.txt"])
        self.assertEqual(
            ["a.txt", "b.txt"],
            sorted(p.name for p in result.iterdir()),
        )

    def test_a_pre_versioning_partial_copy_left_on_disk_is_never_reused(self) -> None:
        """The version before the atomic staging-then-rename invariant could
        leave a destination non-empty but incomplete after a failed copy.
        Upgrading over that on-disk state must not resurrect the bug: a
        directory built by that older code has to be invisible to this one,
        not re-validated by it."""

        (self.root / "b.txt").write_text("also visible\n")
        grant = FileAllowlistGrant(self.root, self.state_dir)

        # What the pre-STORAGE_VERSION code left behind: a real, non-empty,
        # but incomplete directory at the *unversioned* path name it used.
        stale_root = self.state_dir / "project-files"
        stale_destination = stale_root / workspace_slug("run-1:node-1")
        stale_destination.mkdir(parents=True)
        (stale_destination / "a.txt").write_text("copied before the crash\n")

        result = grant.acquire("run-1:node-1", files=["a.txt", "b.txt"])

        self.assertNotEqual(stale_destination, result)
        self.assertEqual(
            ["a.txt", "b.txt"], sorted(p.name for p in result.iterdir()),
        )

    def test_concurrent_acquire_of_the_same_ref_never_corrupts_the_destination(self) -> None:
        grant = FileAllowlistGrant(self.root, self.state_dir)
        results: list[Path] = []
        errors: list[BaseException] = []

        def run() -> None:
            try:
                results.append(grant.acquire("run-1:node-1", files=["a.txt"]))
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual([], errors)
        self.assertEqual(8, len(results))
        self.assertEqual(1, len(set(results)))
        destination = results[0]
        self.assertEqual(["a.txt"], [p.name for p in destination.iterdir()])


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


class GrantDoesNotMoveTheFingerprintTests(unittest.TestCase):
    """Turning the switch on must not invalidate published Workflows.

    Capabilities are part of `HandlerManifest.fingerprint`, and a published
    Workflow records the fingerprint it was compiled against — `resolve()`
    matches it exactly. So a deployment-granted capability folded into the
    manifest would change every agent Handler's fingerprint, and every
    Workflow already published against one would stop binding ("handler
    manifest mismatch") the moment an operator passed
    `--agent-project-access`. The grant travels on the registration instead,
    and is put back together with the manifest's own capabilities only where
    the compiler reads it: `BoundHandler.capabilities`.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        executable = shutil.which("true") or "/usr/bin/true"
        self.agent = DiscoveredAgent(
            AgentCliSpec(
                "opencode", "opencode",
                invocation=AgentInvocation(prompt_flag="-p"),
            ),
            executable, "1.18.16",
        )

    def registrations(self, grant):
        from orbit.web.builtin_handlers import agent_handlers

        registrations, _names = agent_handlers(
            [self.agent], grant_capabilities=grant,
        )
        return registrations

    def bound(self, registrations):
        from orbit.workflow.langgraph_runtime.artifacts import LangGraphArtifactStore
        from orbit.workflow.langgraph_runtime.wiring import trusted_handlers

        db = Path(self.temp.name) / "runs.sqlite3"
        store = LangGraphArtifactStore(db, Path(self.temp.name) / "artifacts")
        registry = trusted_handlers(
            registrations, attempt_db_path=db, artifact_store=store,
        )
        return registry._entries["agent.opencode"]  # noqa: SLF001

    def test_the_grant_leaves_the_manifest_fingerprint_untouched(self) -> None:
        without = self.registrations(frozenset())[0].manifest
        granted = self.registrations(frozenset({"workspace.read"}))[0].manifest

        self.assertEqual(without.fingerprint, granted.fingerprint)
        self.assertEqual(without.capabilities, granted.capabilities)

    def test_the_grant_still_reaches_the_compilers_gate(self) -> None:
        granted = self.bound(self.registrations(frozenset({"workspace.read"})))
        ungranted = self.bound(self.registrations(frozenset()))

        self.assertIn("workspace.read", granted.capabilities)
        self.assertNotIn("workspace.read", ungranted.capabilities)
        # Same Handler either way, as far as a published Workflow can tell.
        self.assertEqual(
            granted.manifest_fingerprint, ungranted.manifest_fingerprint,
        )


class MultiprocessCompatibilityTests(unittest.TestCase):
    """Both grants must survive being handed to a worker process.

    `--execution-workers` is nonzero by default, so Agent Handlers — and the
    grant object `TrustedCliAgentClient.project_workspace` holds — run in a
    separate worker process from the one the cleanup loop runs in
    (`execution_worker.py`'s `_serve_worker`, started via
    `multiprocessing.Process(args=(tuple(registrations), ...))`). On any
    platform without `fork` (Windows, most prominently, but also anywhere
    `multiprocessing`'s default moves to `spawn`/`forkserver`), that means
    every field on a grant must be picklable — an unpicklable field such as a
    `threading.Lock` does not just fail to synchronize across that process
    boundary, it crashes the Runtime at startup the moment
    `--agent-project-access` is combined with the default `--execution-workers`.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_file_allowlist_grant_is_picklable(self) -> None:
        import pickle

        grant = FileAllowlistGrant("/tmp/project", "/tmp/state")
        restored = pickle.loads(pickle.dumps(grant))
        self.assertEqual(grant.project_root, restored.project_root)

    def test_git_worktree_grant_is_picklable(self) -> None:
        import pickle

        provider = GitWorkspaceProvider("/tmp/project", "/tmp/state")
        grant = GitWorktreeGrant(provider)
        restored = pickle.loads(pickle.dumps(grant))
        self.assertEqual(grant.provider.project_root, restored.provider.project_root)

    def test_a_registered_agent_actually_starts_in_an_execution_worker(self) -> None:
        """The end-to-end version of the two tests above: this is what
        `orbit serve`'s default `--execution-workers 1` actually does at
        startup — send every registration, project_workspace grant included,
        to a real child process."""

        import shutil as shutil_module

        from orbit.web.builtin_handlers import agent_handlers
        from orbit.workflow.langgraph_runtime.execution_worker import (
            start_execution_worker,
        )

        executable = shutil_module.which("true") or "/usr/bin/true"
        agent = DiscoveredAgent(
            AgentCliSpec("claude", "claude", invocation=AgentInvocation(prompt_flag="-p")),
            executable, "1.0.0",
        )
        grant = FileAllowlistGrant(self.temp.name, Path(self.temp.name) / "state")
        registrations, _names = agent_handlers(
            [agent], grant_capabilities=frozenset({"workspace.read.files"}),
            project_workspace=grant,
        )

        registry, worker = start_execution_worker(
            registrations, state_directory=Path(self.temp.name) / "worker-state",
        )
        try:
            self.assertTrue(worker.alive)
            self.assertIn("agent.claude", registry._entries)  # noqa: SLF001
        finally:
            worker.stop()


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
        # min_age_seconds=0: this test is about the live/dead distinction,
        # not the grace period — that has its own tests.
        grant = GitWorktreeGrant(provider, min_age_seconds=0)

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
        # min_age_seconds=0: this test is about the live/dead distinction,
        # not the grace period — that has its own tests.
        grant = FileAllowlistGrant(root, Path(temp.name) / "state", min_age_seconds=0)

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

    def project_root_of(self, app):
        composition = app.state.runtime
        for entry in composition.handler_registry.entries():
            if entry.manifest.name == "agent.claude":
                return entry.implementation.client.project_root
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

    def test_a_non_git_project_gets_the_complete_real_directory(self) -> None:
        root = Path(self.temp.name) / "project"
        root.mkdir()
        app = self.build_app(root)
        self.assertIsNone(self.project_workspace_of(app))
        self.assertEqual(root.resolve(), self.project_root_of(app))

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

    def test_a_missing_workspace_path_refuses_startup_rather_than_using_cwd(self) -> None:
        """An embedder that flips the switch on without also naming a
        Workspace must not have it silently default to wherever this
        process happens to have been started from."""

        from orbit.web.app import create_app
        from orbit.web.builtin_handlers import BUILTIN_SCHEMAS

        with patch(
            "orbit.workflow.catalogs.agent_discovery.discover_agent_clis_cached",
            return_value=(self.agent,),
        ):
            with self.assertRaises(ValueError) as caught:
                create_app(
                    self.db, schemas=BUILTIN_SCHEMAS, discover_agents=True,
                    agent_project_access=True,
                )
        self.assertIn("workspace_path", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class DirectProjectAccessTests(unittest.TestCase):
    """`isolation: none` — the project directory itself, not a copy of it.

    The discriminator throughout: content that exists only in the working
    tree. A git worktree is checked out at a commit, so uncommitted edits and
    untracked files are precisely what it cannot show — if the Agent sees
    them, it is standing in the real project.
    """

    def setUp(self) -> None:
        self.cli = Cli()
        self.addCleanup(self.cli.cleanup)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        for argv in (
            ("git", "init", "--initial-branch=main"),
            ("git", "config", "user.email", "t@e.com"),
            ("git", "config", "user.name", "T"),
        ):
            subprocess.run(argv, cwd=self.project, capture_output=True, check=True)
        (self.project / "committed.txt").write_text("committed\n")
        subprocess.run(("git", "add", "-A"), cwd=self.project, capture_output=True, check=True)
        subprocess.run(
            ("git", "commit", "-m", "init"), cwd=self.project,
            capture_output=True, check=True,
        )
        (self.project / "committed.txt").write_text("committed\nUNCOMMITTED\n")
        (self.project / "untracked.txt").write_text("untracked\n")

    def client(self, **kwargs):
        return TrustedCliAgentClient((str(self.cli.path),), **kwargs)

    def run_agent(self, client):
        return client.execute(
            SimpleNamespace(input={}, config={}, idempotency_key="k"),
            context(workspace_access={"mode": "read_write", "isolation": "none"}),
        )

    def test_the_agent_stands_in_the_real_working_tree(self) -> None:
        response = self.run_agent(self.client(project_root=self.project))

        files = response.output["files"]
        self.assertIn("committed.txt", files)
        # Neither of these can appear in a worktree checked out at a commit.
        self.assertIn("untracked.txt", files)
        self.assertEqual(
            "committed\nUNCOMMITTED\n",
            (self.project / "committed.txt").read_text(),
        )

    def test_without_the_grant_it_fails_rather_than_falling_back(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            client = self.client(workspace_root=scratch)  # no project_root
            with self.assertRaises(HandlerValidationError) as caught:
                self.run_agent(client)
            self.assertIn("--agent-project-read", str(caught.exception))
            self.assertEqual([], list(Path(scratch).iterdir()))

    def test_a_vanished_project_is_an_error_not_a_scratch_directory(self) -> None:
        gone = Path(self.temp.name) / "gone"
        with tempfile.TemporaryDirectory() as scratch:
            client = self.client(project_root=self.project, workspace_root=scratch)
            client.project_root = gone
            with self.assertRaises(HandlerValidationError) as caught:
                self.run_agent(client)
            self.assertIn("project directory is gone", str(caught.exception))
            self.assertEqual([], list(Path(scratch).iterdir()))

    def test_a_scratch_directory_is_offered_under_the_state_directory(self) -> None:
        client = self.client(project_root=self.project)
        scratch = client._run_scratch(  # noqa: SLF001
            context(run_id="langgraph_run:abc")
        )
        self.assertEqual(
            self.project.resolve() / ".orbit" / "run-tmp" / "langgraph_run_abc",
            scratch,
        )
        self.assertTrue(scratch.is_dir())


class ScratchDirectoryPromptTests(unittest.TestCase):
    """An Agent in the real project is told where to put its own files."""

    def test_the_prompt_names_the_scratch_directory(self) -> None:
        from orbit.workflow.handlers.agent import render_agent_prompt

        rendered = render_agent_prompt(
            {"prompt": "do the thing"}, {},
            scratch_dir=Path("/p/.orbit/run-tmp/run-1"),
        )

        self.assertIn("/p/.orbit/run-tmp/run-1", rendered)
        self.assertIn("real project directory", rendered)

    def test_a_node_without_the_project_is_told_nothing_extra(self) -> None:
        from orbit.workflow.handlers.agent import render_agent_prompt

        rendered = render_agent_prompt({"prompt": "do the thing"}, {})

        self.assertNotIn("run-tmp", rendered)
        self.assertNotIn("real project directory", rendered)
