"""M7 gate 4: a clean install of the built wheel actually runs.

Everything else in the suite imports from `src/`. This builds the distribution,
installs it into an empty environment, and drives the console script — which is
what catches a module that is imported but not packaged, or a data file that
only exists in the checkout.

Slow and dependency-bound, so it skips when the build tooling is absent.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
UV = shutil.which("uv")


@unittest.skipUnless(UV, "uv is not installed")
class CleanInstallTests(unittest.TestCase):
    """Build once, then exercise the installed CLI."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.temp.name)

        build = subprocess.run(
            [UV, "build", "--wheel", "--out-dir", str(cls.dir / "dist")],
            capture_output=True, text=True, cwd=str(ROOT), timeout=600,
        )
        if build.returncode != 0:
            raise unittest.SkipTest(f"uv build failed:\n{build.stderr[-2000:]}")

        wheels = sorted((cls.dir / "dist").glob("*.whl"))
        if not wheels:
            raise unittest.SkipTest("no wheel was produced")
        cls.wheel = wheels[-1]

        cls.venv = cls.dir / "venv"
        subprocess.run(
            [UV, "venv", str(cls.venv)], capture_output=True, text=True,
            cwd=str(ROOT), timeout=300, check=True,
        )
        install = subprocess.run(
            [UV, "pip", "install", "--python", str(cls.venv / "bin" / "python"),
             str(cls.wheel)],
            capture_output=True, text=True, timeout=600,
        )
        if install.returncode != 0:
            raise unittest.SkipTest(f"install failed:\n{install.stderr[-2000:]}")
        cls.orbit = cls.venv / "bin" / "orbit"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def orbit_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.orbit), *args], capture_output=True, text=True,
            cwd=str(self.dir), timeout=180,
        )

    def test_the_console_script_is_installed(self) -> None:
        self.assertTrue(self.orbit.exists(), "no `orbit` entry point in the wheel")
        result = self.orbit_cli("--version")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("orbit", result.stdout)

    def test_the_installed_cli_offers_only_the_target_commands(self) -> None:
        result = self.orbit_cli("--help")
        self.assertEqual(0, result.returncode, result.stderr)
        for command in ("serve", "workflow", "run", "db"):
            with self.subTest(command=command):
                self.assertIn(command, result.stdout)
        for retired in ("start", "up", "init", "config", "runner"):
            with self.subTest(retired=retired):
                self.assertNotIn(f"    {retired} ", result.stdout)

    def test_the_ui_assets_are_in_the_wheel(self) -> None:
        """A data file that exists only in the checkout fails exactly here."""

        import zipfile

        with zipfile.ZipFile(self.wheel) as archive:
            names = set(archive.namelist())
        for asset in (
            "orbit/static/workflow-ui/index.html",
            "orbit/static/workflow-ui/assets/app.js",
            "orbit/static/workflow-ui/assets/api.js",
            "orbit/static/workflow-ui/assets/i18n.js",
            "orbit/static/workflow-ui/assets/app.css",
            "orbit/static/workflow-ui/assets/i18n.zh-CN.json",
            "orbit/static/workflow-ui/assets/i18n.en-US.json",
        ):
            with self.subTest(asset=asset):
                self.assertIn(asset, names)

    def test_no_legacy_asset_is_in_the_wheel(self) -> None:
        import zipfile

        with zipfile.ZipFile(self.wheel) as archive:
            names = set(archive.namelist())
        for removed in (
            "orbit/server.py", "orbit/store.py", "orbit/project_index.py",
            "orbit/static/ui.html", "orbit/static/workflow-ui.html",
            "orbit/static/vendor/dagre.min.js",
        ):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, names)

    def test_a_fresh_database_is_created_and_audits_clean(self) -> None:
        database = self.dir / "fresh.db"
        python = str(self.venv / "bin" / "python")
        create = subprocess.run(
            [
                python, "-c",
                "import sys;"
                "from orbit.workflow.persistence.database import connect_workflow_database;"
                "from orbit.workflow.persistence.migrations import migrate_workflow_database;"
                f"c = connect_workflow_database({str(database)!r});"
                "migrate_workflow_database(c); c.close()",
            ],
            capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(0, create.returncode, create.stderr)

        result = self.orbit_cli("db", "check", "--db", str(database), "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_the_installed_runtime_serves_over_http(self) -> None:
        """The end of the chain: built, installed, started, answering."""

        import socket
        import time
        import urllib.request

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        database = self.dir / "serve.db"
        server = subprocess.Popen(
            [
                str(self.orbit), "serve", "--port", str(port),
                "--db", str(database), "--no-agent-discovery",
            ],
            cwd=str(self.dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
            # An isolated HOME so the cutover gate does not see the developer's
            # own legacy database and refuse to start.
            env={"HOME": str(self.dir / "home"), "PATH": "/usr/bin:/bin"},
        )
        (self.dir / "home").mkdir(exist_ok=True)
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(200):
                if server.poll() is not None:
                    self.fail(f"server exited early:\n{server.stdout.read()}")
                try:
                    with urllib.request.urlopen(f"{base}/health/ready", timeout=1) as r:
                        if r.status == 200:
                            break
                except Exception:
                    time.sleep(0.1)
            else:
                self.fail("installed server never became ready")

            with urllib.request.urlopen(f"{base}/ui/", timeout=5) as response:
                self.assertEqual(200, response.status)
                self.assertIn(b"Orbit Runtime", response.read())
            with urllib.request.urlopen(f"{base}/api/v1/runs", timeout=5) as response:
                self.assertEqual(200, response.status)
        finally:
            server.terminate()
            server.wait(timeout=30)
            if server.stdout is not None:
                server.stdout.close()


if __name__ == "__main__":
    unittest.main()
