"""The adapter that turns a prompt-and-prose CLI into an Orbit Agent handler.

Every case here runs a real subprocess — a tiny Python script standing in for
the Agent CLI — because the thing under test *is* the process contract: which
argv the CLI sees, where the prompt arrives, and what comes back.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

from orbit.workflow.catalogs.agent_discovery import (
    TRUSTED_AGENT_CLIS, AgentCliSpec, AgentDiscoveryError, AgentInvocation,
    DiscoveredAgent, agent_manifest,
)
from orbit.workflow.domain.handlers import (
    CancelDisposition, HandlerValidationError, RecoveryDisposition,
    UnknownExternalResultError,
)
from orbit.workflow.handlers.agent import (
    AGENT_COMPLETION_MARKER, AGENT_RESULT_PORT, AGENT_RESULT_TEXT_KEY,
    AGENT_RUNTIME_COMPLETION_PROTOCOL, AgentRequest,
    TrustedPromptCliAgentClient, attempt_completion_marker, render_agent_prompt,
)

def runtime_prompt(value: str, attempt_id: str = "attempt-1") -> str:
    """What the Agent is given, including this attempt's own end token."""

    protocol = AGENT_RUNTIME_COMPLETION_PROTOCOL.replace(
        AGENT_COMPLETION_MARKER, attempt_completion_marker(attempt_id),
    )
    return f"{value}\n\n{protocol}"


def generic_prompt(value: str) -> str:
    """render_agent_prompt's own default, before a client mints an attempt."""

    return f"{value}\n\n{AGENT_RUNTIME_COMPLETION_PROTOCOL}"


# A stand-in CLI cannot hard-code the token it is meant to end with: it is
# minted per attempt. It reads it back out of the prompt it was handed, which
# is also the only proof the Agent was ever told what it is.
def echoing_marker(body: str) -> str:
    return f"""
import re, sys
_seen = " ".join(sys.argv[1:])
_found = re.search(r"ORBIT_RESULT_COMPLETE_[0-9a-f]+", _seen)
MARKER = _found.group(0) if _found else "MARKER_NEVER_SENT"
{body}
"""


def context(attempt_id: str = "attempt-1") -> SimpleNamespace:
    return SimpleNamespace(request=SimpleNamespace(attempt_id=attempt_id))


class FakeCli:
    """A script that reports how it was called, in place of a real Agent CLI."""

    def __init__(self, body: str) -> None:
        self.temp = tempfile.TemporaryDirectory()
        # The body goes in its own file rather than a heredoc: a heredoc would
        # occupy the child's stdin, which is one of the transports under test.
        script = Path(self.temp.name) / "body.py"
        script.write_text(body)
        self.path = Path(self.temp.name) / "fake-cli"
        self.path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        self.path.chmod(0o755)

    def cleanup(self) -> None:
        self.temp.cleanup()


ECHO_ARGV = """
import json, sys
print(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}))
"""


class PromptTransportTests(unittest.TestCase):
    def client(self, body: str, **kwargs) -> TrustedPromptCliAgentClient:
        cli = FakeCli(body)
        self.addCleanup(cli.cleanup)
        command = (str(cli.path), *kwargs.pop("args", ()))
        return TrustedPromptCliAgentClient(
            command, environment={"PATH": os.environ["PATH"]}, **kwargs
        )

    def call(self, client, node_input=None, config=None):
        response = client.execute(
            AgentRequest(node_input or {"prompt": "do the thing"}, config or {}, "key"),
            context(),
        )
        result = response.output[AGENT_RESULT_PORT]
        # The reply is carried under a key so it fills an object-typed port.
        return result[AGENT_RESULT_TEXT_KEY]

    def test_a_flag_carries_the_prompt_as_its_value(self) -> None:
        client = self.client(ECHO_ARGV, args=("chat", "-Q"), prompt_flag="-q")
        seen = json.loads(self.call(client))
        self.assertEqual(["chat", "-Q", "-q", runtime_prompt("do the thing")], seen["argv"])
        self.assertEqual("", seen["stdin"])

    def test_stdin_carries_the_prompt_when_no_flag_is_declared(self) -> None:
        client = self.client(ECHO_ARGV, args=("run",))
        seen = json.loads(self.call(client))
        self.assertEqual(["run"], seen["argv"])
        self.assertEqual(runtime_prompt("do the thing"), seen["stdin"])

    def test_a_positional_prompt_is_fenced_behind_a_double_dash(self) -> None:
        """A prompt that starts with a dash must stay data, not become a flag."""

        client = self.client(
            ECHO_ARGV, args=("exec",), prompt_positional=True,
        )
        seen = json.loads(self.call(client, {"prompt": "--version please"}))
        self.assertEqual(["exec", "--", runtime_prompt("--version please")], seen["argv"])

    def test_a_prompt_is_never_split_into_several_arguments(self) -> None:
        client = self.client(ECHO_ARGV, prompt_flag="-p")
        seen = json.loads(self.call(client, {"prompt": "one; rm -rf / && two"}))
        self.assertEqual(["-p", runtime_prompt("one; rm -rf / && two")], seen["argv"])

    def test_the_reply_is_returned_as_text(self) -> None:
        client = self.client(
            echoing_marker("print('  the answer  '); print(MARKER)"),
            prompt_flag="-p",
        )
        self.assertEqual("the answer", self.call(client))

    def test_a_completed_reply_survives_a_cli_that_hangs_after_output(self) -> None:
        started = time.monotonic()
        client = self.client(
            echoing_marker(
                "import time\n"
                "print('answer', flush=True); print(MARKER, flush=True)\n"
                "time.sleep(30)"
            ),
            prompt_flag="-p", timeout_seconds=20, kill_grace_seconds=.1,
        )
        self.assertEqual("answer", self.call(client))
        self.assertLess(time.monotonic() - started, 3)

    def test_a_standalone_marker_completes_even_when_output_follows_it(self) -> None:
        client = self.client(
            echoing_marker("print('answer'); print(MARKER); print('late output')"),
            prompt_flag="-p",
        )

        self.assertEqual("answer", self.call(client))

    def test_the_generic_marker_in_mid_answer_does_not_end_the_turn(self) -> None:
        """An Agent quoting the protocol used to cut itself off.

        The marker is a line the Agent has just read, so anything that makes
        it repeat its instructions printed the terminal line early: the
        process was killed there and the half-written reply was committed as
        the whole answer, recorded a success. The token is minted per attempt
        now, so the quoted one is the wrong one.
        """

        client = self.client(
            echoing_marker(
                "print('here is the protocol Orbit gave me:')\n"
                "print('ORBIT_RESULT_COMPLETE')\n"
                "print('and here is the rest of the answer')\n"
                "print(MARKER)"
            ),
            prompt_flag="-p",
        )

        answer = self.call(client)
        self.assertIn("and here is the rest of the answer", answer)
        self.assertIn("ORBIT_RESULT_COMPLETE", answer)

    def test_the_attempt_decides_the_token(self) -> None:
        """Two attempts, two tokens, each derived and not drawn at random."""

        first = attempt_completion_marker("attempt-1")
        self.assertEqual(first, attempt_completion_marker("attempt-1"))
        self.assertNotEqual(first, attempt_completion_marker("attempt-2"))
        self.assertTrue(first.startswith(f"{AGENT_COMPLETION_MARKER}_"))
        # The bare stem must not satisfy an attempt's own marker, which is the
        # whole reason for the suffix.
        self.assertNotEqual(AGENT_COMPLETION_MARKER, first)

    def test_a_complete_untruncated_marker_is_not_invalidated_by_drain_health(
        self,
    ) -> None:
        """Pipe cleanup is a capacity signal, not evidence about the reply."""

        from unittest.mock import patch
        from orbit.platform.process import ProcessResult

        client = self.client("print('unused')", prompt_flag="-p")
        outcome = ProcessResult(
            returncode=-15,
            stdout=f"answer\n{attempt_completion_marker('attempt-1')}\n",
            stderr="", stdout_truncated=False, stderr_truncated=False,
            cancelled=False, timed_out=False,
            termination_reason="completed_output", leaked_drain_threads=1,
        )
        with patch("orbit.workflow.handlers.agent.ProcessHandle") as handle_type:
            handle_type.return_value.wait.return_value = outcome
            self.assertEqual("answer", self.call(client))

    def test_an_internal_timeout_is_derived_from_the_attempt_budget(self) -> None:
        client = self.client(
            ECHO_ARGV, prompt_flag="-p", process_timeout_flag="--print-timeout",
            timeout_seconds=60, kill_grace_seconds=5,
        )
        seen = json.loads(self.call(client))
        self.assertRegex(seen["argv"][0], r"^--print-timeout=\d+s$")
        self.assertEqual("-p", seen["argv"][1])

    def test_an_oversized_prompt_is_refused_before_the_cli_runs(self) -> None:
        client = self.client(ECHO_ARGV, prompt_flag="-p", max_prompt_bytes=16)
        with self.assertRaises(HandlerValidationError):
            self.call(client, {"prompt": "x" * 17})

    def test_a_failing_cli_leaves_the_result_unknown(self) -> None:
        """It may already have acted, so a non-zero exit is not a clean failure."""

        client = self.client("import sys; sys.exit(3)", prompt_flag="-p")
        with self.assertRaises(UnknownExternalResultError):
            self.call(client)

    def test_a_silent_cli_is_not_a_successful_answer(self) -> None:
        """Exit 0 with nothing on stdout used to fill the result port with "".

        Unknown rather than failed: the CLI was handed a prompt and a
        workspace, and saying nothing is no evidence it did nothing.
        """

        client = self.client("pass", prompt_flag="-p")
        with self.assertRaises(UnknownExternalResultError):
            self.call(client)

    def test_whitespace_alone_is_silence_too(self) -> None:
        client = self.client("print('   \\n\\n  ')", prompt_flag="-p")
        with self.assertRaises(UnknownExternalResultError):
            self.call(client)

    def test_a_hanging_cli_is_killed_and_reported_as_unknown(self) -> None:
        client = self.client(
            "import time; time.sleep(30)", prompt_flag="-p", timeout_seconds=1,
        )
        with self.assertRaises(UnknownExternalResultError):
            self.call(client)

    def test_output_beyond_the_limit_is_refused(self) -> None:
        client = self.client("print('x' * 5000)", prompt_flag="-p", max_output_bytes=64)
        with self.assertRaises(HandlerValidationError):
            self.call(client)

    def test_a_cli_that_leaves_a_child_holding_the_pipes_still_returns(self) -> None:
        """The answer is in hand; nothing may wait on an EOF that never comes.

        Hermes exits but leaves an MCP gateway alive, and that survivor holds
        the write end of the pipes it inherited. Waiting for EOF — or closing
        a pipe a thread is parked in — parks the whole Handler until the lease
        expires, and an attempt that had already succeeded is written off as
        unsettled.
        """

        client = self.client(
            "\n".join((
                "import subprocess, sys",
                # The survivor: outlives its parent, holding stdout and stderr.
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])",
                "sys.stdout.write('the complete answer\\n'); sys.stdout.flush()",
                "sys.stderr.write('shutdown noise\\n'); sys.stderr.flush()",
            )),
            prompt_flag="-q",
            kill_grace_seconds=1,
        )
        started = time.monotonic()
        self.assertEqual("the complete answer", self.call(client).strip())
        self.assertLess(time.monotonic() - started, 20)

    def test_cancelling_an_idle_client_confirms_it_stopped(self) -> None:
        client = self.client(ECHO_ARGV, prompt_flag="-p")
        self.assertEqual(
            CancelDisposition.CONFIRMED_STOPPED, client.cancel("agent:none").disposition
        )

    def test_one_prompt_transport_at_a_time(self) -> None:
        with self.assertRaises(ValueError):
            TrustedPromptCliAgentClient(
                ("/usr/bin/true",), prompt_flag="-p", prompt_positional=True
            )

    def test_a_prompt_flag_must_be_a_flag(self) -> None:
        with self.assertRaises(ValueError):
            TrustedPromptCliAgentClient(("/usr/bin/true",), prompt_flag="exec")


class AgentPortContractTests(unittest.TestCase):
    """The manifest a workflow binds to and the reply a client returns.

    These two named the port differently — the manifest said `result`, the
    client answered `text` — so every prompt-CLI Agent ran perfectly and was
    then refused at completion, leaving the attempt to expire as unsettled.
    """

    def test_the_reply_is_an_object_so_it_fits_an_object_typed_port(self) -> None:
        """The result port is typed as an object; a bare string does not fit it.

        One Agent's output feeds the next Agent's object-typed input, so a bare
        string reaching it is rejected as "not of type object" — the node after
        the Agent that answered. The reply is carried under a key instead.
        """

        cli = FakeCli("print('the prose reply')")
        self.addCleanup(cli.cleanup)
        client = TrustedPromptCliAgentClient(
            (str(cli.path),), environment={"PATH": os.environ["PATH"]},
            prompt_flag="-q",
        )
        output = client.execute(
            AgentRequest({"prompt": "go"}, {}, "key"), context(),
        ).output
        self.assertEqual({AGENT_RESULT_PORT: {AGENT_RESULT_TEXT_KEY: "the prose reply"}}, output)
        self.assertIsInstance(output[AGENT_RESULT_PORT], dict)

    def test_the_client_answers_on_the_port_the_manifest_declares(self) -> None:
        agent = DiscoveredAgent(
            AgentCliSpec("claude", "claude", invocation=AgentInvocation(prompt_flag="-p")),
            "/usr/local/bin/claude", "1.0.0",
        )
        self.assertEqual({AGENT_RESULT_PORT}, set(agent_manifest(agent).outputs))


class _FakeArtifacts:
    """A minimal CAS stand-in: write returns an id, read returns the bytes."""

    def __init__(self):
        self.blobs = {}
        self.writes = []

    def write(self, *, name, content, content_type):
        from orbit.workflow.domain.ids import EntityId

        artifact_id = EntityId("artifact", f"{name}-{len(self.blobs)}")
        self.blobs[str(artifact_id)] = content
        self.writes.append((name, content_type, len(content)))
        return artifact_id

    def read(self, artifact_id, *, max_size_bytes=None):
        return self.blobs[str(artifact_id)]


def _port(port_id, *, transport="inline", max_size_bytes=1_000_000, content_types=("text/plain",)):
    policy = {"transport": transport, "max_size_bytes": max_size_bytes,
              "content_types": list(content_types) if transport == "artifact_ref" else [],
              "visibility": "run" if transport == "artifact_ref" else None}
    return {"id": port_id, "schema_id": "x://o/1.0", "required": True,
            "has_default": False, "default": None, "description": "",
            "data_policy": policy}


def _routing_context(*, output_ports=(), input_ports=(), artifacts=None):
    return SimpleNamespace(
        request=SimpleNamespace(
            attempt_id="attempt-1", output_ports=tuple(output_ports),
            input_ports=tuple(input_ports),
        ),
        artifacts=artifacts,
    )


class AgentArtifactRoutingTests(unittest.TestCase):
    """The result port's transport decides where a reply goes."""

    def client(self, body, **kwargs):
        cli = FakeCli(body)
        self.addCleanup(cli.cleanup)
        return TrustedPromptCliAgentClient(
            (str(cli.path),), environment={"PATH": os.environ["PATH"]}, **kwargs,
        )

    def test_an_artifact_result_port_stages_the_reply_and_returns_a_reference(self) -> None:
        artifacts = _FakeArtifacts()
        client = self.client("print('the long report')", prompt_flag="-q")
        response = client.execute(
            AgentRequest({"prompt": "go"}, {}, "key"),
            _routing_context(
                output_ports=[_port("result", transport="artifact_ref")],
                artifacts=artifacts,
            ),
        )
        artifact_id = response.output[AGENT_RESULT_PORT]["artifact_id"]
        self.assertEqual((artifact_id,), tuple(str(r) for r in response.artifact_refs))
        self.assertEqual(b"the long report", artifacts.blobs[artifact_id])
        self.assertEqual([("result", "text/plain", 15)], artifacts.writes)

    def test_an_inline_result_port_keeps_the_text_envelope(self) -> None:
        client = self.client("print('short')", prompt_flag="-q")
        response = client.execute(
            AgentRequest({"prompt": "go"}, {}, "key"),
            _routing_context(output_ports=[_port("result")], artifacts=_FakeArtifacts()),
        )
        self.assertEqual({AGENT_RESULT_PORT: {AGENT_RESULT_TEXT_KEY: "short"}}, response.output)
        self.assertEqual((), response.artifact_refs)

    def test_an_artifact_input_is_resolved_to_text_before_the_prompt(self) -> None:
        artifacts = _FakeArtifacts()
        from orbit.workflow.domain.ids import EntityId

        artifacts.blobs[str(EntityId("artifact", "up-0"))] = b"upstream prose"
        # Echo argv so we can see what prompt the CLI received.
        client = self.client(ECHO_ARGV, prompt_flag="-q")
        response = client.execute(
            AgentRequest({"prompt": {"artifact_id": str(EntityId("artifact", "up-0"))}}, {}, "key"),
            _routing_context(
                input_ports=[_port("prompt", transport="artifact_ref")],
                artifacts=artifacts,
            ),
        )
        seen = json.loads(response.output[AGENT_RESULT_PORT][AGENT_RESULT_TEXT_KEY])
        self.assertEqual(["-q", runtime_prompt("upstream prose")], seen["argv"])

    def test_a_large_prompt_via_a_flag_is_refused_with_a_hint(self) -> None:
        from orbit.workflow.domain.ids import EntityId

        artifacts = _FakeArtifacts()
        artifacts.blobs[str(EntityId("artifact", "big-0"))] = b"y" * 500_000
        client = self.client("print('x')", prompt_flag="-q")
        with self.assertRaises(HandlerValidationError) as raised:
            client.execute(
                AgentRequest({"prompt": {"artifact_id": str(EntityId("artifact", "big-0"))}}, {}, "key"),
                _routing_context(
                    input_ports=[_port("prompt", transport="artifact_ref")],
                    artifacts=artifacts,
                ),
            )
        self.assertIn("argument", str(raised.exception))

    def test_a_large_prompt_via_stdin_is_allowed_up_to_the_input_budget(self) -> None:
        from orbit.workflow.domain.ids import EntityId

        artifacts = _FakeArtifacts()
        artifacts.blobs[str(EntityId("artifact", "big-0"))] = b"z" * 500_000
        client = self.client("import sys; sys.stdin.read(); print('ok')")  # stdin transport
        response = client.execute(
            AgentRequest({"prompt": {"artifact_id": str(EntityId("artifact", "big-0"))}}, {}, "key"),
            _routing_context(
                input_ports=[_port("prompt", transport="artifact_ref")],
                artifacts=artifacts,
            ),
        )
        self.assertEqual("ok", response.output[AGENT_RESULT_PORT][AGENT_RESULT_TEXT_KEY])


class PromptRenderingTests(unittest.TestCase):
    def test_a_string_input_is_the_prompt(self) -> None:
        self.assertEqual(generic_prompt("go"), render_agent_prompt({"prompt": "go"}, {}))

    def test_a_structured_input_is_rendered_as_stable_json(self) -> None:
        rendered = render_agent_prompt({"prompt": {"b": 2, "a": 1}}, {})
        self.assertEqual(generic_prompt('{"a": 1, "b": 2}'), rendered)

    def test_an_authored_preamble_precedes_the_runtime_value(self) -> None:
        rendered = render_agent_prompt({"prompt": "x"}, {"prompt": "You summarize."})
        self.assertTrue(rendered.startswith("You summarize."))
        self.assertIn("INPUT-BEGIN\nx\nINPUT-END", rendered)
        self.assertTrue(rendered.endswith(AGENT_RUNTIME_COMPLETION_PROTOCOL))

    def test_an_input_without_a_prompt_port_is_rendered_whole(self) -> None:
        self.assertEqual(generic_prompt('{"value": 3}'), render_agent_prompt({"value": 3}, {}))


class InvocationSpecTests(unittest.TestCase):
    def test_every_trusted_cli_declares_how_it_is_invoked(self) -> None:
        """A spec without an invocation is detect-only, and none should be left."""

        for spec in TRUSTED_AGENT_CLIS:
            with self.subTest(agent=spec.name):
                self.assertIsNotNone(spec.invocation, spec.name)
                self.assertTrue(spec.runtime_compatible)

    def test_antigravity_headless_mode_auto_approves_tools(self) -> None:
        spec = next(item for item in TRUSTED_AGENT_CLIS if item.name == "antigravity")
        self.assertEqual(
            ("--dangerously-skip-permissions",),
            spec.invocation.args,
        )

    def test_an_argument_that_is_not_a_plain_token_is_refused(self) -> None:
        for argument in ("$(whoami)", "a b", "; rm -rf /", "`id`", "|tee"):
            with self.subTest(argument=argument):
                with self.assertRaises(AgentDiscoveryError):
                    AgentInvocation(args=(argument,))

    def test_a_spec_may_pass_a_prompt_exactly_one_way(self) -> None:
        with self.assertRaises(AgentDiscoveryError):
            AgentInvocation(prompt_flag="-p", prompt_positional=True)


if __name__ == "__main__":
    unittest.main()


class WorkspaceNameTests(unittest.TestCase):
    """A run id becomes one path segment under the workspace root.

    It arrives as an identifier the Runtime minted, but the directory is where
    an Agent is then let loose, so the name it is given has to be one segment
    and no way out of the root.
    """

    def test_an_ordinary_run_id_keeps_its_shape(self) -> None:
        from orbit.workflow.handlers.agent import _safe_name

        self.assertEqual(
            "langgraph_run_abc123", _safe_name("langgraph_run:abc123"),
        )

    def test_separators_and_traversal_cannot_survive(self) -> None:
        from orbit.workflow.handlers.agent import _safe_name

        for value in ("../../etc/passwd", "a/b", "a\\b", "..", ".", "..."):
            with self.subTest(value=value):
                name = _safe_name(value)
                self.assertNotIn("/", name)
                self.assertNotIn("\\", name)
                self.assertFalse(name.startswith("."))
                self.assertNotEqual("..", name)

    def test_a_name_with_nothing_usable_left_still_names_something(self) -> None:
        from orbit.workflow.handlers.agent import _safe_name

        for value in ("", "...", "///"):
            with self.subTest(value=value):
                self.assertTrue(_safe_name(value))


class CancelBeforeSpawnTests(unittest.TestCase):
    """Cancelling an attempt that has not started a process yet.

    The window is real: the Runtime can decide to stop an attempt between the
    adapter being asked to execute and the CLI actually being spawned. A
    request that arrived in that window has to be remembered, or the process
    starts after the cancellation and runs to completion unwatched.
    """

    def client(self, body: str, **kwargs) -> TrustedPromptCliAgentClient:
        cli = FakeCli(body)
        self.addCleanup(cli.cleanup)
        return TrustedPromptCliAgentClient(
            (str(cli.path),), environment={"PATH": os.environ["PATH"]}, **kwargs,
        )

    def test_a_request_before_spawn_is_remembered_and_refuses_the_attempt(
        self,
    ) -> None:
        client = self.client(
            echoing_marker("print('answer'); print(MARKER)"), prompt_flag="-p",
        )
        reference = "agent:attempt-1"

        acknowledgement = client.request_cancel(reference)
        self.assertEqual(CancelDisposition.UNKNOWN, acknowledgement.disposition)
        self.assertIn("before spawn", acknowledgement.message)

        # The attempt that follows must not quietly succeed.
        with self.assertRaises(UnknownExternalResultError):
            client.execute(
                AgentRequest({"prompt": "go"}, {}, "key"), context("attempt-1"),
            )

    def test_clearing_the_request_lets_the_next_attempt_run(self) -> None:
        client = self.client(
            echoing_marker("print('answer'); print(MARKER)"), prompt_flag="-p",
        )
        reference = "agent:attempt-1"
        client.request_cancel(reference)
        client.clear_cancel_request(reference)

        response = client.execute(
            AgentRequest({"prompt": "go"}, {}, "key"), context("attempt-1"),
        )
        self.assertEqual(
            "answer", response.output[AGENT_RESULT_PORT][AGENT_RESULT_TEXT_KEY],
        )


class RecoveryTests(unittest.TestCase):
    """What a restarted Runtime does about a process it may have left running."""

    def client(self) -> TrustedPromptCliAgentClient:
        cli = FakeCli("print('x')")
        self.addCleanup(cli.cleanup)
        return TrustedPromptCliAgentClient(
            (str(cli.path),), environment={"PATH": os.environ["PATH"]},
        )

    def test_an_unreadable_reference_is_still_an_unknown_outcome(self) -> None:
        """Never `not found`: the process may have acted before we lost it."""

        # An empty reference never reaches here — `RecoveryResult` refuses to
        # carry one — so the cases are the references that are readable and say
        # nothing this adapter can act on.
        for reference in ("not json", '{"kind": "other"}', "null",
                          '{"kind": "local_process_v1", "pid": true, "identity": "x"}'):
            with self.subTest(reference=reference):
                result = self.client().recover(reference)
                self.assertEqual(RecoveryDisposition.UNKNOWN, result.disposition)

    def test_a_reference_naming_this_process_is_not_acted_on_blindly(self) -> None:
        """A pid is reused; the identity is what says it is the same process."""

        reference = json.dumps({
            "kind": "local_process_v1", "pid": os.getpid(),
            "identity": "proc:definitely-not-this-one",
        })
        result = self.client().recover(reference)
        self.assertEqual(RecoveryDisposition.UNKNOWN, result.disposition)
        self.assertEqual(reference, result.provider_request_id)
        # Still here: the identity did not match, so nothing was signalled.
        self.assertTrue(os.getpid())
