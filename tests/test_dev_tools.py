"""M5: git and verify as trusted tools.

The Gate these tests defend: a workflow can *select* a development tool but can
never *describe* a command. Everything else here — artifacts, exit codes,
capability filtering — supports that one property.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.handlers import ExternalEffect, RecoveryDisposition
from orbit.workflow.handlers.dev_tools import (
    CAPABILITY_WORKSPACE_WRITE, DevToolError, GitDiffAdapter, GitIntegrateAdapter,
    GitStatusAdapter, VerifyAdapter, VerifyProfile, WorkspaceRunner,
    dev_tool_manifests, register_dev_tools,
)
from orbit.workflow.handlers.tools import ToolRegistry, ToolRequest
from orbit.workspace.git import GitWorkspaceProvider, git_available


def request(**payload):
    return ToolRequest(payload, "idem-1", {})


class RecordingArtifacts:
    def __init__(self) -> None:
        self.written = []

    def write(self, *, name, content, content_type):
        self.written.append((name, content, content_type))
        return SimpleNamespace(artifact_id=f"artifact:{name}")


class FakeProvider:
    def __init__(self, path) -> None:
        self.path = Path(path)
        self.requested = []

    def acquire(self, workspace_ref):
        self.requested.append(workspace_ref)
        return SimpleNamespace(path=self.path, workspace_ref=workspace_ref)


def scripted_runner(outcomes):
    """outcomes: list of (returncode, stdout) consumed in order."""

    calls = []

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        code, out = outcomes.pop(0) if outcomes else (0, "")
        return SimpleNamespace(
            returncode=code, stdout=out, stderr="", stdout_truncated=False,
            stderr_truncated=False, timed_out=False, cancelled=False,
        )

    run.calls = calls
    return run


class InputValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.runner = WorkspaceRunner(
            FakeProvider(self.temp.name), runner=scripted_runner([])
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_a_workspace_ref_cannot_be_a_path(self) -> None:
        adapter = GitStatusAdapter(self.runner)
        for bad in ("../etc", "/etc/passwd", "a b", "x" * 200, "", None, 7):
            with self.subTest(bad=bad):
                with self.assertRaises(DevToolError):
                    adapter.execute(request(workspace_ref=bad), None)

    def test_the_command_is_never_taken_from_the_request(self) -> None:
        """The decisive test: no input field reaches argv."""

        run = scripted_runner([(0, "")])
        runner = WorkspaceRunner(FakeProvider(self.temp.name), runner=run)
        GitStatusAdapter(runner).execute(
            request(
                workspace_ref="ws1", command="rm -rf /", argv=["curl", "evil"],
                extra_args=["--exec"],
            ),
            None,
        )
        argv, _kwargs = run.calls[0]
        self.assertEqual(GitStatusAdapter.ARGV, argv)

    def test_no_shell_is_used(self) -> None:
        run = scripted_runner([(0, "")])
        runner = WorkspaceRunner(FakeProvider(self.temp.name), runner=run)
        GitStatusAdapter(runner).execute(request(workspace_ref="ws1"), None)
        _argv, kwargs = run.calls[0]
        self.assertNotIn("shell", kwargs)

    def test_the_environment_is_explicit_not_inherited(self) -> None:
        run = scripted_runner([(0, "")])
        runner = WorkspaceRunner(
            FakeProvider(self.temp.name), runner=run, environment={"PATH": "/usr/bin"}
        )
        GitStatusAdapter(runner).execute(request(workspace_ref="ws1"), None)
        _argv, kwargs = run.calls[0]
        self.assertEqual({"PATH": "/usr/bin"}, kwargs["env"])


class StatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def adapter(self, outcomes):
        return GitStatusAdapter(
            WorkspaceRunner(FakeProvider(self.temp.name), runner=scripted_runner(outcomes))
        )

    def test_a_clean_tree_reports_clean(self) -> None:
        result = self.adapter([(0, "")]).execute(request(workspace_ref="ws1"), None)
        self.assertTrue(result.output["clean"])
        self.assertEqual([], result.output["entries"])

    def test_porcelain_output_is_parsed(self) -> None:
        result = self.adapter([(0, " M src/a.py\n?? new.txt\n")]).execute(
            request(workspace_ref="ws1"), None
        )
        self.assertFalse(result.output["clean"])
        self.assertEqual(
            [{"status": "M", "path": "src/a.py"}, {"status": "??", "path": "new.txt"}],
            result.output["entries"],
        )

    def test_a_failing_git_is_an_error_not_a_silent_clean(self) -> None:
        with self.assertRaises(DevToolError):
            self.adapter([(128, "")]).execute(request(workspace_ref="ws1"), None)


class DiffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.artifacts = RecordingArtifacts()
        self.context = SimpleNamespace(artifacts=self.artifacts)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_the_diff_body_goes_to_an_artifact_not_the_payload(self) -> None:
        body = "diff --git a/x b/x\n+line\n"
        adapter = GitDiffAdapter(
            WorkspaceRunner(
                FakeProvider(self.temp.name), runner=scripted_runner([(0, body)])
            )
        )
        result = adapter.execute(request(workspace_ref="ws1"), self.context)
        self.assertEqual("artifact:diff", result.output["diff_artifact_id"])
        self.assertNotIn("+line", repr(result.output))
        self.assertEqual(2, result.output["line_count"])

    def test_staged_is_a_flag_not_a_free_argument(self) -> None:
        run = scripted_runner([(0, "")])
        adapter = GitDiffAdapter(WorkspaceRunner(FakeProvider(self.temp.name), runner=run))
        adapter.execute(request(workspace_ref="ws1", staged=True), self.context)
        self.assertEqual(
            ("git", "diff", "--no-color", "--no-ext-diff", "--cached"), run.calls[0][0]
        )

    def test_an_undeclared_artifact_port_does_not_fail_the_tool(self) -> None:
        class Rejecting:
            def write(self, **_kwargs):
                raise PermissionError("port not declared")

        adapter = GitDiffAdapter(
            WorkspaceRunner(
                FakeProvider(self.temp.name), runner=scripted_runner([(0, "diff\n")])
            )
        )
        result = adapter.execute(
            request(workspace_ref="ws1"), SimpleNamespace(artifacts=Rejecting())
        )
        self.assertIsNone(result.output["diff_artifact_id"])
        self.assertFalse(result.output["empty"])


class VerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.profiles = [
            VerifyProfile("unit", ("python", "-m", "unittest", "discover")),
            VerifyProfile("lint", ("ruff", "check")),
        ]
        self.context = SimpleNamespace(artifacts=RecordingArtifacts())

    def tearDown(self) -> None:
        self.temp.cleanup()

    def adapter(self, outcomes):
        return VerifyAdapter(
            WorkspaceRunner(FakeProvider(self.temp.name), runner=scripted_runner(outcomes)),
            self.profiles,
        )

    def test_a_named_profile_runs_its_registered_command(self) -> None:
        run = scripted_runner([(0, "OK\n")])
        adapter = VerifyAdapter(
            WorkspaceRunner(FakeProvider(self.temp.name), runner=run), self.profiles
        )
        result = adapter.execute(
            request(workspace_ref="ws1", profile="lint"), self.context
        )
        self.assertEqual(("ruff", "check"), run.calls[0][0])
        self.assertTrue(result.output["passed"])

    def test_an_unregistered_profile_is_refused(self) -> None:
        with self.assertRaises(DevToolError) as caught:
            self.adapter([]).execute(
                request(workspace_ref="ws1", profile="rm -rf /"), self.context
            )
        self.assertIn("unknown verify profile", str(caught.exception))

    def test_a_command_shaped_profile_value_is_not_executed(self) -> None:
        run = scripted_runner([(0, "")])
        adapter = VerifyAdapter(
            WorkspaceRunner(FakeProvider(self.temp.name), runner=run), self.profiles
        )
        with self.assertRaises(DevToolError):
            adapter.execute(
                request(workspace_ref="ws1", profile=["curl", "evil.example"]),
                self.context,
            )
        self.assertEqual([], run.calls)

    def test_a_failing_verification_is_a_result_not_an_exception(self) -> None:
        result = self.adapter([(1, "2 failed\n")]).execute(
            request(workspace_ref="ws1", profile="unit"), self.context
        )
        self.assertFalse(result.output["passed"])
        self.assertEqual(1, result.output["exit_code"])

    def test_the_log_is_published_as_an_artifact(self) -> None:
        result = self.adapter([(0, "ran 3 tests\n")]).execute(
            request(workspace_ref="ws1", profile="unit"), self.context
        )
        self.assertEqual("artifact:verify_log", result.output["log_artifact_id"])
        name, content, _type = self.context.artifacts.written[0]
        self.assertEqual("verify_log", name)
        self.assertIn(b"ran 3 tests", content)

    def test_a_profile_needs_a_command(self) -> None:
        with self.assertRaises(ValueError):
            VerifyProfile("empty", ())


class IntegrateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def adapter(self, outcomes):
        return GitIntegrateAdapter(
            WorkspaceRunner(FakeProvider(self.temp.name), runner=scripted_runner(outcomes))
        )

    def test_a_commit_message_is_one_argv_element(self) -> None:
        run = scripted_runner([(0, ""), (0, ""), (0, "abc123\n")])
        adapter = GitIntegrateAdapter(
            WorkspaceRunner(FakeProvider(self.temp.name), runner=run)
        )
        adapter.execute(
            request(workspace_ref="ws1", message="--exec=rm -rf / ; echo pwned"), None
        )
        commit_argv = run.calls[1][0]
        self.assertEqual("--message", commit_argv[-2])
        self.assertEqual("--exec=rm -rf / ; echo pwned", commit_argv[-1])

    def test_a_missing_message_is_refused(self) -> None:
        with self.assertRaises(DevToolError):
            self.adapter([]).execute(request(workspace_ref="ws1"), None)

    def test_writes_declare_an_external_effect(self) -> None:
        result = self.adapter([(0, ""), (0, ""), (0, "abc123\n")]).execute(
            request(workspace_ref="ws1", message="done"), None
        )
        self.assertEqual(ExternalEffect.KNOWN_APPLIED, result.external_effect)
        self.assertEqual("abc123", result.output["commit"])

    def test_recovery_is_unknown_rather_than_merging_twice(self) -> None:
        outcome = self.adapter([]).recover("recovery:1", None)
        self.assertEqual(RecoveryDisposition.UNKNOWN, outcome.disposition)

    def test_read_only_tools_report_nothing_to_recover(self) -> None:
        outcome = GitStatusAdapter(
            WorkspaceRunner(FakeProvider(self.temp.name), runner=scripted_runner([]))
        ).recover("recovery:1", None)
        self.assertEqual(RecoveryDisposition.NOT_FOUND, outcome.disposition)


class ManifestTests(unittest.TestCase):
    def test_only_the_writing_tool_is_unknown_on_lease_loss(self) -> None:
        safety = {m.name: m.execution_safety for m in dev_tool_manifests()}
        self.assertEqual(
            ExecutionSafety.UNKNOWN_ON_LEASE_LOSS, safety["git.integrate"]
        )
        for name in ("git.status", "git.diff", "verify"):
            with self.subTest(name=name):
                self.assertEqual(ExecutionSafety.REPLAY_SAFE, safety[name])

    def test_every_tool_declares_its_capabilities(self) -> None:
        for manifest in dev_tool_manifests():
            with self.subTest(name=manifest.name):
                self.assertIn("process.run", manifest.capabilities)
                self.assertEqual((), manifest.required_secrets)


class RegistrationPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.runner = WorkspaceRunner(
            FakeProvider(self.temp.name), runner=scripted_runner([])
        )
        self.profiles = [VerifyProfile("unit", ("true",))]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_an_ungranted_capability_means_the_tool_does_not_exist(self) -> None:
        registry = ToolRegistry()
        registered = register_dev_tools(
            registry, self.runner, verify_profiles=self.profiles,
            allowed_capabilities=["workspace.read", "process.run"],
        )
        self.assertNotIn("git.integrate", registered)
        registry.seal()
        with self.assertRaises(LookupError):
            registry.resolve("git.integrate", "1.0.0")

    def test_granting_write_registers_the_integrate_tool(self) -> None:
        registry = ToolRegistry()
        registered = register_dev_tools(
            registry, self.runner, verify_profiles=self.profiles,
            allowed_capabilities=[
                "workspace.read", CAPABILITY_WORKSPACE_WRITE, "process.run",
            ],
        )
        self.assertIn("git.integrate", registered)

    def test_a_deployment_can_grant_nothing(self) -> None:
        registry = ToolRegistry()
        self.assertEqual(
            (),
            register_dev_tools(
                registry, self.runner, verify_profiles=self.profiles,
                allowed_capabilities=[],
            ),
        )


@unittest.skipUnless(git_available(), "git is not installed")
class RealGitTests(unittest.TestCase):
    """One end-to-end pass over a real repository and a real worktree."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
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
            ("git", "commit", "-m", "init"), cwd=self.root, capture_output=True, check=True
        )
        self.provider = GitWorkspaceProvider(self.root, Path(self.temp.name) / "state")
        self.runner = WorkspaceRunner(
            self.provider, timeout_seconds=60,
            environment={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": self.temp.name},
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_status_diff_and_integrate_over_a_real_worktree(self) -> None:
        artifacts = RecordingArtifacts()
        context = SimpleNamespace(artifacts=artifacts)

        clean = GitStatusAdapter(self.runner).execute(request(workspace_ref="task1"), context)
        self.assertTrue(clean.output["clean"])

        workspace = self.provider.acquire("task1").path
        (workspace / "README.md").write_text("hello\nworld\n")

        dirty = GitStatusAdapter(self.runner).execute(request(workspace_ref="task1"), context)
        self.assertFalse(dirty.output["clean"])

        diff = GitDiffAdapter(self.runner).execute(request(workspace_ref="task1"), context)
        self.assertFalse(diff.output["empty"])
        self.assertIn(b"world", artifacts.written[-1][1])

        merged = GitIntegrateAdapter(self.runner).execute(
            request(workspace_ref="task1", message="add world"), context
        )
        self.assertTrue(merged.output["committed"])
        self.assertTrue(merged.output["commit"])

        after = GitStatusAdapter(self.runner).execute(request(workspace_ref="task1"), context)
        self.assertTrue(after.output["clean"])

    def test_verify_runs_inside_the_workspace_not_the_project_root(self) -> None:
        workspace = self.provider.acquire("task2").path
        (workspace / "marker.txt").write_text("in-workspace\n")
        adapter = VerifyAdapter(
            self.runner, [VerifyProfile("show", ("cat", "marker.txt"))]
        )
        context = SimpleNamespace(artifacts=RecordingArtifacts())
        result = adapter.execute(
            request(workspace_ref="task2", profile="show"), context
        )
        self.assertTrue(result.output["passed"])
        self.assertFalse((self.root / "marker.txt").exists())


if __name__ == "__main__":
    unittest.main()
