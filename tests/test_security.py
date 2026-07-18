"""M7 gate 9: secrets, ACL, path traversal, symlinks, output bombs, subprocesses.

Each class here covers one way a runtime that executes other people's work
leaks or over-reaches. These are not smoke tests: every case is an attempt to
do the thing, asserting it fails.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import time
import unittest

from orbit.platform import process as process_port
from orbit.web.api_v1 import READ_SCOPE, SENSITIVE_SCOPE, WRITE_SCOPE, Authorizer
from orbit.web.local_identity import (
    LOCAL_ACTOR, local_authorizer, loopback_authenticator,
)
from orbit.workflow.handlers.context import ScopedSecretResolver, SecretAccessError
from orbit.workflow.handlers.dev_tools import (
    DevToolError, GitStatusAdapter, VerifyAdapter, VerifyProfile, WorkspaceRunner,
)
from orbit.workflow.handlers.tools import ToolRequest
from orbit.workspace.git import GitWorkspaceProvider, WorkspaceError, workspace_slug


class SecretTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = ScopedSecretResolver(("API_KEY",), {"API_KEY": "s3cret", "OTHER": "nope"})

    def test_a_handler_cannot_resolve_a_secret_it_did_not_declare(self) -> None:
        with self.assertRaises(SecretAccessError):
            self.resolver.resolve("OTHER")

    def test_a_secret_never_appears_in_a_repr(self) -> None:
        secret = self.resolver.resolve("API_KEY")
        self.assertNotIn("s3cret", repr(secret))
        self.assertNotIn("s3cret", str(secret))
        self.assertEqual("s3cret", secret.reveal())

    def test_declared_secrets_are_redacted_out_of_output(self) -> None:
        text = "token=s3cret trailing"
        self.assertNotIn("s3cret", self.resolver.redact(text))

    def test_redaction_reaches_nested_data(self) -> None:
        payload = {"a": ["prefix s3cret suffix"], "b": {"c": "s3cret"}}
        self.assertNotIn("s3cret", repr(self.resolver.redact_data(payload)))


class AuthorizationTests(unittest.TestCase):
    def test_an_adapter_without_an_authorizer_denies_everything(self) -> None:
        """Default-deny: no configuration must never mean "local is trusted"."""

        guard = Authorizer()
        for scope in (READ_SCOPE, WRITE_SCOPE, SENSITIVE_SCOPE):
            with self.subTest(scope=scope):
                self.assertFalse(guard.allows("anyone", scope))

    def test_scopes_are_not_implied_by_one_another(self) -> None:
        guard = Authorizer(lambda actor: [READ_SCOPE])
        self.assertTrue(guard.allows("reader", READ_SCOPE))
        self.assertFalse(guard.allows("reader", WRITE_SCOPE))
        self.assertFalse(guard.allows("reader", SENSITIVE_SCOPE))

    def test_a_non_loopback_client_gets_no_identity(self) -> None:
        """The check is on the connection, not a header a proxy could forge."""

        for host in ("10.0.0.5", "203.0.113.9", "192.168.1.20"):
            with self.subTest(host=host):
                request = SimpleNamespace(client=SimpleNamespace(host=host), headers={})
                self.assertIsNone(loopback_authenticator(request))

    def test_a_missing_client_gets_no_identity(self) -> None:
        self.assertIsNone(loopback_authenticator(SimpleNamespace(client=None, headers={})))

    def test_loopback_is_the_local_operator(self) -> None:
        for host in ("127.0.0.1", "::1"):
            with self.subTest(host=host):
                request = SimpleNamespace(client=SimpleNamespace(host=host), headers={})
                self.assertEqual(LOCAL_ACTOR, loopback_authenticator(request))

    def test_only_the_local_actor_holds_local_scopes(self) -> None:
        guard = local_authorizer()
        self.assertTrue(guard.allows(LOCAL_ACTOR, WRITE_SCOPE))
        self.assertFalse(guard.allows("someone-else", READ_SCOPE))


class WorkspacePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.state = Path(self.temp.name) / "state"
        self.provider = GitWorkspaceProvider(self.root, self.state)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_a_traversing_ref_cannot_escape_the_worktrees_root(self) -> None:
        """A slug is one path segment, so it cannot traverse.

        `a-..-..-b` is checked for what matters — no separator, and not a
        relative-directory name — rather than for the literal characters:
        dots inside a single filename are harmless, and banning them would be
        a rule about spelling instead of about containment.
        """

        root = self.provider.worktrees_root
        for ref in ("../../etc", "..", ".", "a/../../b", "/etc/passwd", "....//"):
            with self.subTest(ref=ref):
                slug = workspace_slug(ref)
                self.assertNotIn("/", slug)
                self.assertNotIn("\\", slug)
                self.assertNotIn(slug, {".", ".."})
                joined = (root / slug).resolve()
                self.assertEqual(root.resolve(), joined.parent)

    def test_the_resolved_path_is_always_a_direct_child(self) -> None:
        for ref in ("task-1", "../escape", "nested/ref"):
            with self.subTest(ref=ref):
                path = self.provider._resolved_path(ref)
                self.assertEqual(self.provider.worktrees_root.name, path.parent.name)

    def test_a_symlinked_worktrees_root_is_refused(self) -> None:
        """A stale or hostile checkout can point .orbit/worktrees anywhere."""

        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        self.state.mkdir(parents=True, exist_ok=True)
        self.provider.worktrees_root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(WorkspaceError):
            self.provider._resolved_path("task-1")


class DevToolBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.calls = []

        def runner(argv, **kwargs):
            self.calls.append((tuple(argv), kwargs))
            return SimpleNamespace(
                returncode=0, stdout="", stderr="", stdout_truncated=False,
                stderr_truncated=False, timed_out=False, cancelled=False,
            )

        provider = SimpleNamespace(
            acquire=lambda ref: SimpleNamespace(path=Path(self.temp.name), workspace_ref=ref)
        )
        self.runner = WorkspaceRunner(provider, runner=runner, environment={"PATH": "/usr/bin"})

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_injection_attempts_in_a_workspace_ref_are_refused(self) -> None:
        adapter = GitStatusAdapter(self.runner)
        for ref in (
            "; rm -rf /", "$(whoami)", "`id`", "a|b", "a&&b", "../../etc",
            "ref\nsecond", "ref\x00null",
        ):
            with self.subTest(ref=ref):
                with self.assertRaises(DevToolError):
                    adapter.execute(ToolRequest({"workspace_ref": ref}, "k", {}), None)
        self.assertEqual([], self.calls, "nothing may reach the process port")

    def test_a_verify_profile_cannot_be_supplied_by_the_caller(self) -> None:
        adapter = VerifyAdapter(self.runner, [VerifyProfile("unit", ("true",))])
        for profile in ("curl evil.example", ["sh", "-c", "id"], {"argv": ["id"]}, None):
            with self.subTest(profile=profile):
                with self.assertRaises(DevToolError):
                    adapter.execute(
                        ToolRequest(
                            {"workspace_ref": "ws1", "profile": profile}, "k", {}
                        ),
                        None,
                    )
        self.assertEqual([], self.calls)

    def test_the_child_environment_is_built_not_inherited(self) -> None:
        """A verify run must not pick up the operator's exported tokens."""

        os.environ["ORBIT_TEST_LEAKED_TOKEN"] = "leaked"
        try:
            GitStatusAdapter(self.runner).execute(
                ToolRequest({"workspace_ref": "ws1"}, "k", {}), None
            )
        finally:
            del os.environ["ORBIT_TEST_LEAKED_TOKEN"]
        _argv, kwargs = self.calls[0]
        self.assertEqual({"PATH": "/usr/bin"}, kwargs["env"])
        self.assertNotIn("ORBIT_TEST_LEAKED_TOKEN", kwargs["env"])


class OutputBombTests(unittest.TestCase):
    """A handler that prints forever must not take the runtime with it."""

    def test_output_is_capped_and_flagged(self) -> None:
        result = process_port.run(
            [sys.executable, "-c", "print('x' * 10_000_000)"],
            max_output_bytes=4096, timeout=60,
        )
        self.assertLessEqual(len(result.stdout.encode()), 8192)
        self.assertTrue(result.stdout_truncated)

    def test_stderr_is_capped_independently(self) -> None:
        result = process_port.run(
            [sys.executable, "-c", "import sys; sys.stderr.write('y' * 10_000_000)"],
            max_output_bytes=4096, timeout=60,
        )
        self.assertLessEqual(len(result.stderr.encode()), 8192)
        self.assertTrue(result.stderr_truncated)

    def test_a_secret_is_redacted_before_it_reaches_the_buffer(self) -> None:
        resolver = ScopedSecretResolver(("TOKEN",), {"TOKEN": "hunter2"})
        result = process_port.run(
            [sys.executable, "-c", "print('token is hunter2')"],
            timeout=60, redactor=resolver.redact,
        )
        self.assertNotIn("hunter2", result.stdout)


class SubprocessCleanupTests(unittest.TestCase):
    def test_a_timed_out_process_is_killed(self) -> None:
        started = time.monotonic()
        result = process_port.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
        self.assertTrue(result.timed_out)
        self.assertLess(time.monotonic() - started, 20)

    def test_descendants_are_reaped_with_their_parent(self) -> None:
        """A killed handler must not leave its children running."""

        marker = Path(tempfile.mkdtemp()) / "child.pid"
        script = (
            "import subprocess, sys, time, pathlib\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"pathlib.Path({str(marker)!r}).write_text(str(child.pid))\n"
            "time.sleep(60)\n"
        )
        handle = process_port.ProcessHandle([sys.executable, "-c", script])
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.05)
        else:
            self.skipTest("child never reported its pid")

        child_pid = int(marker.read_text())
        handle.cancel()
        handle.wait(timeout=15)

        for _ in range(100):
            if not _alive(child_pid):
                break
            time.sleep(0.05)
        self.assertFalse(_alive(child_pid), "a descendant outlived its parent")

    def test_pid_zero_is_not_treated_as_a_process_tree(self) -> None:
        """On macOS pid 0 parents launchd; walking it would return every pid."""

        self.assertEqual([], process_port.descendant_pids(0))
        self.assertEqual([], process_port.descendant_pids(-1))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class NetworkExposureTests(unittest.TestCase):
    def test_serve_defaults_to_loopback(self) -> None:
        """Binding all interfaces must be a deliberate act, never a default."""

        result = subprocess.run(
            [sys.executable, "-m", "orbit", "serve", "--help"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parents[1]),
            env={
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "PATH": "/usr/bin:/bin",
            },
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("127.0.0.1", result.stdout)
        self.assertNotIn("default: 0.0.0.0", result.stdout)


if __name__ == "__main__":
    unittest.main()
