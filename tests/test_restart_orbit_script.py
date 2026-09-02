"""`restart-orbit.sh` stops what it can prove is Orbit's, then hands over.

The dangerous half of a restart script is the stopping: a PID from a record is
a number that *was* a process, and the OS reuses them. These drive the script
against fabricated records and a fake `ps` so the identity check is exercised
without signalling anything.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "restart-orbit.sh"


class RestartOrbitScriptTests(unittest.TestCase):
    def environment(self, root: Path, *, listed, recorded, ps_answers):
        """A copy of the script with fake `uv`, `ps` and `lsof` around it.

        Copied rather than run in place so it resolves the CLI through `uv`
        instead of this checkout's `.venv`, which would answer about the
        machine actually running the tests.
        """

        bin_dir = root / "bin"
        bin_dir.mkdir()
        (root / "scripts").mkdir()
        (root / "restart-orbit.sh").write_text(
            SCRIPT.read_text(encoding="utf-8"), encoding="utf-8",
        )
        (root / "agent-app.json").write_text(
            json.dumps({"service": {"ready_url": "http://127.0.0.1:8848/health/ready"}}),
            encoding="utf-8",
        )
        (root / "state" / "orbit" / "global").mkdir(parents=True)
        (root / "state" / "orbit" / "global" / "pid.json").write_text(
            json.dumps({"pid": recorded, "app_id": "orbit"}), encoding="utf-8",
        )

        uv = bin_dir / "uv"
        uv.write_text(
            "#!/bin/sh\ncat \"$ORBIT_TEST_RUNTIMES\"\n", encoding="utf-8",
        )
        uv.chmod(0o755)
        (root / "runtimes.json").write_text(json.dumps(listed), encoding="utf-8")

        # `ps -p N -o command=` answers from a table the test wrote.
        ps_script = bin_dir / "ps"
        ps_script.write_text(
            "#!/bin/sh\n"
            'while [ "$#" -gt 0 ]; do\n'
            '  case "$1" in -p) shift; pid="$1";; esac\n'
            "  shift\n"
            "done\n"
            'grep "^$pid " "$ORBIT_TEST_PS" | cut -d" " -f2- || true\n',
            encoding="utf-8",
        )
        ps_script.chmod(0o755)
        (root / "ps.txt").write_text(
            "".join(f"{pid} {command}\n" for pid, command in ps_answers.items()),
            encoding="utf-8",
        )

        lsof = bin_dir / "lsof"
        lsof.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        lsof.chmod(0o755)

        return {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "AGENT_APP_STATE_DIR": str(root / "state"),
            "ORBIT_TEST_RUNTIMES": str(root / "runtimes.json"),
            "ORBIT_TEST_PS": str(root / "ps.txt"),
        }

    def run_dry(self, environment, root: Path):
        return subprocess.run(
            ["bash", str(root / "restart-orbit.sh"), "--dry-run"],
            cwd=root, env=environment, text=True, capture_output=True, check=False,
        )

    def test_a_reused_pid_is_reported_and_left_alone(self) -> None:
        """The record says Runtime; the OS says something else entirely."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.environment(
                root,
                listed=[{"pid": 4242, "project_root": "/tmp/ws"}],
                recorded=4243,
                ps_answers={
                    4242: "/usr/bin/postgres -D /var/lib/postgres",
                    4243: "python -m orbit hub serve",
                },
            )

            result = self.run_dry(environment, root)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertNotIn("4242", result.stdout)
            self.assertIn("skipping PID 4242", result.stderr)
            self.assertIn("Would stop Orbit Hub (PID 4243)", result.stdout)

    def test_the_hub_is_stopped_before_the_runtimes_it_would_relaunch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.environment(
                root,
                listed=[{"pid": 5001}, {"pid": 5002}],
                recorded=5000,
                ps_answers={
                    5000: "python -m orbit hub serve --port 8848",
                    5001: "python -m orbit serve --project-root /a",
                    5002: "python -m orbit serve --project-root /b",
                },
            )

            lines = [
                line for line in self.run_dry(environment, root).stdout.splitlines()
                if line.startswith("Would stop")
            ]

            self.assertEqual(3, len(lines), lines)
            self.assertIn("Orbit Hub", lines[0])

    def test_compact_json_is_read_as_a_document_not_scraped(self) -> None:
        """A pattern expecting one field per line finds nothing when it is not."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.environment(
                root, listed=[{"pid": 6001}], recorded=6000,
                ps_answers={
                    6000: "python -m orbit hub serve",
                    6001: "python -m orbit serve --project-root /a",
                },
            )
            # One line, no spaces: what `json.dumps` gives by default.
            (root / "runtimes.json").write_text(
                json.dumps([{"pid": 6001}], separators=(",", ":")), encoding="utf-8",
            )

            self.assertIn(
                "Would stop Orbit Runtime (PID 6001)",
                self.run_dry(environment, root).stdout,
            )


if __name__ == "__main__":
    unittest.main()
