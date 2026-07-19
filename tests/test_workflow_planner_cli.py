"""TrustedCliPlannerProvider: error mapping, wiring, and the real process port.

The provider's contract is mostly about what it does when things go wrong: a
timeout must surface as *unknown* (the model may have been called and billed),
a missing binary as *permanent* (retrying will not install it), a bad exit as
*transient*. Each mapping gets a test, because each one licenses a different
caller behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from orbit.workflow.catalogs.agent_discovery import AgentCliSpec, DiscoveredAgent
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.versions import Revision
from orbit.workflow.planner import TrustedCliPlannerProvider, build_planning_context
from orbit.workflow.planner.provider import (
    PlannerPermanentError, PlannerProvider, PlannerTransientError,
    PlannerUnknownResultError,
)

NOW = datetime(2026, 7, 19, 12, tzinfo=timezone.utc)


def planning_context():
    return build_planning_context(
        run_id=EntityId("run", "cli"), plan_version=Revision(1), goal="finish",
        graph_summary={"status": "running", "plan_version": 1, "nodes": [],
                       "tokens": [], "joins": [], "waiting_reason": None},
        available_data=[], available_capabilities=["finish"],
        remaining_limits={"decisions": 3},
    )


@dataclass
class FakeOutcome:
    returncode: int | None = 0
    stdout: str = '{"plan": []}'
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    cancelled: bool = False
    timed_out: bool = False


class RecordingRunner:
    def __init__(self, outcome=None, error=None):
        self.outcome, self.error, self.calls = outcome or FakeOutcome(), error, []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, **kwargs})
        if self.error is not None:
            raise self.error
        return self.outcome


class CliProviderContractTest(unittest.TestCase):
    def provider(self, runner, **kwargs):
        return TrustedCliPlannerProvider(["planner-cli"], runner=runner, **kwargs)

    def generate(self, runner, **kwargs):
        return self.provider(runner, **kwargs).generate(
            planning_context(), model_id="m1", request_fingerprint="fp-1"
        )

    def test_satisfies_the_provider_port(self):
        self.assertIsInstance(self.provider(RecordingRunner()), PlannerProvider)

    def test_happy_path_returns_raw_text_and_incomplete_usage(self):
        runner = RecordingRunner(FakeOutcome(stdout='{"plan": []}'))
        response = self.generate(runner)
        self.assertEqual(response.raw_response, '{"plan": []}')
        self.assertIsNone(response.provider_request_id)
        # Usage is marked incomplete on purpose: the CLI reports no token
        # counts, and a zero that looks authoritative would corrupt budgets.
        self.assertTrue(response.usage.incomplete)

    def test_child_receives_versioned_payload_and_nothing_via_argv(self):
        runner = RecordingRunner()
        self.generate(runner)
        call = runner.calls[0]
        self.assertEqual(call["argv"], ["planner-cli"])
        payload = json.loads(call["stdin_text"])
        self.assertEqual(payload["schema_version"], "1.0")
        self.assertEqual(payload["model_id"], "m1")
        self.assertEqual(payload["request_fingerprint"], "fp-1")
        self.assertEqual(payload["context"]["goal"], "finish")

    def test_environment_is_the_explicit_allowlist_not_the_inherited_shell(self):
        runner = RecordingRunner()
        self.generate(runner)
        self.assertEqual(set(runner.calls[0]["env"]), {"PATH", "HOME"})

    def test_bounds_are_forwarded_to_the_process_port(self):
        runner = RecordingRunner()
        self.generate(runner, timeout_seconds=7, max_response_bytes=1024)
        call = runner.calls[0]
        self.assertEqual(call["timeout"], 7)
        self.assertEqual(call["max_output_bytes"], 1024)

    # -- error mapping ----------------------------------------------------

    def test_timeout_is_unknown_not_failed(self):
        with self.assertRaises(PlannerUnknownResultError):
            self.generate(RecordingRunner(FakeOutcome(timed_out=True)))

    def test_cancellation_is_unknown(self):
        with self.assertRaises(PlannerUnknownResultError):
            self.generate(RecordingRunner(FakeOutcome(cancelled=True)))

    def test_nonzero_exit_is_transient_and_carries_stderr(self):
        with self.assertRaises(PlannerTransientError) as caught:
            self.generate(RecordingRunner(FakeOutcome(returncode=3, stderr="boom")))
        self.assertIn("boom", str(caught.exception))

    def test_truncated_output_is_permanent_and_names_the_limit(self):
        with self.assertRaises(PlannerPermanentError) as caught:
            self.generate(
                RecordingRunner(FakeOutcome(stdout_truncated=True)),
                max_response_bytes=2048,
            )
        self.assertIn("2048", str(caught.exception))

    def test_blank_output_is_transient(self):
        with self.assertRaises(PlannerTransientError):
            self.generate(RecordingRunner(FakeOutcome(stdout="   \n")))

    def test_missing_binary_is_permanent(self):
        with self.assertRaises(PlannerPermanentError):
            self.generate(RecordingRunner(error=FileNotFoundError("planner-cli")))

    def test_unrunnable_binary_is_permanent(self):
        with self.assertRaises(PlannerPermanentError):
            self.generate(RecordingRunner(error=PermissionError("planner-cli")))

    def test_other_start_failures_stay_transient(self):
        with self.assertRaises(PlannerTransientError):
            self.generate(RecordingRunner(error=OSError("fork failed")))

    # -- construction guards ----------------------------------------------

    def test_rejects_empty_or_blank_commands_and_nonpositive_bounds(self):
        for command in ([], ["planner", " "]):
            with self.assertRaises(ValueError):
                TrustedCliPlannerProvider(command)
        with self.assertRaises(ValueError):
            TrustedCliPlannerProvider(["planner"], timeout_seconds=0)
        with self.assertRaises(ValueError):
            TrustedCliPlannerProvider(["planner"], max_response_bytes=0)

    def test_cancel_reports_false_rather_than_a_hopeful_true(self):
        self.assertFalse(self.provider(RecordingRunner()).cancel("fp-1"))


class CliProviderRealProcessTest(unittest.TestCase):
    """Through the real process port: stdin in, stdout back, bounds enforced."""

    def test_round_trip_through_a_real_child_process(self):
        provider = TrustedCliPlannerProvider(
            ["/bin/sh", "-c", 'payload=$(cat); printf \'{"echo": true}\''],
            timeout_seconds=30,
        )
        response = provider.generate(
            planning_context(), model_id="m1", request_fingerprint="fp-real"
        )
        self.assertEqual(response.raw_response, '{"echo": true}')

    def test_oversized_response_is_permanent(self):
        provider = TrustedCliPlannerProvider(
            ["/bin/sh", "-c", "cat > /dev/null; yes x | head -c 4096"],
            timeout_seconds=30, max_response_bytes=64,
        )
        with self.assertRaises(PlannerPermanentError):
            provider.generate(
                planning_context(), model_id="m1", request_fingerprint="fp-big"
            )


def discovered(name, executable_path="/usr/bin/true"):
    return DiscoveredAgent(AgentCliSpec(name, name), executable_path, "1.0")


class PlannerProviderFactoryTest(unittest.TestCase):
    def test_defaults_to_the_first_discovered_agent(self):
        from orbit.web.builtin_handlers import planner_provider_from_agents

        provider = planner_provider_from_agents(
            [discovered("claude", "/opt/claude"), discovered("codex", "/opt/codex")]
        )
        self.assertEqual(provider.command, ("/opt/claude",))

    def test_preferred_name_wins_over_discovery_order(self):
        from orbit.web.builtin_handlers import planner_provider_from_agents

        provider = planner_provider_from_agents(
            [discovered("claude", "/opt/claude"), discovered("codex", "/opt/codex")],
            preferred="codex",
        )
        self.assertEqual(provider.command, ("/opt/codex",))

    def test_missing_preferred_yields_none_not_a_silent_fallback(self):
        from orbit.web.builtin_handlers import planner_provider_from_agents

        self.assertIsNone(
            planner_provider_from_agents(
                [discovered("claude")], preferred="gemini"
            )
        )

    def test_no_agents_yields_none(self):
        from orbit.web.builtin_handlers import planner_provider_from_agents

        self.assertIsNone(planner_provider_from_agents([]))


class AppWiringTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "runtime.db"

    def test_no_discovery_means_no_planner(self):
        from orbit.web.app import create_app

        app = create_app(self.db_path, discover_agents=False)
        self.assertIsNone(app.state.planner)

    def test_discovered_agents_wire_a_planner_service(self):
        from orbit.web.app import create_app
        from orbit.web.builtin_handlers import BUILTIN_SCHEMAS

        # A real executable: the agent handler's preflight probes the path.
        agents = (discovered("claude", "/usr/bin/true"),)
        with mock.patch(
            "orbit.workflow.catalogs.agent_discovery.discover_agent_clis",
            return_value=agents,
        ):
            app = create_app(
                self.db_path, schemas=BUILTIN_SCHEMAS, discover_agents=True
            )
        planner = app.state.planner
        self.assertIsNotNone(planner)
        self.assertIsInstance(planner.provider, TrustedCliPlannerProvider)
        self.assertEqual(planner.provider.command, ("/usr/bin/true",))
        app.state.runtime.start()
        try:
            names = {loop.name for loop in app.state.runtime.loops}
            self.assertIn("planner-1", names)
            self.assertIn("planner-recovery", names)
            ready, checks = app.state.runtime.readiness()
            self.assertTrue(ready, checks)
        finally:
            self.assertEqual([], app.state.runtime.stop())

    def test_discovery_that_finds_nothing_leaves_the_planner_off(self):
        from orbit.web.app import create_app

        with mock.patch(
            "orbit.workflow.catalogs.agent_discovery.discover_agent_clis",
            return_value=(),
        ):
            app = create_app(self.db_path, discover_agents=True)
        self.assertIsNone(app.state.planner)


if __name__ == "__main__":
    unittest.main()
