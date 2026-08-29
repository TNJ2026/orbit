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
        buttons = re.findall(r"<button[^>]*>(.*?)</button>", ORBIT_DASHBOARD_HTML)
        self.assertEqual(["刷新"], buttons)

    def test_it_calls_read_only_tools_only(self) -> None:
        """`list_workflows` reads; `start_run` and `generate_workflow` do not."""

        called = re.findall(r"callTool\('([^']+)'", ORBIT_DASHBOARD_HTML)
        self.assertEqual(["list_workflows"], called)

    def test_it_reaches_no_mutating_endpoint(self) -> None:
        fetched = re.findall(r"fetch\('([^']+)'", ORBIT_DASHBOARD_HTML)
        self.assertEqual(["/api/v1/workflows"], fetched)
        self.assertNotIn("method: 'POST'", ORBIT_DASHBOARD_HTML)
        self.assertNotIn('method: "POST"', ORBIT_DASHBOARD_HTML)

    def test_it_says_where_those_actions_went(self) -> None:
        """A panel missing its controls without explanation reads as broken."""

        self.assertIn("Agent", ORBIT_DASHBOARD_HTML)
        self.assertIn("只读", ORBIT_DASHBOARD_HTML)


class BothDataPathsTests(unittest.TestCase):
    """The page is fed by a host bridge or by HTTP, and shapes differ.

    The tool lifts `node_count` out of `summary`; the HTTP projection leaves it
    there. A page that read only one of them would render every workflow as
    zero steps on the other surface — true-looking, and wrong.
    """

    def test_it_reads_the_list_from_either_shape(self) -> None:
        for path in ("payload?.workflows", "payload?.data?.workflows"):
            with self.subTest(path=path):
                self.assertIn(path, ORBIT_DASHBOARD_HTML)

    def test_it_finds_the_step_count_in_either_shape(self) -> None:
        self.assertIn("workflow.node_count", ORBIT_DASHBOARD_HTML)
        self.assertIn("workflow.summary?.node_count", ORBIT_DASHBOARD_HTML)

    def test_it_falls_back_to_http_when_there_is_no_bridge(self) -> None:
        self.assertIn("window.openai?.callTool", ORBIT_DASHBOARD_HTML)
        self.assertIn("fetch('/api/v1/workflows'", ORBIT_DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
