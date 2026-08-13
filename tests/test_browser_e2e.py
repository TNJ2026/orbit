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

    def test_no_view_raises_an_uncaught_error(self) -> None:
        """A net for the class of fault that has no visible symptom at first.

        An identifier that was never in scope cost this shell its background
        refresh for fifty-three commits: the polling chain threw before it
        rescheduled itself, so nothing updated until a reload, and the only
        trace was one line in a console nobody was reading. Every other test
        here drives a feature; none of them was listening for that.
        """

        run_id = self.start_goal("simplified-clean", "Watch for errors")
        context = self.browser.new_context(locale="en-US")
        self.addCleanup(context.close)
        # The fault this exists for did not happen during a render; it happened
        # in the polling chain, one interval later. Watching each view for a
        # moment would have missed it entirely, so the interval is set to its
        # supported minimum and every view is held past a tick.
        context.add_init_script(
            "localStorage.setItem('orbit.refreshSeconds', '5')"
        )
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on(
            "console",
            lambda message: errors.append(f"console.error: {message.text}")
            if message.type == "error" else None,
        )

        views = (
            ("#/workflows", "#content"),
            ("#/goals", "#content"),
            (f"#/runs/{run_id}", ".simplified-run-hero"),
            ("#/home", ".simplified-workspace-composer"),
        )
        for fragment, ready in views:
            with self.subTest(view=fragment):
                page.goto(f"{self.base}/ui/{fragment}")
                page.wait_for_selector(ready)
                page.wait_for_timeout(300)
                self.assertEqual([], errors, f"{fragment} raised {errors}")

        # The polling chain belongs to the shell rather than to any one view,
        # so one hold past a tick covers it — and costs six seconds instead of
        # six times four.
        page.wait_for_timeout(6_000)
        self.assertEqual([], errors, f"the refresh chain raised {errors}")

    def test_the_editor_is_reachable_from_the_navigation(self) -> None:
        """A page nobody can find is not shipped.

        The editor is its own bundle at its own address, so this is a link out
        of the shell rather than a view inside it — and it is offered only
        where the Runtime says the bundle is there, because a nav entry that
        404s is what the capability report exists to prevent.
        """

        from importlib import resources

        page = self.open("en-US")
        page.wait_for_selector(".simplified-workspace-composer")
        link = page.locator("#editorNav")
        built = resources.files("orbit").joinpath(
            "static/workflow-editor/index.html"
        ).is_file()
        self.assertEqual(built, link.is_visible())
        if not built:
            self.skipTest("editor bundle is not built in this checkout")

        self.assertEqual("/editor/", link.get_attribute("href"))
        link.click()
        page.wait_for_load_state("networkidle")
        self.assertTrue(page.url.endswith("/editor/"))
        self.assertEqual(
            1, page.locator("select[aria-label='Workflow']").count()
        )

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
        generated = page.locator(
            '.workflow-card[data-workflow-slug="research"]'
        )
        generated.wait_for(timeout=30_000)
        generated_id = generated.get_attribute("data-workflow-id")
        self.assertRegex(generated_id, r"^workflow:wf_[0-9a-f-]+$")

        page.locator('[data-view="home"]').click()
        page.wait_for_function(
            "(id => [...document.querySelectorAll('#simplifiedWorkflow option')]"
            ".some(node => node.value === id))",
            arg=generated_id,
        )

        current_values = page.locator("#simplifiedWorkflow option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        )
        self.assertNotIn(generated_id, original_values)
        self.assertIn(generated_id, current_values)

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
        page.get_by_role("button", name="More", exact=True).click()
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
            "artifacts", page.locator(".history-goal-row").first.inner_text().lower(),
        )


class SimplifiedGenerationProgressTests(BrowserE2ETestCase):
    @classmethod
    def extra_app_kwargs(cls) -> dict:
        from orbit.workflow.authoring import active_scope
        from tests.test_workflow_authoring_jobs import dsl

        def writer(_prompt):
            active_scope().on_output(
                "stderr",
                "Planning workflow nodes…\n" + ("unbroken-output-" * 120) + "\n"
                + ("streaming output\n" * 40),
            )
            if "fail visibly" in _prompt:
                time.sleep(0.2)
                raise RuntimeError("Agent could not produce a workflow")
            time.sleep(1.2)
            return json.dumps(dsl(workflow_id="generation_progress"))

        return {"single_goal_mode": True, "workflow_generator": writer}

    def test_generation_shows_prompt_agent_and_live_output(self) -> None:
        page = self.open("en-US", "/ui/#/workflows")
        prompt = "Collect sources and produce a concise report"
        page.fill("#generateInstruction", prompt)
        page.locator("#generateWorkflow").click()

        progress = page.locator(".workflow-generation-progress")
        progress.wait_for()
        self.assertIn(prompt, progress.locator(".workflow-generation-prompt").inner_text())
        self.assertTrue(progress.locator(".workflow-generation-agent").is_visible())
        self.assertEqual(4, progress.locator(".workflow-generation-step").count())
        self.assertTrue(progress.locator(".workflow-generation-step.current").is_visible())
        page.wait_for_function(
            "() => document.querySelector('.workflow-generation-console')"
            "?.textContent.includes('Planning workflow nodes')"
        )
        bounds = progress.locator(".workflow-generation-console").evaluate("""
          node => {
            const box = node.getBoundingClientRect();
            const panel = node.closest('.simplified-workflow-generator')
              .getBoundingClientRect();
            return {
              inside: box.left >= panel.left && box.right <= panel.right,
              height: box.height,
              overflowY: getComputedStyle(node).overflowY,
              scrollTop: node.scrollTop,
            };
          }
        """)
        self.assertTrue(bounds["inside"])
        self.assertLessEqual(bounds["height"], 322)
        self.assertEqual("auto", bounds["overflowY"])
        self.assertEqual(0, bounds["scrollTop"])

    def test_failed_generation_keeps_context_and_offers_retry(self) -> None:
        page = self.open("en-US", "/ui/#/workflows")
        page.fill("#generateInstruction", "fail visibly")
        page.locator("#generateWorkflow").click()

        failure = page.locator(".workflow-generation-failure")
        failure.wait_for()
        self.assertIn("Agent could not produce a workflow", failure.inner_text())
        self.assertIn(
            "fail visibly",
            page.locator(".workflow-generation-prompt-value").inner_text(),
        )
        page.get_by_role("button", name="Generate again").click()
        page.locator("#generateInstruction").wait_for()


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
        # Visible, explained, and without a way to start a Goal that would fail.
        # Destructive catalog removal remains available as a separate action.
        self.assertEqual(
            ["Upgrade workflow", "Delete"],
            card.locator(".workflow-card-actions button").evaluate_all(
                "nodes => nodes.map(node => node.getAttribute('aria-label')"
                " || node.textContent.trim())"
            ),
        )

    def test_delete_uses_an_application_dialog(self) -> None:
        page = self.open("en-US", "/ui/#/workflows")
        self.card(page).locator(".delete-workflow").click()

        dialog = page.get_by_role("dialog", name="Permanently delete workflow?")
        dialog.wait_for()
        self.assertIn("cannot be undone", dialog.inner_text())
        self.assertIn("workflow:legacy", dialog.inner_text())
        self.assertTrue(dialog.get_by_role("button", name="Permanently delete").is_visible())
        dialog.get_by_role("button", name="Cancel").click()
        page.locator(".workflow-delete-dialog").wait_for(state="detached")

    def test_upgrading_opens_a_prefilled_prompt_and_reports_what_changed(self) -> None:
        page = self.open("en-US", "/ui/#/workflows")
        self.card(page).locator(".upgrade-workflow").click()

        page.wait_for_selector(".workflow-editor textarea")
        prompt = page.locator(".workflow-editor textarea")
        self.assertIn("Upgrade this workflow", prompt.input_value())
        self.assertEqual(0, page.locator("#regenerateWorkflow").count())

        page.get_by_role("button", name="Revise").click()
        page.wait_for_selector(".workflow-editor .change-summary", timeout=30_000)
        summary = page.locator(".workflow-editor .change-summary").inner_text()
        self.assertIn("Fact check", summary)
        self.assertIn("runs before the report", summary)
        page.wait_for_function(
            "() => document.querySelector('.workflow-editing-version')"
            "?.textContent.includes('v2')"
        )
        # Drawn by the editor's canvas in a frame now, so the assertion has to
        # cross into it rather than read this document.
        self.assertIn(
            "Fact Check",
            page.frame_locator(".workflow-graph-frame")
                .locator(".react-flow").inner_text(),
        )

class SimplifiedRegenerateTests(BrowserE2ETestCase):

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
        page = self.open("en-US", "/ui/#/workflows/workflow:research/edit")
        page.wait_for_selector(".workflow-editor textarea")
        return page

    def test_detail_is_read_only_and_editing_has_its_own_route(self) -> None:
        page = self.open("en-US", "/ui/#/workflows")
        card = page.locator('[data-workflow-id="workflow:research"]')
        card.locator(".edit-workflow, .upgrade-workflow").click()
        page.wait_for_url("**/ui/#/workflows/workflow%3Aresearch/edit")
        page.wait_for_selector(".workflow-edit-page .workflow-editor textarea")
        self.assertEqual(1, page.locator("#closeWorkflowEditor").count())

        page.goto(f"{self.base}/ui/#/workflows/workflow:research")
        page.wait_for_selector(".workflow-detail")
        self.assertEqual(0, page.locator(".workflow-editor").count())
        page.wait_for_selector(".workflow-graph-frame")
        self.assertEqual(
            0,
            page.frame_locator(".workflow-graph-frame")
                .locator(".node-editable").count(),
        )
        self.assertEqual(0, page.locator("#editWorkflow").count())

    def test_the_graph_is_drawn_by_the_editor_canvas(self) -> None:
        """One renderer, embedded, rather than a second one written here.

        The page used to lay out its own SVG and carry its own zoom buttons,
        drawing the same definitions the editor draws with xyflow. The two
        disagreed about what a node looks like, and a fix to either was a fix
        to one of them.
        """

        page = self.open_dialog()
        canvas = page.frame_locator(".workflow-graph-frame")
        canvas.locator(".react-flow__node").first.wait_for(timeout=15_000)

        nodes = canvas.locator(".react-flow__node")
        self.assertTrue(nodes.count() > 1)
        self.assertTrue(canvas.locator(".react-flow__edge").count() > 0)
        # xyflow's own zoom controls, in place of the two buttons this page
        # used to own.
        self.assertTrue(canvas.locator(".react-flow__controls").is_visible())
        # Nothing on this canvas edits the definition. Whether a node has an
        # editor behind it is the embedding page's answer, sent in with the
        # graph; this fixture registers no Agent handlers, so there is none.
        self.assertEqual(0, canvas.locator(".node-editable").count())

    def test_the_embedded_canvas_carries_no_react_flow_badge(self) -> None:
        """The frame fills a modal, where a third-party mark reads as ours."""

        page = self.open_dialog()
        canvas = page.frame_locator(".workflow-graph-frame")
        canvas.locator(".node").first.wait_for(timeout=15_000)
        self.assertEqual(0, canvas.locator(".react-flow__attribution").count())

    def test_a_terminal_is_still_told_apart_from_an_action(self) -> None:
        """Kind was carried by shape and colour; it still has to be carried."""

        page = self.open_dialog()
        canvas = page.frame_locator(".workflow-graph-frame")
        canvas.locator(".node").first.wait_for(timeout=15_000)
        self.assertEqual(1, canvas.locator(".node-terminal").count())
        self.assertTrue(canvas.locator(".node-action").count() > 0)

    def test_regenerate_is_offered_only_after_a_modification_fails(self) -> None:
        page = self.open_dialog()

        self.assertEqual(0, page.locator("#regenerateWorkflow").count())
        page.locator(".workflow-editor textarea").fill("Add a fact check step")
        page.get_by_role("button", name="Revise").click()

        page.wait_for_selector("#retryWorkflowModify", timeout=30_000)
        page.locator("#retryWorkflowModify").click()

        page.wait_for_selector("#regenerateWorkflow")
        self.assertEqual(
            "Add a fact check step",
            page.locator(".workflow-editor textarea").input_value(),
        )
        self.assertEqual(1, page.get_by_role("button", name="Revise").count())
        self.assertIn(
            "Regenerate lets the system redesign",
            page.locator(".workflow-editor").inner_text(),
        )

    def test_a_failed_modification_leaves_the_workflow_alone(self) -> None:
        page = self.open_dialog()
        page.locator(".workflow-editor textarea").fill("Break everything")
        page.get_by_role("button", name="Revise").click()
        page.wait_for_selector("#retryWorkflowModify", timeout=30_000)

        latest = page.evaluate(
            "() => fetch('/api/v1/workflows/workflow:research')"
            ".then(r => r.json()).then(b => b.data.latest_version)"
        )
        self.assertEqual(1, latest)

class ReleaseHardeningTests(BrowserE2ETestCase):
    def test_all_primary_views_fit_the_mobile_viewport(self) -> None:
        context = self.browser.new_context(
            locale="en-US", viewport={"width": 360, "height": 800}
        )
        self.addCleanup(context.close)
        page = context.new_page()
        for view in ("home", "goals", "workflows", "agents"):
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


class SingleAgentHomeTests(BrowserE2ETestCase):
    """Single-agent mode starts published Workflows, not built-in templates.

    One Agent instead of several was supposed to be the whole difference
    between the two products. It was not: the single-agent home offered a
    fixed list of built-in templates, and the Workflow catalog — the page its
    own Agent publishes into — was hidden from the navigation entirely. A
    Workflow it had just generated had nowhere to be opened and no way to be
    started.

    This is also the only browser coverage of the LangGraph home screen; the
    rest of the suite runs the pre-LangGraph composer, which is why the
    template-only home went unnoticed.
    """

    @classmethod
    def extra_app_kwargs(cls) -> dict:
        from tests.test_workflow_authoring_jobs import dsl

        return {
            "single_goal_mode": True,
            "workflow_ui_mode": "single-agent",
            "langgraph_state_directory": Path(cls.temp.name) / "langgraph",
            # `app:` prefixed, because single-agent mode writes with the one
            # connected MCP Agent rather than offering a choice.
            "workflow_generators": {
                "app:writer": lambda _prompt: json.dumps(
                    dsl(name="Generated by the single Agent")
                ),
            },
        }

    def test_the_home_composer_offers_the_published_catalog(self) -> None:
        page = self.open("en-US")
        page.wait_for_selector(".simplified-workspace-composer")

        # The native select is hidden behind the custom one, so ask for what
        # a person sees, as the multi-agent test does.
        self.assertTrue(page.get_by_role("combobox", name="Workflow").is_visible())
        self.assertEqual(0, page.locator("#simplifiedTemplate").count())
        self.assertIn(
            "workflow:linear",
            page.locator("#simplifiedWorkflow option").evaluate_all(
                "nodes => nodes.map(node => node.value)"
            ),
        )

    def test_the_workflow_catalog_is_reachable(self) -> None:
        page = self.open("en-US")
        page.wait_for_selector(".simplified-workspace-composer")

        self.assertTrue(page.locator('[data-view="workflows"]').is_visible())
        page.locator('[data-view="workflows"]').click()
        page.wait_for_url("**/#/workflows")
        page.wait_for_selector(".workflow-card")
        self.assertTrue(page.locator(".workflow-card").count() > 0)

    def test_the_workflow_its_agent_generates_can_be_started(self) -> None:
        """The point of the catalog being here at all."""

        page = self.open("en-US")
        page.wait_for_selector(".simplified-workspace-composer")
        before = page.locator("#simplifiedWorkflow option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        )

        page.locator('[data-view="workflows"]').click()
        page.wait_for_selector(".simplified-workflow-generator")
        page.fill("#generateInstruction", "Collect the input and finish")
        page.click("#generateWorkflow")
        generated = page.locator('.workflow-card[data-workflow-slug="research"]')
        generated.wait_for(timeout=30_000)
        generated_id = generated.get_attribute("data-workflow-id")

        page.locator('[data-view="home"]').click()
        page.wait_for_function(
            "(id => [...document.querySelectorAll('#simplifiedWorkflow option')]"
            ".some(node => node.value === id))",
            arg=generated_id,
        )
        self.assertNotIn(generated_id, before)

    @staticmethod
    def runnable(*, human: bool):
        """A 1.3 document this Runtime can actually start.

        The suite's older fixtures declare no workflow-level `inputs`, so
        LangGraph refuses every input as unknown — they were published to be
        listed, not run.
        """

        from tests.test_workflow_authoring_jobs import dsl

        port = {"id": "value", "schema_id": "example://integer/1.0"}
        document = dsl(name="Runnable")
        document["inputs"] = [dict(port)]
        if human:
            document["metadata"]["id"] = "pausing"
            result = {"id": "result", "schema_id": "schema://object/1.0"}
            document["nodes"].insert(1, {
                "id": "approve", "kind": "human",
                "inputs": [dict(port)], "outputs": [dict(result)],
                # A static human node is an approval, its participants must be
                # unique, and its output has to accept the submission result
                # `{decision, value}` — the compiler refuses all three.
                "config": {
                    "task_kind": "approval", "participants": ["local"],
                    "quorum": "any",
                },
            })
            document["nodes"][-1]["inputs"] = [dict(result)]
            document["edges"] = [
                {"id": "to_approve",
                 "from": {"node": "collect", "port": "value"},
                 "to": {"node": "approve", "port": "value"}},
                {"id": "finish",
                 "from": {"node": "approve", "port": "result"},
                 "to": {"node": "done", "port": "result"}},
            ]
            document["result"] = {"node": "approve", "port": "result"}
        else:
            document["metadata"]["id"] = "runnable"
        return document

    def publish(self, page, document) -> str:
        workflow_id = f"workflow:{document['metadata']['id']}"
        response = page.evaluate(
            """([id, source]) => fetch(
                 `/api/v1/workflows/${encodeURIComponent(id)}/versions`,
                 {
                   method: "POST",
                   headers: {
                     "content-type": "application/json",
                     "idempotency-key": "publish-" + id,
                   },
                   body: JSON.stringify({source, expected_version: 0}),
                 },
               ).then((r) => r.json())""",
            [workflow_id, json.dumps(document)],
        )
        self.assertIn("data", response, response)
        return workflow_id

    def start(self, page, workflow_id: str) -> dict:
        return page.evaluate(
            """(id) => fetch("/api/v1/langgraph-runs", {
                 method: "POST",
                 headers: {
                   "content-type": "application/json",
                   "idempotency-key": "start-" + id,
                 },
                 body: JSON.stringify({workflow_id: id, input: {value: 1}}),
               }).then((r) => r.json()).then((b) => b.data.run)""",
            workflow_id,
        )

    def test_a_finished_run_hands_the_composer_back(self) -> None:
        """The two engines do not share a word for "over".

        The legacy Runtime finishes a run as `succeeded`; LangGraph finishes
        one as `completed`. One set of terminal statuses served both, so a
        finished LangGraph run was never terminal here: the composer stayed
        rendered, and rendered *disabled* — a summary is present, so every
        control locks and the button reads "in progress" — for a run that had
        been over for as long as it stayed selected.
        """

        page = self.open("en-US")
        page.wait_for_selector(".simplified-workspace-composer")
        workflow_id = self.publish(page, self.runnable(human=False))
        run = self.start(page, workflow_id)
        self.assertEqual("completed", run["status"])

        page.goto(f"{self.base}/ui/#/runs/{quote(run['run_id'], safe='')}")
        page.wait_for_selector("#content .panel")

        self.assertEqual(0, page.locator(".simplified-workspace-composer").count())

    def test_a_run_still_in_flight_keeps_the_composer_locked(self) -> None:
        """The other half: `interrupted` is not over, and must still lock."""

        page = self.open("en-US")
        page.wait_for_selector(".simplified-workspace-composer")
        workflow_id = self.publish(page, self.runnable(human=True))
        run = self.start(page, workflow_id)
        self.assertEqual("interrupted", run["status"])

        page.goto(f"{self.base}/ui/#/runs/{quote(run['run_id'], safe='')}")
        page.wait_for_selector(".simplified-workspace-composer")
        self.assertTrue(page.locator("#newGoalStart").is_disabled())

    def test_one_agent_is_still_the_difference(self) -> None:
        """What single-agent mode does withhold, it still withholds."""

        page = self.open("en-US")
        page.wait_for_selector(".simplified-workspace-composer")

        self.assertFalse(page.locator('[data-view="agents"]').is_visible())


if __name__ == "__main__":
    unittest.main()
