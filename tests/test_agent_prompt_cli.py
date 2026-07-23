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
    CancelDisposition, HandlerValidationError, UnknownExternalResultError,
)
from orbit.workflow.handlers.agent import (
    AGENT_RESULT_PORT, AGENT_RESULT_TEXT_KEY, AgentRequest,
    TrustedPromptCliAgentClient, render_agent_prompt,
)


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
        self.assertEqual(["chat", "-Q", "-q", "do the thing"], seen["argv"])
        self.assertEqual("", seen["stdin"])

    def test_stdin_carries_the_prompt_when_no_flag_is_declared(self) -> None:
        client = self.client(ECHO_ARGV, args=("run",))
        seen = json.loads(self.call(client))
        self.assertEqual(["run"], seen["argv"])
        self.assertEqual("do the thing", seen["stdin"])

    def test_a_positional_prompt_is_fenced_behind_a_double_dash(self) -> None:
        """A prompt that starts with a dash must stay data, not become a flag."""

        client = self.client(
            ECHO_ARGV, args=("exec",), prompt_positional=True,
        )
        seen = json.loads(self.call(client, {"prompt": "--version please"}))
        self.assertEqual(["exec", "--", "--version please"], seen["argv"])

    def test_a_prompt_is_never_split_into_several_arguments(self) -> None:
        client = self.client(ECHO_ARGV, prompt_flag="-p")
        seen = json.loads(self.call(client, {"prompt": "one; rm -rf / && two"}))
        self.assertEqual(["-p", "one; rm -rf / && two"], seen["argv"])

    def test_the_reply_is_returned_as_text(self) -> None:
        client = self.client("print('  the answer  ')", prompt_flag="-p")
        self.assertEqual("the answer", self.call(client))

    def test_an_oversized_prompt_is_refused_before_the_cli_runs(self) -> None:
        client = self.client(ECHO_ARGV, prompt_flag="-p", max_prompt_bytes=16)
        with self.assertRaises(HandlerValidationError):
            self.call(client, {"prompt": "x" * 17})

    def test_a_failing_cli_leaves_the_result_unknown(self) -> None:
        """It may already have acted, so a non-zero exit is not a clean failure."""

        client = self.client("import sys; sys.exit(3)", prompt_flag="-p")
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


class PromptRenderingTests(unittest.TestCase):
    def test_a_string_input_is_the_prompt(self) -> None:
        self.assertEqual("go", render_agent_prompt({"prompt": "go"}, {}))

    def test_a_structured_input_is_rendered_as_stable_json(self) -> None:
        rendered = render_agent_prompt({"prompt": {"b": 2, "a": 1}}, {})
        self.assertEqual('{"a": 1, "b": 2}', rendered)

    def test_an_authored_preamble_precedes_the_runtime_value(self) -> None:
        rendered = render_agent_prompt({"prompt": "x"}, {"prompt": "You summarize."})
        self.assertTrue(rendered.startswith("You summarize."))
        self.assertIn("INPUT-BEGIN\nx\nINPUT-END", rendered)

    def test_an_input_without_a_prompt_port_is_rendered_whole(self) -> None:
        self.assertEqual('{"value": 3}', render_agent_prompt({"value": 3}, {}))


class InvocationSpecTests(unittest.TestCase):
    def test_every_trusted_cli_declares_how_it_is_invoked(self) -> None:
        """A spec without an invocation is detect-only, and none should be left."""

        for spec in TRUSTED_AGENT_CLIS:
            with self.subTest(agent=spec.name):
                self.assertIsNotNone(spec.invocation, spec.name)
                self.assertTrue(spec.runtime_compatible)

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
