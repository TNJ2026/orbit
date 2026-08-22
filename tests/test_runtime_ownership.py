from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

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
