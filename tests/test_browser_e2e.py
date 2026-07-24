"""The Goal UI, driven by a real browser, in both languages.

What is covered here: the workspace composer and goal lifecycle, history,
the workflow catalog and its detail drawer (revision band included),
workflow generation and upgrade, Artifact browsing, and release hardening
(mobile viewports, navigation keyboard behaviour, localised network
failures).

playwright is a test-only dependency (`pip install -e '.[dev]'` plus
`playwright install chromium`). The suite skips when it is missing rather than
failing, so a plain checkout still runs green.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from urllib.parse import quote

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - exercised by the skip
    sync_playwright = None

from orbit.web.app import create_app
from orbit.web.api_v1 import Authorizer, WRITE_SCOPE
from orbit.web.local_identity import LOCAL_ACTOR, LOCAL_SCOPES, loopback_authenticator
from orbit.workflow.application.budget_service import BudgetService
from orbit.workflow.application.human_service import HumanTaskService
from orbit.workflow.application.run_service import RunApplicationService
from orbit.workflow.artifacts.local_cas import LocalCASBackend
from orbit.workflow.api.routes import RateLimiter
from orbit.workflow.domain.human import HumanTaskKind
from orbit.workflow.domain.ids import EntityId
from tests.test_web_composition import (
    SCHEMAS, publish_human_workflow, publish_linear_workflow,
    transform_registration,
)


LOCALES = ("zh-CN", "en-US")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@unittest.skipUnless(sync_playwright, "playwright is not installed")
class BrowserE2ETestCase(unittest.TestCase):
    """One server and one browser for the whole class; a page per test."""

    @classmethod
    def extra_app_kwargs(cls) -> dict:
        """Subclass hook for composition extras (e.g. a fake generator)."""
        return {}

    @classmethod
    def setUpClass(cls) -> None:
        import uvicorn

        cls.temp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.temp.name) / "runtime.db"
        cls.artifact_backend = LocalCASBackend(Path(cls.temp.name) / "artifacts")
        # Mutable only inside this test process: it lets a browser load an
        # advertised command and then lose authority before submission, which
        # proves the server re-checks scope at the command boundary.
        cls.scopes = set(LOCAL_SCOPES)
        app = create_app(
            cls.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            worker_count=2, poll_seconds=0.02,
            authenticator=loopback_authenticator,
            authorizer=Authorizer(
                lambda actor: tuple(cls.scopes) if actor == LOCAL_ACTOR else ()
            ),
            artifact_backend=cls.artifact_backend,
            rate_limiter=RateLimiter(requests=1_000),
            serve_ui=True,
            **cls.extra_app_kwargs(),
        )
        cls.app = app
        publish_linear_workflow(cls.db)
        publish_human_workflow(cls.db)

        port = free_port()
        cls.base = f"http://127.0.0.1:{port}"
        cls.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        )
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{cls.base}/health/ready", timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.05)
        else:
            raise AssertionError("server never became ready")

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()
        cls.server.should_exit = True
        cls.thread.join(timeout=30)
        cls.temp.cleanup()

    def open(self, locale: str = "en-US", path: str = "/ui/"):
        """A page whose browser language is `locale`, as a real visitor's is."""

        context = self.browser.new_context(locale=locale)
        page = context.new_page()
        self.addCleanup(context.close)
        page.goto(f"{self.base}{path}")
        page.wait_for_selector("#content")
        return page

    # -- fixtures ---------------------------------------------------------

    def start_run(self, key: str) -> str:
        service = RunApplicationService(self.db, self.app_service())
        return service.start_run(
            workflow_id="workflow:linear", inputs={"value": 1},
            actor="local", idempotency_key=key,
        ).run_id

    def start_goal(self, key: str, goal: str) -> str:
        service = RunApplicationService(self.db, self.app_service())
        return service.start_run(
            workflow_id="workflow:linear", inputs={"value": 1}, goal=goal,
            actor="local", idempotency_key=key,
        ).run_id

    def app_service(self):
        from orbit.workflow.application.durable_runtime_service import (
            DurableRuntimeApplicationService,
        )

        return DurableRuntimeApplicationService(self.db)

    def wait_for_status(self, page, run_id: str, status: str, timeout: float = 20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            payload = page.evaluate(
                "id => fetch(`/api/v1/runs/${encodeURIComponent(id)}`)"
                ".then(r => r.json()).then(b => b.data.status)",
                run_id,
            )
            if payload == status:
                return
            time.sleep(0.1)
        self.fail(f"{run_id} never reached {status}")

class SimplifiedGoalUITests(BrowserE2ETestCase):
    @classmethod
    def extra_app_kwargs(cls) -> dict:
        from tests.test_workflow_authoring_jobs import dsl

        return {
            "single_goal_mode": True,
            "workflow_generator": lambda _prompt: json.dumps(
                dsl(name="Generated from home")
            ),
        }

    def test_navigation_and_workflow_catalog_use_the_simplified_product_mode(self) -> None:
        page = self.open("en-US")
        page.wait_for_selector(".simplified-workspace-composer")

        self.assertEqual("simplified-goal", page.locator("html").get_attribute("data-product-mode"))
        self.assertEqual("Goal", page.locator("#homeNavLabel").inner_text())
        self.assertTrue(page.locator('[data-view="goals"]').is_visible())
        self.assertTrue(page.locator('[data-view="workflows"]').is_visible())
        self.assertFalse(page.locator('[data-view="inbox"]').is_visible())
        self.assertFalse(page.locator('[data-view="settings"]').is_visible())
        self.assertTrue(page.locator("#simplifiedGoal").is_visible())
        self.assertTrue(page.get_by_role("combobox", name="Workflow").is_visible())
        self.assertTrue(page.locator("#homeGenerateWorkflow").is_visible())
        self.assertTrue(any(
            "nodes" in text
            for text in page.locator("#simplifiedWorkflow option").all_inner_texts()
        ))
        self.assertEqual(0, page.locator("#generateWorkflow").count())
        self.assertTrue(page.locator("#newGoalStart").is_visible())
        controls = page.locator(
            ".simplified-workspace-composer #simplifiedWorkflow, "
            ".simplified-workspace-composer #simplifiedGoal, "
            ".simplified-workspace-composer #newGoalStart"
        )
        self.assertEqual(
            ["simplifiedWorkflow", "simplifiedGoal", "newGoalStart"],
            controls.evaluate_all("nodes => nodes.map(node => node.id)"),
        )
        self.assertEqual(0, page.get_by_role("button", name="Manage workflows").count())
        self.assertEqual(0, page.locator(".simplified-execution").count())
        self.assertEqual(0, page.locator(".simplified-result").count())
        self.assertEqual(0, page.locator(".simplified-artifacts").count())
        self.assertEqual(0, page.locator(".history-goal-row").count())
        self.assertEqual(0, page.locator(".workflow-card").count())

        page.click("#homeGenerateWorkflow")
        page.wait_for_url("**/#/workflows")
        page.wait_for_selector(".simplified-workflow-generator")

    def test_generated_workflow_is_added_to_the_home_selector(self) -> None:
        page = self.open("en-US")
        original_values = page.locator("#simplifiedWorkflow option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        )

        page.locator('[data-view="workflows"]').click()
        page.wait_for_selector(".simplified-workflow-generator")
        self.assertTrue(page.locator("#generateInstruction").is_visible())
        self.assertTrue(page.locator("#generateWorkflow").is_visible())
        self.assertEqual(0, page.locator(".filter-bar").count())
        self.assertTrue(page.locator(".workflow-card").count() > 0)
        self.assertTrue(page.evaluate(
            "document.querySelector('.simplified-workflow-generator')"
            ".compareDocumentPosition(document.querySelector('.workflow-grid')) & "
            "Node.DOCUMENT_POSITION_FOLLOWING"
        ))
        page.fill("#generateInstruction", "Collect the input and finish")
        page.click("#generateWorkflow")
        page.locator(
            '.workflow-card[data-workflow-id="workflow:research"]'
        ).wait_for(timeout=30_000)

        page.locator('[data-view="home"]').click()
        page.wait_for_function(
            "() => [...document.querySelectorAll('#simplifiedWorkflow option')]"
            ".some(node => node.value === 'workflow:research')"
        )

        current_values = page.locator("#simplifiedWorkflow option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        )
        self.assertNotIn("workflow:research", original_values)
        self.assertIn("workflow:research", current_values)

    def test_goal_starts_and_stays_in_one_workspace(self) -> None:
        page = self.open("en-US")
        started = {}

        def advertise_goal_ready_workflow(route):
            response = route.fetch()
            payload = response.json()
            entry = next(
                item for item in payload["data"]["workflows"]
                if item["workflow_id"] == "workflow:linear"
            )
            entry["goal_readiness"] = "ready"
            entry["goal_binding"] = {
                "source": "run.goal", "node_id": "first",
                "input_id": "value", "property": "goal", "value_shape": "object",
            }
            entry["allowed_commands"] = [{
                "command": "run.start", "label": "Start goal", "method": "POST",
                "href": "/api/v1/runs", "expected_version": 0,
            }]
            route.fulfill(response=response, json=payload)

        def start_goal(route):
            if route.request.method != "POST":
                return route.continue_()
            goal = route.request.post_data_json["goal"]
            run_id = self.start_goal("simplified-workspace-start", goal)
            started["run_id"] = run_id
            route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({
                    "schema_version": "1.0", "projection_version": None,
                    "data": {"run_id": run_id}, "next_cursor": None,
                }),
            )

        page.route("**/api/v1/workflows", advertise_goal_ready_workflow)
        page.route("**/api/v1/runs", start_goal)
        page.reload()
        page.wait_for_selector(".simplified-workspace-composer")
        page.locator("#simplifiedWorkflow").evaluate(
            "node => { node.value = 'workflow:linear';"
            " node.dispatchEvent(new Event('change', {bubbles: true})); }"
        )
        page.fill("#simplifiedGoal", "Prepare a concise report")
        page.click("#newGoalStart")
        page.wait_for_function("() => location.hash.startsWith('#/runs/run%3A')")
        page.wait_for_selector(".simplified-execution")

        self.assertTrue(started["run_id"])
        self.assertIn("Prepare a concise report", page.input_value("#simplifiedGoal"))
        self.assertEqual("workflow:linear", page.input_value("#simplifiedWorkflow"))
        self.assertTrue(page.locator(".simplified-result").is_visible())
        self.assertTrue(page.locator(".simplified-artifacts").is_visible())

    def test_run_detail_has_no_runtime_tabs(self) -> None:
        run_id = self.start_goal("simplified-run", "Prepare a concise report")
        page = self.open("en-US", f"/ui/#/runs/{run_id}")
        page.wait_for_selector(".simplified-run-hero")

        self.assertEqual(0, page.locator(".run-tabs").count())
        self.assertEqual(0, page.locator(".why-panel").count())
        self.assertEqual(0, page.locator(".simplified-workspace-composer").count())
        self.assertEqual(0, page.get_by_role("button", name="Run again").count())
        self.assertTrue(page.locator(".simplified-execution").is_visible())
        self.assertTrue(page.locator(".simplified-step-runner").first.is_visible())
        self.assertIn("Tool", page.locator(".simplified-step-runner").first.inner_text())
        self.assertTrue(page.locator(".simplified-result").is_visible())
        self.assertTrue(page.locator(".simplified-result .panel-body > .pill.succeeded").is_visible())
        self.assertNotIn("text/plain", page.locator(".simplified-result").inner_text())
        self.assertTrue(page.locator(".simplified-artifacts").is_visible())

    def test_run_step_output_loads_only_when_expanded(self) -> None:
        run_id = self.start_goal("simplified-step-output", "Prepare a source summary")
        context = self.browser.new_context(locale="en-US")
        self.addCleanup(context.close)
        page = context.new_page()
        requested: list[str] = []

        def output(route):
            requested.append(route.request.url)
            route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({
                    "schema_version": "1.0", "projection_version": None,
                    "data": {
                        "chunks": [{
                            "chunk_id": 1,
                            "node_run_id": "node_run:test",
                            "attempt_id": "attempt:test",
                            "stream": "stdout",
                            "text": "Collected and summarized the source.\n",
                            "created_at": "2026-07-24T00:00:00+00:00",
                        }],
                        "after": 1,
                        "has_more": False,
                    },
                    "next_cursor": None,
                }),
            )

        page.route("**/api/v1/runs/*/output?*", output)
        page.goto(f"{self.base}/ui/#/runs/{run_id}")
        step_output = page.locator(".simplified-step-output").first
        step_output.wait_for()

        self.assertEqual([], requested)
        step_output.locator("summary").click()
        page.wait_for_function(
            "() => document.querySelector('.simplified-step-output-log')"
            ".textContent.includes('Collected and summarized the source.')"
        )

        self.assertTrue(any("node_run_id=" in url for url in requested))
        self.assertTrue(step_output.locator(".simplified-step-output-log").is_visible())

    def test_refresh_interval_moves_to_the_topbar_and_settings_are_removed(self) -> None:
        page = self.open("en-US")
        interval = page.get_by_role("combobox", name="Live refresh interval")
        interval.wait_for()
        self.assertTrue(page.evaluate("""
          () => {
            const language = document.querySelector(
              '.topbar [role="combobox"][aria-label="Change language"]'
            );
            const refresh = document.querySelector(
              '.topbar [role="combobox"][aria-label="Live refresh interval"]'
            );
            return Boolean(language.compareDocumentPosition(refresh)
              & Node.DOCUMENT_POSITION_FOLLOWING);
          }
        """))
        interval.click()
        page.get_by_role("option", name="30 seconds").click()
        self.assertEqual(
            "30", page.evaluate("localStorage.getItem('orbit.refreshSeconds')")
        )

        page.goto(f"{self.base}/ui/#/settings")
        page.wait_for_function("() => location.hash === '#/home'")
        page.wait_for_selector(".simplified-workspace-composer")

    def test_history_lists_finished_goals_only(self) -> None:
        finished = self.start_goal("simplified-history", "Summarise the quarter")
        self.wait_for_status(self.open("en-US"), finished, "succeeded")

        page = self.open("en-US", "/ui/#/goals")
        page.wait_for_selector(".history-goal-row")
        rows = page.locator(".history-goal-row")
        self.assertIn("Summarise the quarter", rows.first.inner_text())
        self.assertIn("Artifacts", rows.first.inner_text())
        self.assertTrue(page.locator(".history-day-heading").first.is_visible())
        self.assertEqual(4, page.locator(".history-status-filter").count())
        self.assertEqual(0, page.locator(".history-goal-row .status-dot").count())
        self.assertTrue(page.locator(".history-goal-chevron").first.is_visible())
        # History keeps user-facing chips instead of the Runtime's technical filter select.
        self.assertEqual(0, page.locator("select[aria-label='Filter goals by status']").count())

        rows.first.click()
        page.wait_for_selector(".simplified-run-hero")
        self.assertEqual(0, page.locator(".simplified-workspace-composer").count())
        self.assertEqual(0, page.get_by_role("button", name="Run again").count())

    def test_history_loads_the_next_cursor_page(self) -> None:
        context = self.browser.new_context(locale="en-US")
        self.addCleanup(context.close)
        page = context.new_page()

        def history_page(route):
            second_page = "cursor=next" in route.request.url
            run = {
                "run_id": f"run:{'second' if second_page else 'first'}",
                "workflow_id": "workflow:linear", "workflow_version": 1,
                "display_name": "Second page Goal" if second_page else "First page Goal",
                "goal": "Second page Goal" if second_page else "First page Goal",
                "status": "succeeded", "artifact_count": 1,
                "created_at": "2026-07-24T08:00:00Z",
                "updated_at": "2026-07-24T08:02:00Z",
            }
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "schema_version": "1.0", "projection_version": None,
                "data": {"runs": [run]},
                "next_cursor": None if second_page else "next",
            }))

        page.route("**/api/v1/runs?*", history_page)
        page.goto(f"{self.base}/ui/#/goals")
        page.wait_for_selector(".history-goal-row")
        self.assertEqual(1, page.locator(".history-goal-row").count())

        page.get_by_role("button", name="Load more").click()
        page.wait_for_function(
            "() => document.querySelectorAll('.history-goal-row').length === 2"
        )
        self.assertIn("Second page Goal", page.locator(".history-goal-row").last.inner_text())
        self.assertFalse(page.get_by_role("button", name="Load more").is_visible())

    def test_progress_rides_the_live_cursor_and_adds_no_second_channel(self) -> None:
        """One refresh mechanism for the whole shell, including this page."""

        run_id = self.start_goal("simplified-live", "Watch this run")
        context = self.browser.new_context(locale="en-US")
        self.addCleanup(context.close)
        page = context.new_page()
        polled: list[str] = []
        page.on("request", lambda request: polled.append(request.url))
        page.goto(f"{self.base}/ui/#/runs/{run_id}")
        page.wait_for_selector(".simplified-run-hero")
        page.wait_for_function(
            "() => window.performance.getEntriesByType('resource')"
            ".some(entry => entry.name.includes('/api/v1/live'))",
            timeout=30_000,
        )

        self.assertFalse(
            [url for url in polled if "/events" in url or "/stream" in url],
            "the simplified run page must not open a second progress channel",
        )

    def test_history_reads_one_page_rather_than_two_calls_per_row(self) -> None:
        """Twenty-five rows must not become fifty-one requests."""

        finished = self.start_goal("simplified-history-cost", "Tidy the archive")
        self.wait_for_status(self.open("en-US"), finished, "succeeded")

        context = self.browser.new_context(locale="en-US")
        self.addCleanup(context.close)
        page = context.new_page()
        per_row: list[str] = []
        page.on("request", lambda request: per_row.append(request.url) if (
            "/outcome" in request.url or "/api/v1/artifacts?" in request.url
        ) else None)
        page.goto(f"{self.base}/ui/#/goals")
        page.wait_for_selector(".history-goal-row")
        page.wait_for_timeout(500)

        self.assertEqual([], per_row)
        self.assertIn(
            "content", page.locator(".history-goal-row").first.inner_text().lower(),
        )


class SimplifiedUpgradeTests(BrowserE2ETestCase):
    """A published Workflow that cannot start a Goal is fixed by prompting."""

    @classmethod
    def extra_app_kwargs(cls) -> dict:
        from tests.test_workflow_authoring_jobs import dsl as goal_ready_dsl

        return {
            "single_goal_mode": True,
            # The Agent answers with the envelope the modify contract asks for,
            # so the summary the page shows is the Agent's own words.
            "workflow_generator": lambda _prompt: json.dumps({
                "workflow": {
                    **goal_ready_dsl(nodes=("collect", "fact_check")),
                    "metadata": {"id": "legacy", "name": "Legacy archive"},
                },
                "change_summary": [{
                    "kind": "added", "node_id": "fact_check",
                    "label": "Fact check", "detail": "runs before the report",
                }],
            }),
        }

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from orbit.workflow.application.workflows import (
            WorkflowCatalogs, WorkflowDefinitionService,
        )
        from orbit.workflow.catalogs import (
            InMemoryHandlerCatalog, InMemorySchemaCatalog,
        )
        from orbit.workflow.catalogs.extensions import InMemoryExtensionRegistry
        from orbit.workflow.persistence.workflow_versions import (
            SQLiteWorkflowVersionStore,
        )
        from tests.test_workflow_authoring_jobs import MANIFEST, dsl as goal_ready_dsl

        legacy = goal_ready_dsl()
        legacy["dsl_version"] = "1.2"
        legacy["metadata"] = {"id": "legacy", "name": "Legacy archive"}
        legacy.pop("result")
        WorkflowDefinitionService(
            WorkflowCatalogs(
                InMemoryHandlerCatalog([MANIFEST]),
                InMemorySchemaCatalog({
                    "example://integer/1.0": {"type": "integer"},
                    "schema://object/1.0": {"type": "object"},
                }),
                InMemoryExtensionRegistry(),
            ),
            SQLiteWorkflowVersionStore(cls.db),
        ).publish_workflow(
            json.dumps(legacy), source_name="<test>", source_format="json",
            expected_latest_version=0, actor="local",
        )

    def card(self, page):
        card = page.locator(".workflow-card", has_text="Legacy archive").first
        card.wait_for()
        return card

    def test_a_workflow_needing_an_upgrade_stays_visible_but_cannot_start(self) -> None:
        page = self.open("en-US", "/ui/#/workflows")
        card = self.card(page)

        self.assertIn("Upgrade needed", card.inner_text())
        # Visible, explained, and without a way to start a Goal that would fail:
        # upgrading is the only action the card offers.
        self.assertEqual(
            ["Upgrade workflow"],
            card.locator(".workflow-card-actions button").all_inner_texts(),
        )

    def test_upgrading_opens_a_prefilled_prompt_and_reports_what_changed(self) -> None:
        page = self.open("en-US", "/ui/#/workflows")
        self.card(page).locator(".upgrade-workflow").click()

        page.wait_for_selector(".workflow-editor textarea")
        prompt = page.locator(".workflow-editor textarea")
        self.assertIn("Upgrade this workflow", prompt.input_value())
        # Regenerate is the bigger hammer and stays hidden until modify fails.
        self.assertEqual(0, page.locator("#regenerateWorkflow").count())

        page.get_by_role("button", name="Revise with Agent").click()
        page.wait_for_selector(".workflow-editor .change-summary", timeout=30_000)
        summary = page.locator(".workflow-editor .change-summary").inner_text()
        self.assertIn("Fact check", summary)
        self.assertIn("runs before the report", summary)


class SimplifiedRegenerateTests(BrowserE2ETestCase):
    """Regenerate appears only once keeping the structure has demonstrably failed.

    It discards a structure the author already accepted, so offering it before
    there is any evidence that modifying cannot work would put the largest
    action in the easiest place to hit by accident.
    """

    @classmethod
    def extra_app_kwargs(cls) -> dict:
        return {
            "single_goal_mode": True,
            # Never produces anything the compiler accepts, so every attempt
            # settles as failed and the retry path is the one under test.
            "workflow_generator": lambda _prompt: "not a workflow at all",
        }

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from orbit.workflow.application.workflows import (
            WorkflowCatalogs, WorkflowDefinitionService,
        )
        from orbit.workflow.catalogs import (
            InMemoryHandlerCatalog, InMemorySchemaCatalog,
        )
        from orbit.workflow.catalogs.extensions import InMemoryExtensionRegistry
        from orbit.workflow.persistence.workflow_versions import (
            SQLiteWorkflowVersionStore,
        )
        from tests.test_workflow_authoring_jobs import MANIFEST, dsl

        # Modifying needs the author's source, which the shared linear fixture
        # does not carry.
        WorkflowDefinitionService(
            WorkflowCatalogs(
                InMemoryHandlerCatalog([MANIFEST]),
                InMemorySchemaCatalog({
                    "example://integer/1.0": {"type": "integer"},
                    "schema://object/1.0": {"type": "object"},
                }),
                InMemoryExtensionRegistry(),
            ),
            SQLiteWorkflowVersionStore(cls.db),
        ).publish_workflow(
            json.dumps(dsl()), source_name="<test>", source_format="json",
            expected_latest_version=0, actor="local",
        )

    def open_dialog(self):
        page = self.open("en-US", "/ui/#/workflows/workflow:research")
        page.wait_for_selector(".workflow-editor textarea")
        return page

    def test_regenerate_is_offered_only_after_a_modification_fails(self) -> None:
        page = self.open_dialog()

        # 1. The first form offers modifying and nothing larger.
        self.assertEqual(0, page.locator("#regenerateWorkflow").count())
        page.locator(".workflow-editor textarea").fill("Add a fact check step")
        page.get_by_role("button", name="Revise with Agent").click()

        # 2. The failure offers a retry rather than a dead end.
        page.wait_for_selector("#retryWorkflowModify", timeout=30_000)
        page.locator("#retryWorkflowModify").click()

        # 3. Only now are both offered, and the prompt survived the round trip.
        page.wait_for_selector("#regenerateWorkflow")
        self.assertEqual(
            "Add a fact check step",
            page.locator(".workflow-editor textarea").input_value(),
        )
        self.assertEqual(
            1, page.get_by_role("button", name="Revise with Agent").count(),
        )
        self.assertIn(
            "Regenerate lets the system redesign",
            page.locator(".workflow-editor").inner_text(),
        )

    def test_a_failed_modification_leaves_the_workflow_alone(self) -> None:
        page = self.open_dialog()
        page.locator(".workflow-editor textarea").fill("Break everything")
        page.get_by_role("button", name="Revise with Agent").click()
        page.wait_for_selector("#retryWorkflowModify", timeout=30_000)

        latest = page.evaluate(
            "() => fetch('/api/v1/workflows/workflow:research')"
            ".then(r => r.json()).then(b => b.data.latest_version)"
        )
        self.assertEqual(1, latest)


PIXEL_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
CHECKER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAM0lEQVR42mN0av3CAAPL0uFMhq"
    "iZDFjFmRhIBLTXwPjizReC7kYWH4R+YCHG3aPxQHMNAER/FF7xPoSoAAAAAElFTkSuQmCC"
)


class ArtifactCatalogTests(BrowserE2ETestCase):
    def artifact(
        self, *, content=b"reviewable artifact text", content_type="text/plain",
        key="browser-artifact", goal=None, output_port="report",
    ) -> str:
        from orbit.workflow.persistence.database import connect_workflow_database

        run_id = (
            self.start_goal(key, goal) if goal is not None else self.start_run(key)
        )
        receipt = self.artifact_backend.write(content, max_size_bytes=1024 * 1024)
        artifact_id = f"artifact:{receipt.checksum.value.removeprefix('sha256:')}"
        with connect_workflow_database(self.db) as connection:
            event_id = connection.execute(
                "SELECT event_id FROM run_events WHERE run_id=? ORDER BY global_position LIMIT 1",
                (run_id,),
            ).fetchone()[0]
            now = "2026-01-01T00:00:00+00:00"
            connection.execute(
                "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact_id, run_id, "workflow:linear", "attempt", "attempt:browser",
                    "node_run:browser", output_port, "schema:text", content_type,
                    receipt.checksum.value, receipt.size_bytes, receipt.blob_key,
                    "run", run_id, "committed", now, now, event_id, None,
                ),
            )
            connection.execute(
                "INSERT INTO artifact_acl VALUES (?,'local','read','local',?)",
                (artifact_id, now),
            )
            connection.execute(
                "INSERT INTO artifact_links VALUES (?,?,?,?,?,?,?,?)",
                (
                    f"artifact_link:{key}-producer", "workflow:linear", run_id,
                    artifact_id, "producer", "attempt:browser", event_id, now,
                ),
            )
            connection.commit()
        return artifact_id

    def test_the_detail_dialog_closes_back_to_the_catalog(self) -> None:
        artifact_id = self.artifact(
            content=b"dialog artifact text", key="browser-artifact-dialog",
        )
        page = self.open("en-US", path=f"/ui/#/artifacts/{artifact_id}")
        page.wait_for_selector("dialog[open].artifact-detail .panel-title")
        page.keyboard.press("Escape")
        page.wait_for_selector("dialog.artifact-detail", state="detached")
        # Escape and Close must both leave the address on the catalog: a
        # dismissed dialog that keeps its own URL reopens on reload.
        page.wait_for_function("() => location.hash === '#/artifacts'")
        page.wait_for_selector(".artifact-card-main")

        page.locator(".artifact-card-main").first.click()
        page.wait_for_selector("dialog[open].artifact-detail .panel-title")
        page.get_by_role("button", name="Close").click()
        page.wait_for_selector("dialog.artifact-detail", state="detached")
        page.wait_for_function("() => location.hash === '#/artifacts'")

    def test_a_document_card_leads_with_its_title_goal_and_workflow(self) -> None:
        self.artifact(
            content=b"# Launch checklist\n\nStep one.\n", content_type="text/markdown",
            key="browser-artifact-document", goal="Ship the launch",
        )
        page = self.open("en-US", path="/ui/#/artifacts")
        card = page.locator(".artifact-card", has_text="Launch checklist").first
        card.wait_for()
        self.assertEqual("Launch checklist", card.locator(".artifact-name").inner_text())
        text = card.inner_text()
        self.assertIn("Ship the launch", text)
        # The workflow reads by name; `workflow:` is the id's kind, not a fact
        # the card has to spend a line on.
        self.assertIn("linear", text)
        self.assertNotIn("workflow:linear", text)
        # Addressing — the output port, the producer, the Artifact id — belongs
        # to the detail panel, not to a card being scanned.
        self.assertNotIn("report", text)
        self.assertNotIn("attempt:browser", text)

    def test_an_image_card_shows_the_image(self) -> None:
        artifact_id = self.artifact(
            content=PIXEL_PNG, content_type="image/png", key="browser-artifact-image",
        )
        page = self.open("en-US", path="/ui/#/artifacts")
        thumb = page.locator(".artifact-thumb").first
        thumb.wait_for()
        self.assertIn(quote(artifact_id, safe=""), thumb.get_attribute("src"))
        # A broken thumbnail would still be an <img>; only a decoded one has
        # intrinsic dimensions. The fetch is the browser's own, so wait for it
        # rather than sampling the frame the assertion happens to land on.
        page.wait_for_function(
            "() => { const img = document.querySelector('.artifact-thumb');"
            " return !!img && img.complete && img.naturalWidth > 0; }"
        )

    def test_z_failed_detail_image_can_be_retried(self) -> None:
        artifact_id = self.artifact(
            content=CHECKER_PNG, content_type="image/png",
            key="browser-artifact-image-error", output_port="report-error",
        )
        page = self.open("en-US", path="/ui/#/artifacts")
        content_path = "**/api/v1/artifacts/*/content"
        page.route(content_path, lambda route: route.abort())
        page.goto(f"{self.base}/ui/#/artifacts/{quote(artifact_id, safe='')}")
        dialog = page.locator("dialog[open].artifact-detail")
        dialog.wait_for()
        retry = dialog.get_by_role("button", name="Try again")
        retry.wait_for()
        self.assertEqual(0, dialog.locator(".artifact-image").count())

        page.unroute(content_path)
        retry.click()
        page.wait_for_function(
            "() => { const img = document.querySelector("
            "'dialog.artifact-detail .artifact-image');"
            " return !!img && img.complete && img.naturalWidth > 0; }"
        )


class ReleaseHardeningTests(BrowserE2ETestCase):
    def test_all_primary_views_fit_the_mobile_viewport(self) -> None:
        context = self.browser.new_context(
            locale="en-US", viewport={"width": 360, "height": 800}
        )
        self.addCleanup(context.close)
        page = context.new_page()
        for view in ("home", "goals", "workflows", "artifacts"):
            with self.subTest(view=view):
                page.goto(f"{self.base}/ui/#/{view}")
                page.wait_for_function(
                    "() => document.querySelector('#content').childElementCount > 0"
                    " && !document.querySelector('#content .loading')"
                )
                overflow = page.evaluate(
                    "() => document.documentElement.scrollWidth - window.innerWidth"
                )
                self.assertLessEqual(overflow, 1, f"{view} overflows by {overflow}px")

    def test_mobile_navigation_closes_with_escape(self) -> None:
        context = self.browser.new_context(
            locale="en-US", viewport={"width": 360, "height": 800}
        )
        self.addCleanup(context.close)
        page = context.new_page()
        page.goto(f"{self.base}/ui/")
        page.click("#navToggle")
        self.assertEqual("true", page.get_attribute("#navToggle", "aria-expanded"))
        page.keyboard.press("Escape")
        self.assertEqual("false", page.get_attribute("#navToggle", "aria-expanded"))
        self.assertEqual("navToggle", page.evaluate("document.activeElement.id"))

    def test_network_failure_is_localised_and_retryable(self) -> None:
        page = self.open("en-US")
        failing = {"value": True}

        def network(route):
            if failing["value"]:
                route.abort()
            else:
                route.continue_()

        page.route("**/api/v1/dashboard*", network)
        page.click("#refresh")
        page.wait_for_selector("#content .data-state.error")
        self.assertIn("Cannot reach the runtime", page.inner_text("#content"))
        failing["value"] = False
        # Invoke the currently rendered retry control synchronously. The live
        # refresh loop may legitimately replace the error state between
        # Playwright's actionability checks, which made a semantic click flaky
        # even though the button and its handler were both correct.
        page.locator("#content .data-state.error button").evaluate(
            "button => button.click()"
        )
        page.wait_for_selector("#content .panel")

    def test_service_unavailable_is_locatable_and_retryable(self) -> None:
        page = self.open("en-US", path="/ui/#/inbox")
        failing = {"value": True}

        def unavailable(route):
            if failing["value"]:
                route.fulfill(
                    status=503, content_type="application/json",
                    body=json.dumps({
                        "error": {
                            "code": "temporarily_unavailable",
                            "message": "projection is rebuilding", "details": {},
                        }
                    }),
                )
            else:
                route.continue_()

        page.route("**/api/v1/dashboard", unavailable)
        page.click('[data-view="home"]')
        page.wait_for_selector("#content .data-state.error")
        self.assertIn("projection is rebuilding", page.inner_text("#content"))
        failing["value"] = False
        page.get_by_role("button", name="Try again").click()
        page.wait_for_selector("#content .simplified-workspace-composer")


if __name__ == "__main__":
    unittest.main()
