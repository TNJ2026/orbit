from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "start-orbit.sh"


class StartOrbitScriptTests(unittest.TestCase):
    def test_missing_project_path_delegates_to_the_core_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            capture = root / "arguments.txt"
            fake_uv = bin_dir / "uv"
            fake_uv.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$ORBIT_TEST_CAPTURE\"\n",
                encoding="utf-8",
            )
            fake_uv.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                "ORBIT_TEST_CAPTURE": str(capture),
            }
            environment.pop("ORBIT_AGENT_APP_WORKSPACE", None)

            result = subprocess.run(
                ["bash", str(SCRIPT)], cwd=ROOT, env=environment,
                text=True, capture_output=True, check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            arguments = capture.read_text(encoding="utf-8").splitlines()
            self.assertEqual("ensure", arguments[-2])
            self.assertTrue(arguments[-1].endswith("agent-app.json"))
            self.assertNotIn("--workspace", arguments)

    def test_explicit_project_path_is_canonicalized_and_forwarded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            workspace = root / "workspace"
            bin_dir.mkdir()
            workspace.mkdir()
            capture = root / "arguments.txt"
            fake_uv = bin_dir / "uv"
            fake_uv.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$ORBIT_TEST_CAPTURE\"\n",
                encoding="utf-8",
            )
            fake_uv.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                "ORBIT_TEST_CAPTURE": str(capture),
            }
            environment.pop("ORBIT_AGENT_APP_WORKSPACE", None)

            result = subprocess.run(
                ["bash", str(SCRIPT), str(workspace / ".")],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            arguments = capture.read_text(encoding="utf-8").splitlines()
            self.assertEqual("--workspace", arguments[-2])
            self.assertEqual(str(workspace.resolve()), arguments[-1])

    def test_nonexistent_project_path_fails_before_starting_uv(self):
        result = subprocess.run(
            ["bash", str(SCRIPT), "/definitely/missing/orbit-project"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("is not a directory", result.stderr)


if __name__ == "__main__":
    unittest.main()
