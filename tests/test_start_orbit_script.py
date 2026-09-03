from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "start-orbit.sh"


class StartOrbitScriptTests(unittest.TestCase):
    def test_missing_project_path_registers_the_current_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / "arguments.txt"
            fake_orbit = root / "orbit"
            fake_orbit.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$ORBIT_TEST_CAPTURE\"\n"
                "printf '%s\\n' '---' >> \"$ORBIT_TEST_CAPTURE\"\n",
                encoding="utf-8",
            )
            fake_orbit.chmod(0o755)
            environment = {
                **os.environ,
                "ORBIT_CLI": str(fake_orbit),
                "ORBIT_TEST_CAPTURE": str(capture),
            }
            environment.pop("ORBIT_AGENT_APP_WORKSPACE", None)

            result = subprocess.run(
                ["bash", str(SCRIPT)], cwd=root, env=environment,
                text=True, capture_output=True, check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            arguments = capture.read_text(encoding="utf-8").splitlines()
            separator = arguments.index("---")
            self.assertEqual(
                ["agent-app", "ensure", str(ROOT / "agent-app.json")],
                arguments[:separator],
            )
            self.assertEqual(
                ["hub", "register", str(root.resolve())],
                arguments[separator + 1:-1],
            )

    def test_explicit_project_path_is_canonicalized_and_forwarded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            capture = root / "arguments.txt"
            fake_orbit = root / "orbit"
            fake_orbit.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$ORBIT_TEST_CAPTURE\"\nprintf '%s\\n' '---' >> \"$ORBIT_TEST_CAPTURE\"\n",
                encoding="utf-8",
            )
            fake_orbit.chmod(0o755)
            environment = {
                **os.environ,
                "ORBIT_CLI": str(fake_orbit),
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
            separator = arguments.index("---")
            self.assertEqual(
                ["agent-app", "ensure", str(ROOT / "agent-app.json")],
                arguments[:separator],
            )
            self.assertEqual(
                ["hub", "register", str(workspace.resolve())],
                arguments[separator + 1:-1],
            )

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

    def test_agent_app_launches_through_the_single_start_script(self):
        manifest = json.loads((ROOT / "agent-app.json").read_text(encoding="utf-8"))

        self.assertEqual(
            "{manifest_dir}/start-orbit.sh",
            manifest["service"]["command"][0],
        )
        self.assertEqual("--hub-service", manifest["service"]["command"][1])
        self.assertNotIn("uv", manifest["service"]["command"])
        self.assertIn("ORBIT_CLI", manifest["service"]["environment"])

    def test_internal_hub_mode_accepts_an_explicit_orbit_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / "arguments.txt"
            fake_orbit = root / "orbit"
            fake_orbit.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$ORBIT_TEST_CAPTURE\"\n",
                encoding="utf-8",
            )
            fake_orbit.chmod(0o755)

            result = subprocess.run(
                ["bash", str(SCRIPT), "--hub-service"], cwd=ROOT,
                env={
                    **os.environ,
                    "ORBIT_CLI": str(fake_orbit),
                    "ORBIT_TEST_CAPTURE": str(capture),
                },
                text=True, capture_output=True, check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(["hub", "serve"], capture.read_text().splitlines())


if __name__ == "__main__":
    unittest.main()
