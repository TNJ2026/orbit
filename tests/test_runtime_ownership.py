from __future__ import annotations

import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from orbit.platform.runtime_ownership import (
    RuntimeOwnership, RuntimeOwnershipError,
)


class RuntimeOwnershipTests(unittest.TestCase):
    def test_second_owner_is_refused_until_first_releases(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "runtime.db"
            first = RuntimeOwnership(database).acquire()
            second = RuntimeOwnership(database)
            try:
                with self.assertRaisesRegex(
                    RuntimeOwnershipError, "already owned"
                ):
                    second.acquire()
            finally:
                first.release()

            second.acquire()
            second.release()

    def test_lock_metadata_is_diagnostic_and_release_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            owner = RuntimeOwnership(Path(root) / "runtime.db").acquire()
            metadata = json.loads(owner.lock_path.read_text(encoding="utf-8"))
            self.assertEqual(str(owner.db_path), metadata["db_path"])
            self.assertGreater(metadata["pid"], 0)
            owner.release()
            owner.release()

    def test_context_manager_releases_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "runtime.db"
            with self.assertRaisesRegex(RuntimeError, "stop"):
                with RuntimeOwnership(database):
                    raise RuntimeError("stop")
            with RuntimeOwnership(database):
                pass


class McpOwnershipCleanupTests(unittest.TestCase):
    """The two paths that only run when something else has already gone wrong.

    Neither is reachable from a passing run, so neither was covered: the
    cleanup around a failed startup, and the cleanup around a failed
    shutdown. Both leave the database owned when they are wrong, and a CLI
    process exiting hides that behind the kernel — an embedded caller or a
    test does not.
    """

    def args(self, root: str) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            db=str(Path(root) / "runtime.db"), artifact_root=None,
            no_agent_discovery=True, actor="local", mcp_tool_profile="full",
        )

    def test_a_failed_artifact_store_does_not_bury_its_own_fault(self) -> None:
        from orbit.__main__ import _mcp

        boom = RuntimeError("the disk went away")
        with tempfile.TemporaryDirectory() as root:
            args = self.args(root)
            with patch("orbit.workflow.artifacts.LocalCASBackend", side_effect=boom):
                with self.assertRaises(RuntimeError) as caught:
                    _mcp(args)
            # The fault that happened, not an UnboundLocalError from the
            # handler meant to clean up after it.
            self.assertIs(boom, caught.exception)
            # And the database is free for the next process.
            RuntimeOwnership(Path(args.db)).acquire().release()

    def test_a_failed_shutdown_still_releases(self) -> None:
        """Asserted on the call, not on a second acquire succeeding.

        Skipping the release leaks nothing observable here: the exception
        leaves `ownership` unreachable, CPython closes the handle, and the
        flock goes with it. That makes the release look optional, which is
        the reason to pin it — correctness that rests on refcounting is
        correctness that a different interpreter, or one stray reference held
        for diagnostics, quietly removes.
        """

        from orbit.__main__ import _mcp

        boom = RuntimeError("a loop refused to stop")
        released: list[int] = []
        original = RuntimeOwnership.release

        def spy(self) -> None:
            released.append(1)
            original(self)

        with tempfile.TemporaryDirectory() as root:
            args = self.args(root)
            with patch("orbit.web.mcp.serve_stdio"), \
                 patch("orbit.web.app.RuntimeComposition.stop", side_effect=boom), \
                 patch.object(RuntimeOwnership, "release", spy):
                with self.assertRaises(RuntimeError) as caught:
                    _mcp(args)

        self.assertIs(boom, caught.exception)
        self.assertEqual([1], released)
