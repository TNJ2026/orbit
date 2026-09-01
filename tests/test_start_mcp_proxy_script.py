from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "start-mcp-proxy.sh"


class StartMcpProxyScriptTests(unittest.TestCase):
    def test_missing_workspace_delegates_to_the_core_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / "capture.txt"
            fake_uv = root / "uv"
            fake_uv.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$ORBIT_TEST_CAPTURE\"\n",
                encoding="utf-8",
            )
            fake_uv.chmod(0o755)
            environment = {
                "HOME": str(root),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "ORBIT_TEST_CAPTURE": str(capture),
                "ORBIT_UV_BIN": str(fake_uv),
            }

            result = subprocess.run(
                ["/bin/bash", str(SCRIPT)], cwd=ROOT, env=environment,
                text=True, capture_output=True, check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            arguments = capture.read_text(encoding="utf-8").splitlines()
            self.assertEqual("mcp-proxy", arguments[-2])
            self.assertTrue(arguments[-1].endswith("agent-app.json"))
            self.assertNotIn("--workspace", arguments)

    def test_explicit_uv_survives_restricted_path_and_is_forwarded_to_children(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            capture = root / "capture.txt"
            fake_uv = root / "uv"
            fake_uv.write_text(
                "#!/bin/sh\n"
                "printf 'path=%s\\n' \"$PATH\" > \"$ORBIT_TEST_CAPTURE\"\n"
                "printf '%s\\n' \"$@\" >> \"$ORBIT_TEST_CAPTURE\"\n",
                encoding="utf-8",
            )
            fake_uv.chmod(0o755)
            environment = {
                "HOME": str(root),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "ORBIT_AGENT_APP_WORKSPACE": str(workspace),
                "ORBIT_TEST_CAPTURE": str(capture),
                "ORBIT_UV_BIN": str(fake_uv),
            }

            result = subprocess.run(
                ["/bin/bash", str(SCRIPT)],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            lines = capture.read_text(encoding="utf-8").splitlines()
            self.assertTrue(lines[0].startswith(f"path={root.resolve()}:"), lines[0])
            self.assertEqual("run", lines[1])
            self.assertEqual("--workspace", lines[-2])
            self.assertEqual(str(workspace.resolve()), lines[-1])

    def test_missing_uv_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = {
                "HOME": temporary,
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "ORBIT_UV_BIN": str(Path(temporary) / "missing-uv"),
            }

            result = subprocess.run(
                ["/bin/bash", str(SCRIPT)],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(127, result.returncode)
            self.assertIn("could not find uv", result.stderr)
            self.assertIn("ORBIT_UV_BIN", result.stderr)


if __name__ == "__main__":
    unittest.main()
