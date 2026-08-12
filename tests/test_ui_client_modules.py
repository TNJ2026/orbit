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
# The editor's canvas-to-DSL mapping. It has no dependencies of its own — that
# is why it is a plain module rather than part of the React app — so it runs
# under node with nothing installed, unlike the rest of the editor.
EDITOR_SUITES = (
    ROOT / "ui" / "editor" / "src" / "dsl-graph.test.mjs",
    ROOT / "ui" / "editor" / "src" / "api.test.mjs",
    ROOT / "ui" / "editor" / "src" / "expressions.test.mjs",
    ROOT / "ui" / "editor" / "src" / "document.test.mjs",
    ROOT / "ui" / "editor" / "src" / "layout-store.test.mjs",
    ROOT / "ui" / "editor" / "src" / "config-form.test.mjs",
)
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is not installed")
class ClientModuleTests(unittest.TestCase):
    def _run(self, suite: Path) -> None:
        result = subprocess.run(
            [NODE, "--test", str(suite)],
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

    def test_the_javascript_client_modules_pass(self) -> None:
        self._run(SUITE)

    def test_the_editor_modules_pass(self) -> None:
        for suite in EDITOR_SUITES:
            if not suite.is_file():
                self.skipTest("editor sources are not present in this checkout")
            with self.subTest(suite=suite.name):
                self._run(suite)


if __name__ == "__main__":
    unittest.main()
