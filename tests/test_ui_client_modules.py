"""Runs the JavaScript client-module tests under node, if node is installed.

node is not a build dependency of orbit, so this skips rather than fails when
it is missing — but it runs in any environment that has it, which is where the
client-side regressions would otherwise go unnoticed until someone opened the
page.
"""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "tests" / "ui" / "client_modules.test.mjs"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is not installed")
class ClientModuleTests(unittest.TestCase):
    def test_the_javascript_client_modules_pass(self) -> None:
        result = subprocess.run(
            [NODE, "--test", str(SUITE)],
            capture_output=True, text=True, cwd=str(ROOT), timeout=120,
        )
        if result.returncode != 0:
            self.fail(
                "node --test failed\n"
                f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
            )
        # A suite that silently ran nothing would otherwise pass forever.
        # node's summary line is "ℹ pass N" (TAP reporters use "# pass N").
        passed = re.search(r"[ℹ#]\s*pass\s+(\d+)", result.stdout)
        self.assertIsNotNone(passed, f"no pass count in output:\n{result.stdout}")
        self.assertGreater(int(passed.group(1)), 0)


if __name__ == "__main__":
    unittest.main()
