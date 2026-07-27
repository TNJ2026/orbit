"""What happens to an Agent CLI's descendants when the CLI has to be stopped.

Every case runs real processes. The thing under test is not what the CLI
prints but whether the tree it started is gone afterwards: a killed CLI that
leaves a test suite running keeps writing to the workspace long after the
Runtime has written the attempt off, which is the failure this suite exists
to prevent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

from orbit.workflow.domain.handlers import UnknownExternalResultError
from orbit.workflow.handlers.agent import AgentRequest, TrustedPromptCliAgentClient


def context(attempt_id: str = "attempt-1", deadline: datetime | None = None):
    request = SimpleNamespace(attempt_id=attempt_id)
    if deadline is not None:
        request.deadline = deadline
    return SimpleNamespace(request=request)


class Cli:
    """A Python script standing in for an installed Agent CLI."""

    def __init__(self, body: str) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.pid_file = Path(self.temp.name) / "child.pid"
        script = Path(self.temp.name) / "body.py"
        script.write_text(body)
        self.path = Path(self.temp.name) / "fake-cli"
        self.path.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n'
        )
        self.path.chmod(0o755)

    def cleanup(self) -> None:
        self.temp.cleanup()


# The shape of the reported failure: a CLI that hangs while a process it
# started holds the inherited stdout open. Killing only the CLI leaves that
# grandchild running and the pipe unclosed, so the reader thread never sees
# EOF and the Handler has nothing to return.
HANGS_WITH_A_CHILD_HOLDING_STDOUT = """
import os, subprocess, sys, time

child = subprocess.Popen([sys.executable, "-c", (
    "import os, sys, time;"
    "open(os.environ['PID_FILE'], 'w').write(str(os.getpid()));"
    "time.sleep(120)"
)])
# stdout is inherited, so the child holds the write end of the same pipe.
time.sleep(120)
"""

# Same, but the CLI refuses SIGTERM. Only the group SIGKILL ends it.
IGNORES_SIGTERM = """
import os, signal, sys, time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
open(os.environ['PID_FILE'], 'w').write(str(os.getpid()))
time.sleep(120)
"""

# A child that leaves the group with setsid: the pre-kill descendant snapshot
# is the only thing that can still find it.
ESCAPES_THE_GROUP = """
import os, subprocess, sys, time

child = subprocess.Popen([sys.executable, "-c", (
    "import os, signal, sys, time;"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
    "open(os.environ['PID_FILE'], 'w').write(str(os.getpid()));"
    "time.sleep(120)"
)], start_new_session=True)
time.sleep(120)
"""


def alive(pid: int) -> bool:
    """Whether a pid still exists, ignoring whether it has been reaped."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class AgentProcessTreeTests(unittest.TestCase):
    def client(self, body: str, **kwargs) -> TrustedPromptCliAgentClient:
        cli = Cli(body)
        self.addCleanup(cli.cleanup)
        self.cli = cli
        kwargs.setdefault("timeout_seconds", 1)
        kwargs.setdefault("kill_grace_seconds", 1)
        return TrustedPromptCliAgentClient(
            (str(cli.path),), prompt_flag="-p",
            environment={
                "PATH": os.environ["PATH"], "PID_FILE": str(cli.pid_file),
            },
            **kwargs,
        )

    def run_until_timeout(self, client, **kwargs) -> None:
        with self.assertRaises(UnknownExternalResultError):
            client.execute(
                AgentRequest({"prompt": "work"}, {}, "key"), context(**kwargs),
            )

    def recorded_pid(self, timeout: float = 10.0) -> int:
        """The descendant's pid, once it has announced itself."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                text = self.cli.pid_file.read_text().strip()
            except (FileNotFoundError, OSError):
                text = ""
            if text:
                return int(text)
            time.sleep(0.05)
        self.fail("the CLI's descendant never recorded its pid")

    def assert_gone(self, pid: int, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not alive(pid):
                return
            time.sleep(0.05)
        os.kill(pid, signal.SIGKILL)  # do not leak the process out of the suite
        self.fail(f"pid {pid} survived the Agent's termination")

    def test_a_timeout_kills_the_child_holding_the_inherited_pipe(self) -> None:
        """The reported failure: killing the CLI alone leaves the work running.

        The grandchild inherits stdout, so terminating only the CLI leaves the
        pipe open and the reader parked forever. Signalling the group ends
        both, and the Handler gets its timeout back instead of hanging.
        """

        client = self.client(HANGS_WITH_A_CHILD_HOLDING_STDOUT)

        # The pid is read afterwards: the CLI only starts inside execute(), and
        # the file it writes outlives it.
        self.run_until_timeout(client)

        self.assert_gone(self.recorded_pid())
        self.assertEqual(0, client.leaked_reader_threads)

    def test_a_cli_that_ignores_sigterm_is_still_stopped(self) -> None:
        client = self.client(IGNORES_SIGTERM)

        self.run_until_timeout(client)

        self.assert_gone(self.recorded_pid())

    def test_a_descendant_that_left_the_group_is_still_found(self) -> None:
        """`setsid` escapes the group; the pre-kill snapshot does not."""

        client = self.client(ESCAPES_THE_GROUP)

        self.run_until_timeout(client)

        self.assert_gone(self.recorded_pid())

    def test_cancelling_stops_the_whole_tree(self) -> None:
        """Cancel takes the same path as timeout, from another thread."""

        from threading import Thread

        client = self.client(HANGS_WITH_A_CHILD_HOLDING_STDOUT, timeout_seconds=60)
        child_pid: list[int] = []

        def cancel_once_started() -> None:
            child_pid.append(self.recorded_pid())
            client.cancel("agent:attempt-1")

        canceller = Thread(target=cancel_once_started, daemon=True)
        canceller.start()
        self.run_until_timeout(client)
        canceller.join(timeout=15)

        self.assertTrue(child_pid, "the canceller never saw the child start")
        self.assert_gone(child_pid[0])


class AttemptDeadlineTests(unittest.TestCase):
    """The adapter waits for *this* attempt, not for every attempt alike."""

    def client(self, **kwargs) -> TrustedPromptCliAgentClient:
        cli = Cli(IGNORES_SIGTERM)
        self.addCleanup(cli.cleanup)
        self.cli = cli
        return TrustedPromptCliAgentClient(
            (str(cli.path),), prompt_flag="-p",
            environment={
                "PATH": os.environ["PATH"], "PID_FILE": str(cli.pid_file),
            },
            **kwargs,
        )

    def test_the_request_deadline_shortens_the_constructor_timeout(self) -> None:
        """A node budgeted for one second must not wait the registry's hour.

        Without this the CLI is still running when the Runtime has already
        settled the attempt, and every timeout arrives as a late result.
        """

        client = self.client(timeout_seconds=3600, kill_grace_seconds=1)
        deadline = datetime.now(timezone.utc) + timedelta(seconds=1)

        started = time.monotonic()
        with self.assertRaises(UnknownExternalResultError):
            client.execute(
                AgentRequest({"prompt": "work"}, {}, "key"),
                context(deadline=deadline),
            )

        self.assertLess(time.monotonic() - started, 30)

    def test_the_constructor_timeout_still_caps_a_distant_deadline(self) -> None:
        """The deadline shortens the wait; it never extends it."""

        client = self.client(timeout_seconds=1, kill_grace_seconds=1)
        deadline = datetime.now(timezone.utc) + timedelta(hours=1)

        started = time.monotonic()
        with self.assertRaises(UnknownExternalResultError):
            client.execute(
                AgentRequest({"prompt": "work"}, {}, "key"),
                context(deadline=deadline),
            )

        self.assertLess(time.monotonic() - started, 30)

    def test_a_context_without_a_deadline_keeps_the_constructor_value(self) -> None:
        """Callers that predate per-attempt deadlines must keep working."""

        client = self.client(timeout_seconds=1, kill_grace_seconds=1)

        with self.assertRaises(UnknownExternalResultError):
            client.execute(AgentRequest({"prompt": "work"}, {}, "key"), context())


if __name__ == "__main__":
    unittest.main()
