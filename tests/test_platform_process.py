"""M1B: child process control.

Replaces the process half of tests/test_worktree.py
(ProcessControlPortabilityTests, DescendantPidSnapshotTests) and the process
scenarios inside test_workflow_engine.py::AutoRunnerTests. No import of
orbit.server or orbit.store.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest

from orbit.platform import process


PY = sys.executable


class SnapshotTests(unittest.TestCase):
    def test_snapshot_sees_this_process(self) -> None:
        mapping = process.snapshot_ppids()
        self.assertTrue(mapping, "no ppid backend worked on this platform")
        self.assertIn(os.getpid(), mapping)
        self.assertEqual(os.getppid(), mapping[os.getpid()])

    def test_descendants_find_a_grandchild(self) -> None:
        # A child that spawns its own child: the grandchild is only reachable
        # through the snapshot, which is the whole point of taking one.
        script = (
            "import subprocess,sys,time;"
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
            "print(p.pid,flush=True);time.sleep(30)"
        )
        handle = process.ProcessHandle([PY, "-c", script])
        try:
            deadline = time.monotonic() + 10
            grandchild = None
            while time.monotonic() < deadline:
                text = handle.stdout.text
                if text.strip():
                    grandchild = int(text.split()[0])
                    break
                if not handle.stdout.chunks:
                    # drains start in wait(); poll the pipe directly here
                    line = handle._process.stdout.readline()
                    if line:
                        grandchild = int(line.split()[0])
                        break
                time.sleep(0.05)
            self.assertIsNotNone(grandchild, "child never reported its grandchild")
            descendants = process.descendant_pids(handle.pid)
            self.assertIn(grandchild, descendants)
        finally:
            handle.kill()
            handle.wait(timeout=5)

    def test_descendants_of_a_leaf_are_empty(self) -> None:
        handle = process.ProcessHandle([PY, "-c", "import time;time.sleep(5)"])
        try:
            self.assertEqual([], process.descendant_pids(handle.pid))
        finally:
            handle.kill()
            handle.wait(timeout=5)

    def test_unknown_pid_has_no_descendants(self) -> None:
        self.assertEqual([], process.descendant_pids(0))


class RunTests(unittest.TestCase):
    def test_captures_stdout_stderr_and_exit_code(self) -> None:
        result = process.run(
            [PY, "-c", "import sys;print('out');print('err',file=sys.stderr);sys.exit(3)"]
        )
        self.assertEqual(3, result.returncode)
        self.assertIn("out", result.stdout)
        self.assertIn("err", result.stderr)

    def test_stdin_is_delivered(self) -> None:
        result = process.run(
            [PY, "-c", "import sys;sys.stdout.write(sys.stdin.read().upper())"],
            stdin_text="hello",
        )
        self.assertIn("HELLO", result.stdout)

    def test_child_that_never_reads_stdin_does_not_deadlock(self) -> None:
        # Regression: a large stdin payload plus a child that ignores it used to
        # wedge the writer on a full pipe.
        result = process.run(
            [PY, "-c", "print('done')"], stdin_text="x" * 200_000, timeout=20
        )
        self.assertIn("done", result.stdout)

    def test_output_is_streamed_before_exit(self) -> None:
        seen = threading.Event()
        chunks: list[str] = []

        def on_stdout(chunk: str) -> None:
            chunks.append(chunk)
            seen.set()

        handle = process.ProcessHandle(
            [PY, "-u", "-c", "import time;print('early',flush=True);time.sleep(3)"],
            on_stdout=on_stdout,
        )
        thread = threading.Thread(target=handle.wait, kwargs={"timeout": 20})
        thread.start()
        try:
            self.assertTrue(seen.wait(10), "no output before process exit")
            self.assertIn("early", "".join(chunks))
        finally:
            handle.kill()
            thread.join(timeout=10)

    def test_timeout_kills_the_tree(self) -> None:
        result = process.run([PY, "-c", "import time;time.sleep(30)"], timeout=1)
        self.assertTrue(result.timed_out)
        self.assertNotEqual(0, result.returncode)

    def test_rejects_empty_argv(self) -> None:
        with self.assertRaises(ValueError):
            process.ProcessHandle([])
        with self.assertRaises(ValueError):
            process.ProcessHandle(["", "x"])


class OutputLimitTests(unittest.TestCase):
    def test_output_bomb_is_truncated_not_unbounded(self) -> None:
        result = process.run(
            [PY, "-c", "print('x'*100000)"], max_output_bytes=1000, timeout=30
        )
        self.assertTrue(result.stdout_truncated)
        self.assertLessEqual(len(result.stdout.encode()), 1000)

    def test_redactor_runs_before_capture(self) -> None:
        result = process.run(
            [PY, "-c", "print('token=SECRET123')"],
            redactor=lambda text: text.replace("SECRET123", "***"),
        )
        self.assertNotIn("SECRET123", result.stdout)
        self.assertIn("***", result.stdout)

    def test_buffer_clips_on_a_character_boundary(self) -> None:
        buffer = process.OutputBuffer(limit_bytes=4)
        buffer.append("你好")  # 3 bytes per char
        self.assertTrue(buffer.truncated)
        # Must not emit a partial codepoint.
        buffer.text.encode("utf-8")

    def test_sink_failure_does_not_break_capture(self) -> None:
        def explode(_chunk: str) -> None:
            raise RuntimeError("sink is down")

        result = process.run([PY, "-c", "print('still captured')"], on_stdout=explode)
        self.assertIn("still captured", result.stdout)


class CancellationTests(unittest.TestCase):
    def test_cancel_stops_a_long_child(self) -> None:
        handle = process.ProcessHandle([PY, "-c", "import time;time.sleep(30)"])
        thread = threading.Thread(target=handle.wait, kwargs={"timeout": 30})
        thread.start()
        time.sleep(0.3)
        handle.cancel(grace_seconds=3)
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertTrue(handle.cancelled)

    def test_cancel_from_another_thread_is_safe(self) -> None:
        handle = process.ProcessHandle([PY, "-c", "import time;time.sleep(30)"])
        errors: list[BaseException] = []

        def cancel() -> None:
            try:
                handle.cancel(grace_seconds=2)
            except BaseException as exc:  # noqa: BLE001 - recorded for assertion
                errors.append(exc)

        cancellers = [threading.Thread(target=cancel) for _ in range(4)]
        for thread in cancellers:
            thread.start()
        result = handle.wait(timeout=20)
        for thread in cancellers:
            thread.join(timeout=10)
        self.assertEqual([], errors)
        self.assertTrue(result.cancelled)

    def test_kill_unwedges_a_reader_blocked_by_an_escaped_child(self) -> None:
        # Regression from the legacy engine: a grandchild inherits the stdout
        # pipe and keeps it open, so the drain thread blocks on read long after
        # the direct child is gone. Killing must close our read end.
        script = (
            "import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
            "stdout=sys.stdout);"
            "time.sleep(30)"
        )
        handle = process.ProcessHandle([PY, "-c", script])
        done = threading.Event()

        def wait() -> None:
            handle.wait(timeout=25)
            done.set()

        thread = threading.Thread(target=wait)
        thread.start()
        time.sleep(0.5)
        handle.kill()
        self.assertTrue(done.wait(15), "wait() stayed wedged after kill")
        thread.join(timeout=5)


class ProcessTreeTests(unittest.TestCase):
    def test_kill_reaps_a_detached_grandchild(self) -> None:
        script = (
            "import subprocess,sys,os,time;"
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
            "start_new_session=True);"
            "print(p.pid,flush=True);time.sleep(30)"
        )
        handle = process.ProcessHandle([PY, "-u", "-c", script])
        grandchild = int(handle._process.stdout.readline().split()[0])
        handle.kill()
        handle.wait(timeout=10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
            except OSError:
                return  # reaped
            time.sleep(0.1)
        self.fail("detached grandchild survived kill")

    def test_terminate_and_kill_on_dead_pid_are_safe(self) -> None:
        self.assertFalse(process.terminate_pid_tree(0))
        self.assertFalse(process.kill_pid_tree(0))


class BoundaryTests(unittest.TestCase):
    def test_process_module_does_not_import_engine_or_domain(self) -> None:
        import ast
        from pathlib import Path

        tree = ast.parse(Path(process.__file__).read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if any(part in {"server", "store", "workflow"} for part in name.split(".")):
                    offenders.append(f"{node.lineno}:{name}")
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
