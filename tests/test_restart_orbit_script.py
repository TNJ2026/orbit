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

        # `ps -p N -o lstart=,command=` answers from a table the test wrote.
        # After `ORBIT_TEST_PS_SWITCH` calls it answers from the second table,
        # which is how a PID reused mid-run is modelled.
        ps_script = bin_dir / "ps"
        ps_script.write_text(
            "#!/bin/sh\n"
            'while [ "$#" -gt 0 ]; do\n'
            '  case "$1" in -p) shift; pid="$1";; esac\n'
            "  shift\n"
            "done\n"
            'calls=$(cat "$ORBIT_TEST_PS_CALLS" 2>/dev/null || echo 0)\n'
            'echo $((calls + 1)) > "$ORBIT_TEST_PS_CALLS"\n'
            'table="$ORBIT_TEST_PS"\n'
            'if [ -n "${ORBIT_TEST_PS_SWITCH:-}" ] '
            '&& [ "$calls" -ge "$ORBIT_TEST_PS_SWITCH" ]; then\n'
            '  table="$ORBIT_TEST_PS_AFTER"\n'
            "fi\n"
            # Non-zero for a PID it has no row for, the way the real `ps`
            # answers — a fake that always succeeds hides what `set -e` does
            # with the failure.
            'line=$(grep "^$pid " "$table") || exit 1\n'
            'printf "%s\\n" "$line" | cut -d" " -f2-\n',
            encoding="utf-8",
        )
        ps_script.chmod(0o755)
        self.write_ps_table(root / "ps.txt", ps_answers)
        self.write_ps_table(root / "ps-after.txt", ps_answers)

        # The start half is somebody else's; a stub keeps `exec` from failing.
        start = root / "scripts" / "start-orbit.sh"
        start.write_text("#!/bin/sh\necho started\n", encoding="utf-8")
        start.chmod(0o755)

        lsof = bin_dir / "lsof"
        lsof.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        lsof.chmod(0o755)

        return {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "AGENT_APP_STATE_DIR": str(root / "state"),
            "ORBIT_TEST_RUNTIMES": str(root / "runtimes.json"),
            "ORBIT_TEST_PS": str(root / "ps.txt"),
            "ORBIT_TEST_PS_AFTER": str(root / "ps-after.txt"),
            "ORBIT_TEST_PS_CALLS": str(root / "ps-calls.txt"),
        }

    @staticmethod
    def write_ps_table(path: Path, answers) -> None:
        """`lstart` then `command`, the two fields the script asks `ps` for."""

        path.write_text(
            "".join(
                f"{pid} Wed Sep  2 09:00:00 2026 {command}\n"
                for pid, command in answers.items()
            ),
            encoding="utf-8",
        )

    @staticmethod
    def reap_process(process: subprocess.Popen) -> None:
        """Stop a live test child and always collect its exit status."""

        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        process.wait()

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

    def test_a_pid_reused_during_the_wait_is_not_killed(self) -> None:
        """The gap between the check and the signal is up to 45 seconds long.

        A graceful stop can finish in that gap and the OS can hand the number
        straight to something else. Checking only that *a* process still exists
        before SIGKILL is how a restart script destroys an unrelated program,
        and a start time is what tells the two apart.
        """

        # A real process standing in for whoever got the number next, so an
        # existence check passes and only the identity check can save it.
        bystander = subprocess.Popen(["sleep", "30"])
        self.addCleanup(self.reap_process, bystander)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.environment(
                root, listed=[], recorded=bystander.pid,
                ps_answers={bystander.pid: "python -m orbit hub serve"},
            )
            # One `ps` call discovers it; every later call sees a stranger.
            self.write_ps_table(
                root / "ps-after.txt",
                {bystander.pid: "/usr/bin/postgres -D /var/lib/pg"},
            )
            environment["ORBIT_TEST_PS_SWITCH"] = "1"

            result = subprocess.run(
                ["bash", str(root / "restart-orbit.sh")], cwd=root, env=environment,
                text=True, capture_output=True, check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn(f"Stopping Orbit Hub (PID {bystander.pid})", result.stdout)
            self.assertIn("no longer the Orbit Hub", result.stderr)
            self.assertNotIn("Force stopping", result.stdout)
            self.assertIn("started", result.stdout)
            self.assertIsNone(
                bystander.poll(), "the process that inherited the PID was signalled",
            )

    def test_a_process_exiting_mid_run_does_not_end_the_restart(self) -> None:
        """`ps` exits non-zero for a PID that is gone, and `set -e` reads that.

        The graceful stop finishing between the check and the signal is the
        ordinary path, not an edge: the assignment from that command
        substitution failed, errexit ended the script where it stood — after
        the Hub was told to quit and before anything started it again — and
        the reported symptom was simply that Orbit did not come back.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.environment(
                root, listed=[{"pid": 8001}], recorded=8000,
                ps_answers={
                    8000: "python -m orbit hub serve",
                    8001: "python -m orbit serve --project-root /a",
                },
            )
            # Both exit while the run is still working through them.
            self.write_ps_table(root / "ps-after.txt", {})
            environment["ORBIT_TEST_PS_SWITCH"] = "2"

            result = subprocess.run(
                ["bash", str(root / "restart-orbit.sh")], cwd=root, env=environment,
                text=True, capture_output=True, check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("started", result.stdout, "the Hub must still be started")

    def test_a_process_that_stops_cleanly_is_not_forced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.environment(
                root, listed=[], recorded=7002,
                ps_answers={7002: "python -m orbit hub serve"},
            )
            # Gone after discovery: `ps` answers nothing for it.
            self.write_ps_table(root / "ps-after.txt", {})
            environment["ORBIT_TEST_PS_SWITCH"] = "1"

            result = subprocess.run(
                ["bash", str(root / "restart-orbit.sh")], cwd=root, env=environment,
                text=True, capture_output=True, check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertNotIn("Force stopping", result.stdout)
            self.assertNotIn("no longer", result.stderr)

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
