from __future__ import annotations

from pathlib import Path
import platform
import sys
import tempfile
import unittest

from orbit.workflow.security import (
    SandboxPolicy, SandboxUnavailable, run_sandboxed,
)


class SandboxAttackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "root"
        self.root.mkdir()
        self.python = Path(sys.executable).resolve()

    def policy(self, **changes):
        values = {
            "root": self.root,
            "allowed_executables": (self.python.name,),
            "timeout_seconds": 3,
            "require_memory_enforcement": False,
        }
        values.update(changes)
        return SandboxPolicy(**values)

    @unittest.skipUnless(platform.system() == "Darwin", "macOS profile test")
    def test_script_network_bypass_is_blocked_by_os_profile(self):
        script = (
            "import socket,sys\n"
            "try:\n socket.socket().connect(('127.0.0.1', 1))\n"
            "except PermissionError:\n sys.exit(0)\n"
            "except OSError:\n sys.exit(2)\n"
        )
        result = run_sandboxed((str(self.python), "-c", script), self.policy())
        self.assertEqual(0, result.returncode, result.stderr.decode())
        self.assertEqual("macos-sandbox-exec", result.backend)

    @unittest.skipUnless(platform.system() == "Darwin", "macOS profile test")
    def test_outside_write_and_symlink_escape_are_blocked(self):
        outside = self.base / "outside.txt"
        link = self.root / "link"
        link.symlink_to(outside)
        for target in (outside, link):
            script = (
                "import pathlib,sys\n"
                f"p=pathlib.Path({str(target)!r})\n"
                "try:\n p.write_text('escape')\n"
                "except PermissionError:\n sys.exit(0)\n"
                "except OSError as e:\n sys.exit(0 if e.errno==1 else 2)\n"
                "sys.exit(3)\n"
            )
            with self.subTest(target=target):
                result = run_sandboxed((str(self.python), "-c", script), self.policy())
                self.assertEqual(0, result.returncode, result.stderr.decode())
                self.assertFalse(outside.exists())

    def test_path_traversal_cwd_is_rejected_before_spawn(self):
        with self.assertRaises(PermissionError):
            run_sandboxed(
                (str(self.python), "-c", "pass"), self.policy(), cwd=self.base
            )

    def test_output_bomb_is_killed_at_streaming_limit(self):
        policy = self.policy(
            trusted_first_party=True,
            max_output_bytes=1024,
        )
        with self.assertRaisesRegex(ValueError, "output limit"):
            run_sandboxed((str(self.python), "-c", "print('x'*100000)"), policy)

    @unittest.skipUnless(platform.system() == "Darwin", "macOS capability test")
    def test_missing_hard_memory_backend_fails_closed(self):
        with self.assertRaisesRegex(SandboxUnavailable, "address-space"):
            run_sandboxed(
                (str(self.python), "-c", "pass"),
                SandboxPolicy(self.root, (self.python.name,)),
            )

    @unittest.skipUnless(platform.system() == "Darwin", "macOS profile test")
    def test_process_fork_is_denied(self):
        script = (
            "import os,sys\n"
            "try:\n os.fork()\n"
            "except OSError:\n sys.exit(0)\n"
            "sys.exit(3)\n"
        )
        result = run_sandboxed(
            (str(self.python), "-c", script), self.policy(max_processes=1)
        )
        self.assertEqual(0, result.returncode, result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
