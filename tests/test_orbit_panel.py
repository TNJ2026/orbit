"""The read-only panel shown beside a conversation.

The panel exists to be *less* than the UI. Starting a goal and writing a
workflow are asked of the Agent holding the Orbit skill, so the page beside the
conversation deliberately carries no way to do either. That is a property
somebody can erase in one well-meaning commit — a goal box "just here for
convenience" — and no other test would notice, because the page would still
render. These tests are the guard.
"""

from __future__ import annotations

from pathlib import Path
import re
import tempfile
import unittest

from starlette.testclient import TestClient

from orbit.web.app import create_app
from orbit.web.mcp_app import (
    ORBIT_DASHBOARD_HTML, ORBIT_DASHBOARD_URI, ORBIT_PANEL_PATH,
)


class PanelRouteTests(unittest.TestCase):
    def client(self) -> TestClient:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        app = create_app(Path(temp.name) / "runtime.db", serve_ui=True)
        client = TestClient(app)
        self.addCleanup(client.close)
        return client

    def test_the_panel_is_served_over_http(self) -> None:
        """A host that does not mount MCP Apps still has somewhere to send."""

        response = self.client().get(ORBIT_PANEL_PATH)
        self.assertEqual(200, response.status_code)
        self.assertIn("text/html", response.headers["content-type"])

    def test_the_panel_and_the_mcp_resource_are_one_page(self) -> None:
        """Two surfaces, one document.

        The panel is not a second implementation that may drift from the card:
        a host mounting `ui://orbit/workflows.html` and a host opening `/panel`
        must show the user the same thing.
        """

        self.assertEqual(
            ORBIT_DASHBOARD_HTML, self.client().get(ORBIT_PANEL_PATH).text,
        )
        self.assertEqual("ui://orbit/workflows.html", ORBIT_DASHBOARD_URI)

    def test_the_page_still_serves_when_the_ui_is_not(self) -> None:
        """`/ui` is a separate surface, and one does not imply the other."""

        self.assertEqual(200, self.client().get("/ui/").status_code)


class NoWayToMutateTests(unittest.TestCase):
    """What the panel must never grow.

    Each assertion names one route around the Agent. A control that appears
    here is a user starting runs from the conversation sidebar, which is the
    arrangement the panel was built to prevent.
    """

    def test_it_takes_no_input(self) -> None:
        for tag in ("<form", "<input", "<textarea", "<select"):
            with self.subTest(tag=tag):
                self.assertNotIn(tag, ORBIT_DASHBOARD_HTML)

    def test_its_only_control_is_refresh(self) -> None:
        buttons = re.findall(r"<button([^>]*)>", ORBIT_DASHBOARD_HTML)
        self.assertEqual(1, len(buttons))
        self.assertIn('id="refresh"', buttons[0])

    def test_it_names_read_only_tools_only(self) -> None:
        """Reading what Orbit holds and is doing; changing neither.

        The panel names its tools in one place, so the whole of its authority
        is this list. `start_run`, `resume_run`, `cancel_run` and
        `generate_workflow` are the ones that must never join it.
        """

        named = set(re.findall(r"load\(\s*'([a-z_]+)'", ORBIT_DASHBOARD_HTML))
        self.assertEqual({"list_workflows", "list_runs", "get_run_steps"}, named)

    def test_it_reaches_no_mutating_endpoint(self) -> None:
        self.assertNotIn("method: 'POST'", ORBIT_DASHBOARD_HTML)
        self.assertNotIn('method: "POST"', ORBIT_DASHBOARD_HTML)
        self.assertNotIn("idempotency-key", ORBIT_DASHBOARD_HTML.lower())
        # The single fetch is a GET, and every path it is given is a read.
        self.assertEqual(1, ORBIT_DASHBOARD_HTML.count("await fetch("))

    def test_it_says_where_those_actions_went(self) -> None:
        """A panel missing its controls without explanation reads as broken."""

        self.assertIn("read-only", ORBIT_DASHBOARD_HTML)
        self.assertIn("只读", ORBIT_DASHBOARD_HTML)


class SpeaksBothLanguagesTests(unittest.TestCase):
    """The rule the full UI is held to, applied to the page that escaped it.

    `tests/test_ui_assets.py` refuses hardcoded user-visible Chinese and holds
    the catalogs to matching keys, but it reads `static/workflow-ui`. This page
    is a string in Python, so none of that saw it, and it shipped monolingual
    with an English enum showing through. Being out of a guard's reach is not
    an exemption from the rule it enforces.
    """

    @staticmethod
    def _markup() -> str:
        """Everything before the script: what the document itself spells."""

        return ORBIT_DASHBOARD_HTML.split("<script>")[0]

    def test_the_markup_carries_no_user_visible_text(self) -> None:
        """Every string is chosen at runtime, so none is baked into the page."""

        self.assertEqual([], re.findall(r"[一-鿿]", self._markup()))
        self.assertNotIn("<html lang=", self._markup())

    def test_it_ships_both_catalogs(self) -> None:
        self.assertIn("'en-US'", ORBIT_DASHBOARD_HTML)
        self.assertIn("'zh-CN'", ORBIT_DASHBOARD_HTML)

    def test_it_translates_the_readiness_states_the_api_produces(self) -> None:
        """`ready`, and the two the workflows projection substitutes.

        An untranslated token reaching the card is how "15 个步骤 · ready"
        happened. The fallback keeps a state nobody translated readable as
        exactly that, rather than dressed as an ordinary one.
        """

        for state in ("ready", "needs_upgrade", "needs_migration"):
            with self.subTest(state=state):
                self.assertIn(state, ORBIT_DASHBOARD_HTML)
        self.assertIn("t().readiness[state] || state", ORBIT_DASHBOARD_HTML)


class ActivityTests(unittest.TestCase):
    """What Orbit is doing, which is the question a panel is there to answer.

    The catalogue says what Orbit *can* run. Beside a conversation the live
    goal matters more, and it is the one thing a static list cannot show: a
    user who asks the Agent to start something and then watches the sidebar
    should see it move.
    """

    def test_a_live_goal_is_the_one_it_shows(self) -> None:
        for state in ("running", "waiting", "interrupted"):
            with self.subTest(state=state):
                self.assertIn(f"'{state}'", ORBIT_DASHBOARD_HTML)
        self.assertIn("runs.find(item => ACTIVE.has(item.status))", ORBIT_DASHBOARD_HTML)

    def test_it_falls_back_to_the_newest_run(self) -> None:
        """So "did that finish?" is answerable after the run has ended."""

        self.assertIn("|| runs[0] || null", ORBIT_DASHBOARD_HTML)

    def test_every_run_status_has_words(self) -> None:
        """An untranslated status is the bug this page already shipped once."""

        for state in ("running", "waiting", "interrupted", "completed",
                      "failed", "cancelled", "unknown"):
            with self.subTest(state=state):
                self.assertIn(f"{state}:", ORBIT_DASHBOARD_HTML)
        self.assertIn("t().runStatus[status_] || status_", ORBIT_DASHBOARD_HTML)

    def test_steps_are_read_only_for_a_run_still_going(self) -> None:
        """A finished goal has nothing left to watch; do not pay for it."""

        self.assertIn("if (live === null) return;", ORBIT_DASHBOARD_HTML)

    def test_an_idle_panel_still_watches_for_a_goal_starting(self) -> None:
        """The case the card is for, and the one it first got wrong.

        A user asks the Agent to start a goal while looking at the panel. A
        poll that runs only while something is already live never notices, and
        the card sits on the last finished goal for good. Idle looks too, just
        rarely.
        """

        self.assertIn("POLL_LIVE_MS = 5000", ORBIT_DASHBOARD_HTML)
        self.assertIn("POLL_IDLE_MS = 20000", ORBIT_DASHBOARD_HTML)
        self.assertIn("live ? POLL_LIVE_MS : POLL_IDLE_MS", ORBIT_DASHBOARD_HTML)

    def test_nothing_polls_while_nobody_is_looking(self) -> None:
        """Each poll is a tool call the host may show its user."""

        self.assertIn("document.visibilityState === 'hidden'", ORBIT_DASHBOARD_HTML)
        self.assertIn("visibilitychange", ORBIT_DASHBOARD_HTML)

    def test_a_failed_read_leaves_the_last_answer_standing(self) -> None:
        """Activity is supplementary; a blip must not blank the catalogue."""

        self.assertIn("schedulePoll(false);", ORBIT_DASHBOARD_HTML)


class HostContextTests(unittest.TestCase):
    """Theme and language belong to the host, not to the operating system.

    The panel is drawn inside somebody else's conversation. Reading the OS for
    a theme the host has overridden puts a light card in a dark thread — and
    the disagreement appears exactly when a user has set the theme by hand.
    """

    def test_it_takes_the_context_offered_at_the_handshake(self) -> None:
        self.assertIn("applyHostContext(result?.hostContext)", ORBIT_DASHBOARD_HTML)

    def test_it_follows_the_host_when_the_context_changes(self) -> None:
        self.assertIn(
            "'ui/notifications/host-context-changed'", ORBIT_DASHBOARD_HTML,
        )

    def test_the_theme_reaches_the_whole_page(self) -> None:
        """One property, because the page is drawn in system colours."""

        self.assertIn("style.colorScheme = context.theme", ORBIT_DASHBOARD_HTML)
        self.assertIn("color-scheme: light dark", ORBIT_DASHBOARD_HTML)


class McpAppsBridgeTests(unittest.TestCase):
    """The handshake this page performs when a host speaks MCP Apps.

    The ext-apps SDK would supply this, and cannot: the page ships as one
    self-contained document with no build step. So the wire protocol is
    written out, which means the spec is only honoured for as long as these
    strings survive — nothing here fails loudly if the sequence is broken, the
    panel just sits empty on a host nobody tests against.

    Behaviour was verified in a browser against a simulated host: the page
    completes the handshake, renders from `ui/notifications/tool-result`,
    refreshes over `tools/call`, and falls back to HTTP against a parent that
    answers nothing. These assertions pin what that verification exercised.
    """

    def test_it_announces_the_protocol_revision_it_speaks(self) -> None:
        self.assertIn("'2026-01-26'", ORBIT_DASHBOARD_HTML)

    def test_it_performs_the_initialization_sequence(self) -> None:
        """`ui/initialize`, then the notification that unblocks the host.

        A host MUST NOT send tool input or results before the initialized
        notification, so dropping it leaves the panel waiting on data that is
        never coming.
        """

        self.assertIn("'ui/initialize'", ORBIT_DASHBOARD_HTML)
        self.assertIn("'ui/notifications/initialized'", ORBIT_DASHBOARD_HTML)

    def test_it_takes_the_opening_list_from_the_tool_result(self) -> None:
        self.assertIn("'ui/notifications/tool-result'", ORBIT_DASHBOARD_HTML)

    def test_it_refreshes_with_a_standard_tool_call(self) -> None:
        self.assertIn("'tools/call'", ORBIT_DASHBOARD_HTML)

    def test_it_speaks_to_the_parent_frame(self) -> None:
        self.assertIn("window.parent.postMessage", ORBIT_DASHBOARD_HTML)
        self.assertIn("window.parent === window", ORBIT_DASHBOARD_HTML)

    def test_every_host_request_is_deadlined(self) -> None:
        """Being framed says nothing about who is out there.

        A plain embed answers no request at all. Without a deadline the first
        one never settles, and the HTTP path that would have worked is never
        reached — the panel says "refreshing" forever.
        """

        self.assertIn("setTimeout", ORBIT_DASHBOARD_HTML)
        self.assertIn("宿主未响应", ORBIT_DASHBOARD_HTML)

    def test_a_host_that_does_not_answer_is_dropped(self) -> None:
        """And dropped for good, or every refresh spends another deadline."""

        self.assertIn("hostBridge = null", ORBIT_DASHBOARD_HTML)


class BothDataPathsTests(unittest.TestCase):
    """The page is fed by a host bridge or by HTTP, and shapes differ.

    The tool lifts `node_count` out of `summary`; the HTTP projection leaves it
    there. A page that read only one of them would render every workflow as
    zero steps on the other surface — true-looking, and wrong.
    """

    def test_it_reads_the_list_from_either_shape(self) -> None:
        for path in ("payload?.[key]", "payload?.data?.[key]",
                     "payload?.structuredContent?.[key]"):
            with self.subTest(path=path):
                self.assertIn(path, ORBIT_DASHBOARD_HTML)

    def test_it_finds_the_step_count_in_either_shape(self) -> None:
        self.assertIn("workflow.node_count", ORBIT_DASHBOARD_HTML)
        self.assertIn("workflow.summary?.node_count", ORBIT_DASHBOARD_HTML)

    def test_it_falls_back_to_http_when_there_is_no_bridge(self) -> None:
        self.assertIn("window.openai?.callTool", ORBIT_DASHBOARD_HTML)
        self.assertIn("await fetch(httpPath", ORBIT_DASHBOARD_HTML)
        for path in ("'/api/v1/workflows'", "'/api/v1/langgraph-runs?limit=5'",
                     "/steps`"):
            with self.subTest(path=path):
                self.assertIn(path, ORBIT_DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
