"""The UI, driven by a real browser, in both languages.

What is covered here: locale selection and switching, the new-run dialog,
approving from the inbox, granting budget to an exhausted run, cancelling a
run parked on a person, the plan panel's definition/overlay split, the ops
page, and a failed run's error panel.

Artifact metadata and lineage are inspected from Run detail, and recovery is
applied from an actual finding on the Ops page.  The Human journey is one
published static workflow: Transform action -> Human controller -> terminal.

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

    def complete_goal_wizard(
        self, page, workflow_id: str, *, goal: str, inputs: dict[str, object]
    ) -> None:
        page.check(f'input[name="workflow"][value="{workflow_id}"]')
        page.click('[data-wizard-next]')
        page.fill("#newRunGoal", goal)
        for port_id, value in inputs.items():
            control = f"#newRunInput-{port_id}"
            if isinstance(value, bool):
                page.set_checked(control, value)
            else:
                page.fill(control, str(value))
        page.click('[data-wizard-next]')
        page.click('[data-wizard-next]')
        page.click("#newGoalStart")


class LocaleTests(BrowserE2ETestCase):
    def test_the_browser_language_picks_the_locale(self) -> None:
        for locale, expected in (("zh-CN", "工作台"), ("en-US", "Workspace")):
            with self.subTest(locale=locale):
                page = self.open(locale)
                # boot applies the negotiated locale only after the async
                # catalog, capability, and runtime-card loads; wait for that
                # instead of racing it (same pattern as the switching test).
                page.wait_for_function(
                    f"() => document.documentElement.lang === '{locale}'"
                )
                page.wait_for_function(
                    "() => document.querySelector('#viewTitle')?.textContent"
                    f".includes('{expected}')"
                )
                self.assertEqual(locale, page.get_attribute("html", "lang"))
                self.assertIn(expected, page.inner_text("#viewTitle"))

    def test_switching_locale_retranslates_the_page(self) -> None:
        page = self.open("en-US")
        page.locator("#localeSelect").locator("..").get_by_role("combobox").click()
        page.get_by_role("option", name="简体中文").click()
        page.wait_for_function("() => document.documentElement.lang === 'zh-CN'")
        # setLocale updates the static shell before awaiting the async view
        # render. Waiting only on <html lang> races that second phase.
        page.wait_for_function(
            "() => document.querySelector('#viewTitle')?.textContent.includes('工作台')"
        )
        self.assertIn("工作台", page.inner_text("#viewTitle"))
        self.assertIn("待办", page.inner_text(".sidebar"))

    def test_no_key_leaks_into_the_page_in_either_locale(self) -> None:
        """A missing translation renders as its key; that must never ship."""

        import re

        for locale in LOCALES:
            with self.subTest(locale=locale):
                page = self.open(locale)
                text = page.inner_text("body")
                leaked = re.findall(
                    r"\b(?:home|goals|workflows|newRun|runs|run|inbox|ops|action|plan|wait|responsibility)\.[a-z][\w.]+",
                    text,
                )
                self.assertEqual([], leaked, f"untranslated keys: {leaked}")


class AccessibilityAndResponsiveTests(BrowserE2ETestCase):
    def test_keyboard_reaches_the_skip_link_and_main_navigation(self) -> None:
        page = self.open("en-US")
        page.keyboard.press("Tab")
        self.assertIn("skip-link", page.locator(":focus").get_attribute("class") or "")
        page.keyboard.press("Tab")
        self.assertEqual("globalSearch", page.locator(":focus").get_attribute("id"))
        page.keyboard.press("Tab")
        self.assertEqual("home", page.locator(":focus").get_attribute("data-view"))

    def test_mobile_viewport_has_no_page_level_horizontal_overflow(self) -> None:
        context = self.browser.new_context(
            locale="en-US", viewport={"width": 390, "height": 844}
        )
        self.addCleanup(context.close)
        page = context.new_page()
        page.goto(f"{self.base}/ui/")
        page.wait_for_selector("#content")
        self.assertTrue(page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        ))

    def test_mobile_navigation_opens_as_a_drawer_and_esc_closes_it(self) -> None:
        context = self.browser.new_context(
            locale="en-US", viewport={"width": 390, "height": 844}
        )
        self.addCleanup(context.close)
        page = context.new_page()
        page.goto(f"{self.base}/ui/")
        page.click("#navToggle")
        self.assertEqual("true", page.get_attribute("#navToggle", "aria-expanded"))
        self.assertEqual("true", page.get_attribute("body", "data-nav-open"))
        page.keyboard.press("Escape")
        self.assertEqual("false", page.get_attribute("#navToggle", "aria-expanded"))
        self.assertEqual("navToggle", page.locator(":focus").get_attribute("id"))
        self.assertTrue(page.locator("#sidebar").evaluate("node => node.inert"))

    def test_default_text_meets_wcag_aa_contrast(self) -> None:
        page = self.open("en-US")
        ratio = page.evaluate("""
          () => {
            const rgb = value => value.match(/[0-9.]+/g).slice(0, 3).map(Number);
            const luminance = value => {
              const channels = rgb(value).map(v => {
                v /= 255;
                return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
              });
              return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
            };
            const style = getComputedStyle(document.body);
            const foreground = luminance(style.color);
            const background = luminance(style.backgroundColor);
            return (Math.max(foreground, background) + 0.05) /
                   (Math.min(foreground, background) + 0.05);
          }
        """)
        self.assertGreaterEqual(ratio, 4.5)


class DiscoveryViewsTests(BrowserE2ETestCase):
    def test_custom_select_matches_button_surface_and_keeps_keyboard_semantics(self) -> None:
        page = self.open("en-US")
        trigger = page.locator(".workspace-history .custom-select-trigger")
        trigger.wait_for()
        self.assertEqual("combobox", trigger.get_attribute("role"))
        self.assertEqual("false", trigger.get_attribute("aria-expanded"))

        trigger.click()
        self.assertEqual("true", trigger.get_attribute("aria-expanded"))
        page.get_by_role("option", name="Running").click()

        selected = page.locator(".workspace-history .custom-select-trigger")
        self.assertIn("Running", selected.inner_text())
        self.assertEqual("running", page.locator(
            ".workspace-history select"
        ).input_value())

        selected.click()
        page.keyboard.press("Escape")
        self.assertEqual("false", selected.get_attribute("aria-expanded"))

    def test_active_goal_is_the_home_workbench_and_exposes_authorised_action(self) -> None:
        service = RunApplicationService(self.db, self.app_service())
        run_id = service.start_run(
            workflow_id="workflow:human", inputs={"value": 3},
            goal="Approve the local release", actor="local",
            idempotency_key="browser-active-workbench",
        ).run_id
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            summary = service.reads.run_summary(EntityId.parse(run_id))
            if summary["status"] == "waiting":
                break
            time.sleep(0.05)
        else:
            self.fail("human workflow never reached waiting")

        try:
            page = self.open("en-US")
            page.wait_for_selector(".active-goal-hero")
            self.assertIn("Approve the local release", page.inner_text(".active-goal-hero"))
            self.assertIn("Current step and next action", page.inner_text(".current-step-panel"))
            self.assertTrue(page.locator(".current-step-panel .actions button").count())
        finally:
            current = service.reads.run_summary(EntityId.parse(run_id))
            service.cancel_run(
                run_id, current["projection_version"], actor="local",
                idempotency_key="browser-active-workbench-cleanup",
            )

    def test_home_uses_dashboard_facts_and_global_search_uses_the_server(self) -> None:
        phrase = "Reconcile lunar invoices"
        run_id = self.start_goal("browser-discovery-search", phrase)
        page = self.open("en-US")
        page.wait_for_function(
            "id => fetch('/api/v1/dashboard').then(r => r.json())"
            ".then(b => b.data.recent_runs.some(run => run.run_id === id))",
            arg=run_id,
        )
        page.reload()
        page.wait_for_selector(".home-hero")
        self.assertIn(phrase, page.inner_text(".goal-list"))

        page.fill("#globalSearch", "lunar invoices")
        page.press("#globalSearch", "Enter")
        page.wait_for_function("() => location.hash === '#/home'")
        page.wait_for_selector(".goal-row")
        self.assertIn(phrase, page.inner_text(".goal-list"))

    def test_goal_deep_link_shows_the_projection_and_opens_the_run(self) -> None:
        phrase = "Prepare quarterly launch brief"
        run_id = self.start_goal("browser-goal-detail", phrase)
        page = self.open("en-US", path=f"/ui/#/goals/{run_id}")
        page.wait_for_selector(".goal-detail")
        self.assertIn(phrase, page.inner_text(".goal-detail"))
        self.assertIn("workflow:linear", page.inner_text(".goal-detail"))
        page.click(".goal-detail >> text=Open run")
        page.wait_for_function("id => location.hash === `#/runs/${encodeURIComponent(id)}`", arg=run_id)


class WorkflowCatalogTests(BrowserE2ETestCase):
    def test_catalog_card_opens_the_immutable_definition(self) -> None:
        page = self.open("en-US", path="/ui/#/workflows")
        card = page.locator(".workflow-card", has_text="Linear")
        card.wait_for()
        self.assertIn("4 nodes", card.inner_text())
        card.locator(".workflow-card-main").click()
        # The drawing opens first; the node list is one tab away.
        page.wait_for_selector(".workflow-detail .workflow-graph")
        self.assertEqual(0, page.locator(".workflow-detail .definition-list").count())
        page.click('.workflow-detail [data-workflow-tab="definition"]')
        page.wait_for_selector(".workflow-detail .definition-list")
        self.assertEqual(0, page.locator(".workflow-detail .workflow-graph").count())
        detail = page.inner_text(".workflow-detail")
        self.assertIn("workflow:linear", detail.lower())
        self.assertIn("collect", detail)
        self.assertIn("transform@1.0.0", detail)

    def test_the_definition_is_drawn_from_the_server_layout(self) -> None:
        page = self.open("en-US", path="/ui/#/workflows")
        card = page.locator(".workflow-card", has_text="Linear")
        card.wait_for()
        card.locator(".workflow-card-main").click()
        page.wait_for_selector(".workflow-graph")
        graph = page.evaluate(
            """() => fetch('/api/v1/workflows/workflow%3Alinear')
                 .then(r => r.json()).then(b => b.data.graph)"""
        )
        # Every node is a box, and each box sits where the server put it:
        # depth is the column, lane the row.
        self.assertEqual(
            len(graph["nodes"]), page.locator(".workflow-graph .graph-box").count()
        )
        self.assertEqual(
            len(graph["edges"]), page.locator(".workflow-graph .graph-edge").count()
        )
        columns = page.evaluate(
            """() => [...document.querySelectorAll('.workflow-graph .graph-box')]
                 .map(g => ({
                   id: g.querySelector('.graph-box-id').textContent,
                   x: Number(g.getAttribute('transform').match(/translate\\(([-\\d.]+)/)[1]),
                 }))"""
        )
        depth = {p["node_id"]: p["depth"] for p in graph["layout"]["positions"]}
        ordered = sorted(columns, key=lambda box: box["x"])
        self.assertEqual(
            [depth[box["id"]] for box in ordered],
            sorted(depth[box["id"]] for box in ordered),
        )

    def test_catalog_network_failure_is_not_reported_as_invalid_workflow(self) -> None:
        page = self.open("en-US")
        page.route("**/api/v1/workflows", lambda route: route.abort())
        page.click("#newRun")
        page.wait_for_selector("#liveRegion.error")
        self.assertIn("catalog is unavailable", page.inner_text("#liveRegion"))
        self.assertNotIn("Choose a published workflow", page.inner_text("#liveRegion"))

    def test_unsupported_input_schema_falls_back_to_validated_json(self) -> None:
        page = self.open("en-US")

        def force_json_mode(route):
            response = route.fetch()
            payload = response.json()
            for entry in payload["data"]["workflows"]:
                entry["input_mode"] = "json"
            route.fulfill(response=response, json=payload)

        page.route("**/api/v1/workflows", force_json_mode)
        page.click("#newRun")
        page.check('input[name="workflow"][value="workflow:linear"]')
        page.click('[data-wizard-next]')
        page.wait_for_selector("#newRunInput")
        self.assertIn("complete JSON object", page.inner_text(".wizard-content"))
        page.fill("#newRunGoal", "Exercise JSON fallback")
        page.fill("#newRunInput", "{not json")
        page.click('[data-wizard-next]')
        page.wait_for_selector(".wizard-problem:not([hidden])")
        self.assertIn("valid JSON object", page.inner_text(".wizard-problem"))

    def test_agent_goal_binding_hides_json_and_builds_the_input(self) -> None:
        page = self.open("en-US")

        def advertise_goal_binding(route):
            response = route.fetch()
            payload = response.json()
            for entry in payload["data"]["workflows"]:
                if entry["workflow_id"] != "workflow:linear":
                    continue
                entry["input_mode"] = "structured"
                entry["inputs"] = [{
                    "id": "prompt", "schema_id": "schema://object/1.0",
                    "required": True, "has_default": False, "default": None,
                    "description": "", "schema": {"type": "object"},
                    "transport": "inline",
                }]
                entry["goal_binding"] = {
                    "source": "run.goal", "node_id": "collect",
                    "input_id": "prompt", "property": "goal",
                    "value_shape": "object",
                }
            route.fulfill(response=response, json=payload)

        captured = {}

        def capture_start(route):
            if route.request.method != "POST":
                return route.continue_()
            captured.update(route.request.post_data_json)
            route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({
                    "schema_version": "1.0", "projection_version": None,
                    "data": {"run_id": "run:goal-bound"}, "next_cursor": None,
                }),
            )

        page.route("**/api/v1/workflows", advertise_goal_binding)
        page.route("**/api/v1/runs", capture_start)
        page.click("#newRun")
        page.check('input[name="workflow"][value="workflow:linear"]')
        page.click("[data-wizard-next]")
        self.assertIn("no JSON is required", page.inner_text(".wizard-content"))
        self.assertFalse(page.locator("#newRunInput").is_visible())
        page.fill("#newRunGoal", "Build a release dashboard")
        page.click("details.advanced-input summary")
        page.fill("#newRunInput", '{"prompt":{"context":"game"}}')
        page.click("[data-wizard-next]")
        self.assertIn("Automatically bound to input prompt", page.inner_text(".wizard-content"))
        page.click("[data-wizard-next]")
        page.click("#newGoalStart")
        page.wait_for_function("() => location.hash.includes('goal-bound')")
        self.assertEqual("Build a release dashboard", captured["goal"])
        self.assertEqual(
            {"goal": "Build a release dashboard", "context": "game"},
            captured["input"]["prompt"],
        )

    def test_workflow_removed_after_review_is_reported_before_mutation(self) -> None:
        page = self.open("en-US")
        calls = {"count": 0}

        def catalog(route):
            calls["count"] += 1
            if calls["count"] == 1:
                route.continue_()
            else:
                route.fulfill(
                    status=200, content_type="application/json",
                    body=json.dumps({
                        "schema_version": "1.0", "data": {"workflows": []},
                        "next_cursor": None,
                    }),
                )

        page.route("**/api/v1/workflows", catalog)
        page.click("#newRun")
        page.wait_for_selector("dialog[open]")
        self.complete_goal_wizard(
            page, "workflow:linear", goal="A workflow that may disappear",
            inputs={"value": 7},
        )
        page.wait_for_selector(".wizard-problem:not([hidden])")
        self.assertIn("no longer available", page.inner_text(".wizard-problem"))
        self.assertTrue(page.is_visible("dialog[open]"))

    def test_new_workflow_version_requires_a_fresh_review(self) -> None:
        page = self.open("en-US")
        calls = {"count": 0}

        def catalog(route):
            calls["count"] += 1
            response = route.fetch()
            payload = response.json()
            if calls["count"] > 1:
                next(
                    entry for entry in payload["data"]["workflows"]
                    if entry["workflow_id"] == "workflow:linear"
                )["latest_version"] = 2
            route.fulfill(response=response, json=payload)

        page.route("**/api/v1/workflows", catalog)
        page.click("#newRun")
        self.complete_goal_wizard(
            page, "workflow:linear", goal="Review a pinned version",
            inputs={"value": 9},
        )
        page.wait_for_selector("#liveRegion.error")
        self.assertIn("changed after review", page.inner_text("#liveRegion"))
        self.assertFalse(page.is_visible("dialog[open]"))


GENERATED_WORKFLOW = {
    "dsl_version": "1.2",
    "metadata": {"id": "prompted", "name": "Prompted flow"},
    "nodes": [
        {
            "id": "work", "kind": "action",
            "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
            "outputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
            "handler": {"name": "transform", "version": "1.0.0"},
        },
        {
            "id": "done", "kind": "terminal",
            "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
        },
    ],
    "edges": [{
        "id": "flow", "from": {"node": "work", "port": "value"},
        "to": {"node": "done", "port": "value"},
    }],
    "entry": ["work"], "terminals": ["done"],
}


class GenerateWorkflowTests(BrowserE2ETestCase):
    """描述 → 生成 → 预览 → 发布 → 运行, entirely through the browser.

    The model is a scripted fake wired through the composition's generator
    seam; everything else — validation, the advertised publish command, the
    catalog refresh and the run — is the production path.
    """

    @classmethod
    def extra_app_kwargs(cls) -> dict:
        return {
            "workflow_generator": lambda prompt: json.dumps(GENERATED_WORKFLOW),
        }

    def test_cancel_leaves_the_generate_dialog_without_a_prompt(self) -> None:
        """Abandoning the dialog must not require filling the field first."""

        page = self.open("en-US", path="/ui/#/workflows")
        page.wait_for_selector("#generateWorkflow")
        page.click("#generateWorkflow")
        page.wait_for_selector("#generateInstruction")
        page.locator("dialog[open] button", has_text="Cancel").click()
        page.wait_for_selector("dialog", state="detached")

    def test_cancel_leaves_the_goal_wizard_without_a_goal(self) -> None:
        page = self.open("en-US")
        page.click("#newRun")
        page.wait_for_selector("dialog[open]")
        page.check('input[name="workflow"][value="workflow:linear"]')
        page.click("[data-wizard-next]")
        page.wait_for_selector("#newRunGoal")
        page.locator("dialog[open] button", has_text="Cancel").click()
        page.wait_for_selector("dialog", state="detached")

    def test_a_described_workflow_is_published_and_runs(self) -> None:
        page = self.open("zh-CN", path="/ui/#/workflows")
        page.click("#generateWorkflow")
        page.wait_for_selector("dialog[open]")
        page.fill("#generateInstruction", "先转换输入，然后结束")
        page.click("#generateSubmit")

        # Preview: the draft names the workflow and its nodes before anything
        # is written.
        page.wait_for_selector("#generatePublish")
        # The eyebrow style upper-cases the id visually; compare content.
        preview = page.inner_text("dialog[open]").lower()
        self.assertIn("workflow:prompted", preview)
        self.assertIn("transform@1.0.0", preview)

        page.click("#generatePublish")
        page.wait_for_selector(".workflow-card:has-text('Prompted flow')")

        # The published workflow starts a run through the ordinary wizard.
        page.locator(
            ".workflow-card", has_text="Prompted flow"
        ).locator(".workflow-card-main").click()
        page.wait_for_selector(".workflow-detail .workflow-tabs")
        page.locator(".workflow-detail button", has_text="新建目标").click()
        page.wait_for_selector("dialog[open]")
        self.complete_goal_wizard(
            page, "workflow:prompted", goal="试运行生成的工作流",
            inputs={"value": 5},
        )
        page.wait_for_function("() => location.hash.startsWith('#/runs/run')")
        run_id = page.evaluate(
            "() => decodeURIComponent(location.hash.split('/')[2])"
        )
        self.wait_for_status(page, run_id, "succeeded")

    def test_default_agent_is_prompt_context_and_preview_is_read_only(self) -> None:
        page = self.open("en-US", path="/ui/#/workflows")
        captured = {"generate": None, "validate": None}
        agent_source = {
            "dsl_version": "1.2",
            "metadata": {"id": "agent-prompted", "name": "Agent prompted"},
            "nodes": [
                {
                    "id": "work", "kind": "action",
                    "inputs": [{"id": "prompt", "schema_id": "schema://object/1.0"}],
                    "outputs": [{"id": "result", "schema_id": "schema://object/1.0"}],
                    "handler": {"name": "agent.claude", "version": "1.0.0"},
                    "config": {"prompt": "Do the work"},
                },
                {
                    "id": "done", "kind": "terminal",
                    "inputs": [{"id": "result", "schema_id": "schema://object/1.0"}],
                },
            ],
            "edges": [{
                "id": "flow", "from": {"node": "work", "port": "result"},
                "to": {"node": "done", "port": "result"},
            }],
            "entry": ["work"], "terminals": ["done"],
        }

        def handlers(route):
            response = route.fetch()
            payload = response.json()
            payload["data"]["handlers"].extend([
                {
                    "name": "agent.claude", "version": "1.0.0",
                    "registration_status": "registered",
                    "capabilities": ["agent.invoke"],
                },
                {
                    "name": "agent.codex", "version": "2.0.0",
                    "registration_status": "registered",
                    "capabilities": ["agent.invoke"],
                },
            ])
            route.fulfill(response=response, json=payload)

        def commands(source, definition_hash):
            return [
                {
                    "command": "workflow.publish", "method": "POST",
                    "href": "/api/v1/workflows/workflow:agent-prompted/versions",
                    "target_aggregate_id": "workflow:agent-prompted",
                    "expected_version": 0,
                },
                {
                    "command": "workflow.validate", "method": "POST",
                    "href": "/api/v1/workflows/validate",
                    "target_aggregate_id": "workflow:agent-prompted",
                    "expected_version": 0,
                },
            ]

        def generate(route):
            captured["generate"] = route.request.post_data_json
            source = json.dumps(agent_source)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "schema_version": "1.0", "projection_version": None,
                "data": {
                    "source": source, "workflow_id": "workflow:agent-prompted",
                    "definition_hash": "sha256:first", "node_count": 2,
                    "attempts": 1, "latest_version": 0,
                    "allowed_commands": commands(source, "sha256:first"),
                },
                "next_cursor": None,
            }))

        def validate(route):
            captured["validate"] = route.request.post_data_json
            source = captured["validate"]["source"]
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "schema_version": "1.0", "projection_version": None,
                "data": {
                    "source": source, "workflow_id": "workflow:agent-prompted",
                    "definition_hash": "sha256:edited", "node_count": 2,
                    "latest_version": 0,
                    "allowed_commands": commands(source, "sha256:edited"),
                },
                "next_cursor": None,
            }))

        page.route("**/api/v1/handler-catalog", handlers)
        page.route("**/api/v1/workflows/generate", generate)
        page.route("**/api/v1/workflows/validate", validate)
        page.click("#generateWorkflow")
        default_agent = page.locator("#generateDefaultAgent").locator("..")
        default_agent.get_by_role("combobox").wait_for()
        default_agent.get_by_role("combobox").click()
        page.get_by_role("option", name="agent.codex@2.0.0").click()
        page.fill("#generateInstruction", "Build with agents")
        page.click("#generateSubmit")
        page.wait_for_selector("#generatePublish")
        self.assertEqual("agent.codex", captured["generate"]["default_agent"])
        self.assertEqual(0, page.locator(".draft-agent-select").count())
        self.assertIsNone(captured["validate"])


class WorkflowEditorTests(BrowserE2ETestCase):
    """Agent-only edit v1 → publish v2; a v1 run keeps its version."""

    @classmethod
    def extra_app_kwargs(cls) -> dict:
        from tests.test_workflow_drafts import dsl as editable_dsl

        return {
            "workflow_generator": lambda _prompt: json.dumps(
                editable_dsl(name="Draftable, edited")
            ),
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
        from tests.test_workflow_drafts import dsl as editable_dsl

        catalogs = WorkflowCatalogs(
            InMemoryHandlerCatalog([transform_registration().manifest]),
            InMemorySchemaCatalog(SCHEMAS),
            InMemoryExtensionRegistry(),
        )
        WorkflowDefinitionService(
            catalogs, SQLiteWorkflowVersionStore(cls.db)
        ).publish_workflow(
            json.dumps(editable_dsl()), source_name="<fixture>",
            source_format="json", expected_latest_version=0, actor="fixture",
        )

    def test_editing_publishes_v2_and_the_v1_run_is_untouched(self) -> None:
        page = self.open("en-US")
        page.click("#newRun")
        page.wait_for_selector("dialog[open]")
        self.complete_goal_wizard(
            page, "workflow:draftable", goal="Run before editing",
            inputs={"value": 3},
        )
        page.wait_for_function("() => location.hash.startsWith('#/runs/run')")
        run_id = page.evaluate(
            "() => decodeURIComponent(location.hash.split('/')[2])"
        )
        self.wait_for_status(page, run_id, "succeeded")

        page.goto(f"{self.base}/ui/#/workflows")
        card = page.locator(".workflow-card", has_text="Draftable")
        card.wait_for()
        card.locator(".workflow-card-main").click()
        page.wait_for_selector("#editWorkflow")
        page.click("#editWorkflow")
        page.wait_for_selector("#draftRevisionInstruction")
        page.fill("#draftRevisionInstruction", "Rename this workflow to Draftable, edited")
        page.click("#draftRevise")
        page.wait_for_selector("#draftAccept", timeout=15000)
        self.assertIn("Draftable, edited", page.text_content("#draftSourcePreview"))
        page.click("#draftAccept")
        page.wait_for_selector("#draftPublish", timeout=15000)
        page.click("#draftPublish")

        page.wait_for_function("() => location.hash === '#/workflows'")
        page.wait_for_selector(".workflow-card:has-text('Draftable, edited')")

        # The pre-edit run still names v1 — published versions are immutable
        # and runs pin the version they started with.
        summary = page.evaluate(
            "id => fetch(`/api/v1/runs/${encodeURIComponent(id)}`)"
            ".then(r => r.json()).then(b => b.data)",
            arg=run_id,
        )
        self.assertEqual(1, summary["workflow_version"])

    def test_only_the_agent_prompt_can_modify_the_draft(self) -> None:
        page = self.open("en-US", path="/ui/#/workflows")
        card = page.locator(".workflow-card", has_text="Draftable")
        card.wait_for()
        card.locator(".workflow-card-main").click()
        page.click("#editWorkflow")
        page.wait_for_selector("#draftRevisionInstruction")
        self.assertEqual(1, page.locator("#draftRevisionInstruction").count())
        self.assertEqual(1, page.locator("#draftSourcePreview").count())
        self.assertEqual(0, page.locator("#draftSource").count())
        self.assertEqual(0, page.locator("#draftApplyMetadata").count())
        self.assertEqual(0, page.locator("#draftApplyNode").count())
        self.assertEqual(0, page.locator("#draftValidate").count())

    def test_empty_agent_instruction_is_rejected_in_the_browser(self) -> None:
        page = self.open("en-US", path="/ui/#/workflows")
        card = page.locator(".workflow-card", has_text="Draftable")
        card.wait_for()
        card.locator(".workflow-card-main").click()
        page.click("#editWorkflow")
        page.wait_for_selector("#draftRevisionInstruction")
        page.click("#draftRevise")
        self.assertTrue(page.locator("#draftRevisionInstruction").evaluate(
            "node => !node.checkValidity()"
        ))


class WorkflowRevisionJobTests(BrowserE2ETestCase):
    """A revision is a durable job: it survives a reload and can be cancelled."""

    gate = threading.Event()

    @classmethod
    def extra_app_kwargs(cls) -> dict:
        from tests.test_workflow_drafts import dsl as editable_dsl

        def generator(_prompt: str) -> str:
            # Hold the agent so the browser sees the job mid-flight.
            cls.gate.wait(20)
            return json.dumps(editable_dsl(name="Draftable, edited"))

        return {"workflow_generator": generator}

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
        from tests.test_workflow_drafts import dsl as editable_dsl

        catalogs = WorkflowCatalogs(
            InMemoryHandlerCatalog([transform_registration().manifest]),
            InMemorySchemaCatalog(SCHEMAS),
            InMemoryExtensionRegistry(),
        )
        WorkflowDefinitionService(
            catalogs, SQLiteWorkflowVersionStore(cls.db)
        ).publish_workflow(
            json.dumps(editable_dsl()), source_name="<fixture>",
            source_format="json", expected_latest_version=0, actor="fixture",
        )

    def setUp(self) -> None:
        super().setUp()
        type(self).gate.clear()
        self.addCleanup(type(self).gate.set)

    def _open_editor(self):
        page = self.open("en-US", path="/ui/#/workflows")
        card = page.locator(".workflow-card", has_text="Draftable")
        card.wait_for()
        card.locator(".workflow-card-main").click()
        page.wait_for_selector("#editWorkflow")
        page.click("#editWorkflow")
        page.wait_for_selector("#draftRevisionInstruction")
        return page

    def test_an_in_flight_revision_survives_a_reload_and_can_be_cancelled(self) -> None:
        page = self._open_editor()
        page.fill("#draftRevisionInstruction", "Rename this workflow")
        page.click("#draftRevise")
        page.wait_for_selector("#revisionProgress", timeout=15000)
        # Wait for the dispatcher to actually claim it: cancelling a queued job
        # is a different (and much easier) path than cancelling a running one.
        draft_id = page.evaluate(
            "() => decodeURIComponent(location.hash.split('/edit/')[1])"
        )
        page.wait_for_function(
            "id => fetch(`/api/v1/workflow-drafts/${id}`).then(r => r.json())"
            ".then(b => b.data.pending_revision?.status === 'running')",
            arg=draft_id, timeout=15000,
        )

        # A reload rebuilds the view from the server, not from page memory.
        page.reload()
        page.wait_for_selector("#revisionProgress", timeout=15000)
        self.assertEqual(1, page.locator("#draftCancelRevision").count())
        self.assertEqual(0, page.locator("#draftRevise").count())

        page.click("#draftCancelRevision")
        # Release the agent only once the cancel is recorded; otherwise the
        # answer can land first and the candidate legitimately stands.
        page.wait_for_function(
            "id => fetch(`/api/v1/workflow-drafts/${id}`).then(r => r.json())"
            ".then(b => b.data.pending_revision?.cancel_requested === true)",
            arg=draft_id, timeout=15000,
        )
        type(self).gate.set()
        # The agent answer is discarded: the prompt comes back, no candidate.
        page.wait_for_selector("#draftRevise", timeout=15000)
        self.assertEqual(0, page.locator("#draftAccept").count())
        self.assertNotIn("Draftable, edited", page.text_content("#draftSourcePreview"))


class WorkflowEditorP4Tests(BrowserE2ETestCase):
    """The Agent compiler funnel replaces client-side edge/policy editing."""

    @classmethod
    def extra_app_kwargs(cls) -> dict:
        from tests.test_workflow_drafts import dsl as editable_dsl

        return {
            "workflow_generator": lambda _prompt: json.dumps(
                editable_dsl("p4-editor", "P4 Agent revision")
            ),
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
        from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
        from tests.test_workflow_drafts import dsl as editable_dsl

        catalogs = WorkflowCatalogs(
            InMemoryHandlerCatalog([transform_registration().manifest]),
            InMemorySchemaCatalog(SCHEMAS), InMemoryExtensionRegistry(),
        )
        WorkflowDefinitionService(
            catalogs, SQLiteWorkflowVersionStore(cls.db)
        ).publish_workflow(
            json.dumps(editable_dsl("p4-editor", "P4 Editor")),
            source_name="<fixture>", source_format="json",
            expected_latest_version=0, actor="fixture",
        )

    def test_edge_policy_failures_are_located_and_recoverable(self) -> None:
        page = self.open("en-US", path="/ui/#/workflows")
        card = page.locator(".workflow-card", has_text="P4 Editor")
        card.wait_for()
        card.locator(".workflow-card-main").click()
        page.locator(
            ".workflow-detail .eyebrow", has_text="workflow:p4-editor"
        ).wait_for()
        page.click("#editWorkflow")
        page.wait_for_selector("#draftRevisionInstruction")
        page.fill(
            "#draftRevisionInstruction",
            "Make the routing and retry policies safe and keep all ports compatible",
        )
        page.click("#draftRevise")
        page.wait_for_selector("#draftReject", timeout=15000)
        self.assertIn("P4 Agent revision", page.text_content("#draftSourcePreview"))
        self.assertIn("Change summary", page.inner_text("[data-semantic-diff]"))
        self.assertIn("WORKFLOW FIELDS", page.inner_text("[data-semantic-diff]"))
        self.assertIn("CURRENT DRAFT", page.inner_text(".agent-editor-diff"))
        page.click("#draftReject")
        page.wait_for_function(
            "() => document.querySelector('.agent-editor-history')?.textContent.includes('Rejected')",
            timeout=15000,
        )
        self.assertNotIn("P4 Agent revision", page.text_content("#draftSourcePreview"))
        self.assertEqual(0, page.locator("#draftAddEdge").count())
        self.assertEqual(0, page.locator("#draftAddPolicy").count())


class WorkflowEditorP5Tests(BrowserE2ETestCase):
    """Release hardening: accessibility, offline retry and reload recovery."""

    @classmethod
    def extra_app_kwargs(cls) -> dict:
        from tests.test_workflow_drafts import dsl as editable_dsl

        return {
            "workflow_generator": lambda _prompt: json.dumps(
                editable_dsl("p5-editor", "P5 Agent revision")
            ),
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
        from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
        from tests.test_workflow_drafts import dsl as editable_dsl

        catalogs = WorkflowCatalogs(
            InMemoryHandlerCatalog([transform_registration().manifest]),
            InMemorySchemaCatalog(SCHEMAS), InMemoryExtensionRegistry(),
        )
        definitions = WorkflowDefinitionService(
            catalogs, SQLiteWorkflowVersionStore(cls.db)
        )
        definitions.publish_workflow(
            json.dumps(editable_dsl("p5-editor", "P5 Editor")),
            source_name="<fixture>", source_format="json",
            expected_latest_version=0, actor="fixture",
        )
        definitions.publish_workflow(
            json.dumps(editable_dsl("p5-editor", "P5 Editor v2")),
            source_name="<fixture-v2>", source_format="json",
            expected_latest_version=1, actor="fixture",
        )

    def test_editor_has_no_console_errors(self) -> None:
        context = self.browser.new_context(locale="en-US")
        self.addCleanup(context.close)
        page = context.new_page()
        errors: list[str] = []
        page.on(
            "console",
            lambda message: errors.append(message.text) if message.type == "error" else None,
        )
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"{self.base}/ui/#/workflows")
        card = page.locator(".workflow-card", has_text="P5 Editor v2")
        card.wait_for()
        card.locator(".workflow-card-main").click()
        page.click("#editWorkflow")
        page.wait_for_selector("#draftRevisionInstruction")
        page.fill("#draftRevisionInstruction", "Improve this workflow")
        page.wait_for_timeout(800)
        self.assertEqual([], errors)
        page.click("#draftDiscard")
        page.click("#draftDiscard")
        page.wait_for_function("() => location.hash === '#/workflows'")

    def test_history_can_start_pinned_and_replace_an_active_draft(self) -> None:
        page = self.open("en-US", path="/ui/#/workflows")
        card = page.locator(".workflow-card", has_text="P5 Editor v2")
        card.wait_for()
        card.locator(".workflow-card-main").click()
        page.wait_for_selector("#workflowVersionSelect", state="attached")
        version_picker = page.locator("#workflowVersionSelect").locator("..")
        version_picker.get_by_role("combobox").click()
        version_picker.locator("[role='option'][data-value='1']").click()
        page.wait_for_function(
            "() => document.querySelector('.workflow-detail .eyebrow')?.textContent.includes('v1')"
        )

        page.get_by_role("button", name="New goal").last.click()
        dialog = page.locator("dialog[open]")
        dialog.wait_for()
        self.assertIn("v1", dialog.inner_text())
        self.complete_goal_wizard(
            page, "workflow:p5-editor", goal="Replay v1", inputs={"value": 3},
        )
        page.wait_for_function("() => location.hash.startsWith('#/runs/run')")
        run_id = page.evaluate("() => decodeURIComponent(location.hash.split('/')[2])")
        summary = page.evaluate(
            "id => fetch(`/api/v1/runs/${encodeURIComponent(id)}`)"
            ".then(response => response.json()).then(body => body.data)",
            arg=run_id,
        )
        self.assertEqual(1, summary["workflow_version"])
        self.wait_for_status(page, run_id, "succeeded")
        page.goto(f"{self.base}/ui/#/workflows")
        card = page.locator(".workflow-card", has_text="P5 Editor v2")
        card.wait_for()
        card.locator(".workflow-card-main").click()
        page.wait_for_selector("#workflowVersionSelect", state="attached")
        version_picker = page.locator("#workflowVersionSelect").locator("..")
        version_picker.get_by_role("combobox").click()
        version_picker.locator("[role='option'][data-value='1']").click()
        page.wait_for_function(
            "() => document.querySelector('.workflow-detail .eyebrow')?.textContent.includes('v1')"
        )

        page.click("#editWorkflow")
        page.wait_for_selector("#draftSourcePreview", state="attached")
        page.goto(f"{self.base}/ui/#/workflows")
        card = page.locator(".workflow-card", has_text="P5 Editor v2")
        card.wait_for()
        card.locator(".workflow-card-main").click()
        page.wait_for_selector("#workflowVersionSelect", state="attached")
        version_picker = page.locator("#workflowVersionSelect").locator("..")
        version_picker.get_by_role("combobox").click()
        version_picker.locator("[role='option'][data-value='2']").click()
        page.click("#editWorkflow")
        page.wait_for_selector("#replaceActiveDraft")
        page.click("#replaceActiveDraft")
        page.wait_for_selector("#draftSourcePreview", state="attached")
        self.assertEqual(
            "P5 Editor v2",
            json.loads(page.text_content("#draftSourcePreview"))["metadata"]["name"],
        )

        page.click("#draftDiscard")
        page.click("#draftDiscard")
        page.wait_for_function("() => location.hash === '#/workflows'")

    def test_keyboard_offline_retry_and_reload_recovery(self) -> None:
        page = self.open("en-US", path="/ui/#/workflows")
        draft_responses: list[str] = []
        page.on(
            "response",
            lambda response: draft_responses.append(
                f"{response.status} {response.request.method} {response.url}"
            ) if "/workflow-drafts/" in response.url else None,
        )
        card = page.locator(".workflow-card", has_text="P5 Editor")
        card.wait_for()
        card.locator(".workflow-card-main").click()
        page.locator(
            ".workflow-detail .eyebrow", has_text="workflow:p5-editor"
        ).wait_for()
        page.click("#editWorkflow")
        page.wait_for_selector("#draftRevisionInstruction")
        prompt = page.locator("#draftRevisionInstruction")
        prompt.focus()
        self.assertEqual("draftRevisionInstruction", page.locator(":focus").get_attribute("id"))
        page.fill("#draftRevisionInstruction", "Keep the workflow valid after reconnect")
        page.context.set_offline(True)
        page.click("#draftRevise")
        page.wait_for_function(
            "() => !document.querySelector('#liveRegion').hidden", timeout=15000,
        )
        page.context.set_offline(False)
        page.fill("#draftRevisionInstruction", "Keep the workflow valid after reconnect")
        page.click("#draftRevise")
        page.wait_for_selector("#draftAccept", timeout=15000)
        self.assertIn("P5 Agent revision", page.text_content("#draftSourcePreview"))
        page.click("#draftAccept")
        page.wait_for_selector("#draftUndo", timeout=15000)
        self.assertIn("Accepted", page.inner_text(".agent-editor-history"))
        page.click("#draftUndo")
        page.wait_for_function(
            "() => document.querySelector('.agent-editor-history')?.textContent.includes('Undone')",
            timeout=15000,
        )
        self.assertIn("P5 Editor v2", page.text_content("#draftSourcePreview"))
        self.assertIn("Undone", page.inner_text(".agent-editor-history"))


class NewRunTests(BrowserE2ETestCase):
    def test_a_run_started_from_the_dialog_reaches_its_detail_page(self) -> None:
        page = self.open("en-US")
        page.click("#newRun")
        page.wait_for_selector("dialog[open]")
        self.complete_goal_wizard(
            page, "workflow:linear", goal="Verify the launch", inputs={"value": 3}
        )

        page.wait_for_function("() => location.hash.startsWith('#/runs/run')")
        self.assertIn("Started goal", page.inner_text("#liveRegion"))
        page.wait_for_selector("text=Open responsibilities")

    def test_required_schema_input_is_reported_and_nothing_starts(self) -> None:
        page = self.open("en-US")
        before = page.evaluate(
            "() => fetch('/api/v1/runs?limit=200').then(r => r.json())"
            ".then(b => b.data.runs.length)"
        )
        page.click("#newRun")
        page.wait_for_selector("dialog[open]")
        page.check('input[name="workflow"][value="workflow:linear"]')
        page.click('[data-wizard-next]')
        page.fill("#newRunGoal", "Needs a valid input")
        page.click('[data-wizard-next]')

        self.assertTrue(page.is_visible("dialog[open]"))
        self.assertFalse(page.evaluate("document.querySelector('#newRunInput-value').checkValidity()"))
        after = page.evaluate(
            "() => fetch('/api/v1/runs?limit=200').then(r => r.json())"
            ".then(b => b.data.runs.length)"
        )
        self.assertEqual(before, after)


class HumanTaskTests(BrowserE2ETestCase):
    def test_an_approval_is_completed_from_the_inbox_in_both_locales(self) -> None:
        for index, locale in enumerate(LOCALES):
            with self.subTest(locale=locale):
                page = self.open(locale)
                page.click("#newRun")
                page.wait_for_selector("dialog[open]")
                self.complete_goal_wizard(
                    page, "workflow:human", goal="Review the transformed value",
                    inputs={"value": 3},
                )
                page.wait_for_function("() => location.hash.startsWith('#/runs/run')")
                run_id = page.evaluate(
                    "() => decodeURIComponent(location.hash.split('/')[2])"
                )
                self.wait_for_status(page, run_id, "waiting")
                page.wait_for_function(
                    "id => fetch('/api/v1/inbox?limit=200').then(r => r.json())"
                    ".then(b => b.data.items.some(item => item.run_id === id))",
                    arg=run_id, timeout=15000,
                )

                # The inbox identifies an item by its run and its label — the
                # task id is addressing data, not something the row displays.
                page.click('[data-view="inbox"]')
                page.wait_for_function("() => location.hash === '#/inbox'")
                row = page.locator("tr", has_text=run_id)
                row.wait_for(timeout=15000)

                # The first button is whatever the server advertised first;
                # the test must not assume which command that is.
                approve = row.locator("td").last.locator("button").first
                self.assertIn(
                    approve.inner_text(),
                    {"Approve", "批准"},
                    "the inbox offered an unexpected first command",
                )
                approve.click()

                # The whole journey stays on HTTP: the token is fetched from
                # the server-advertised reissue command, never taken out of
                # the process. This is exactly the surface an operator has
                # after a restart wiped the in-memory delivery.
                page.wait_for_selector("dialog[open]")
                page.click("#humanTokenFetch")
                page.wait_for_function(
                    "() => document.querySelector('#humanToken').value.length > 0"
                )
                issued_token = page.input_value("#humanToken")
                self.assertNotIn(issued_token, page.url)
                self.assertNotIn(
                    issued_token,
                    page.evaluate("() => JSON.stringify({...localStorage})"),
                )
                if locale == "en-US":
                    # Plan B4: malformed JSON stays inside the form — the
                    # dialog remains open, the field is marked, input is kept.
                    page.fill("#humanValue", "{not json")
                    page.click("dialog button[value=confirm]")
                    page.wait_for_selector("#humanValueError:not([hidden])")
                    self.assertTrue(page.is_visible("dialog[open]"))
                    self.assertEqual("{not json", page.input_value("#humanValue"))
                    page.fill("#humanValue", "")
                page.click("dialog button[value=confirm]")

                # The row leaves the inbox because the server stopped listing
                # it, not because the client crossed it off.
                page.wait_for_function(
                    "id => !document.body.innerText.includes(id)", arg=run_id,
                    timeout=15000,
                )
                self.wait_for_status(page, run_id, "succeeded")


class BudgetTests(BrowserE2ETestCase):
    def exhausted_run(self, key: str, total: int = 1_000) -> str:
        """A run whose budget is spent, which is when the UI offers a grant.

        The "Add budget" command is advertised on an exhausted account, so
        driving a real grant from the browser needs a real exhaustion first.
        """

        run_id = self.start_run(key)
        now = datetime.now(timezone.utc)
        budget = BudgetService(self.db)
        budget.open_account(EntityId.parse(run_id), total, actor="local", now=now)
        reservation = budget.reserve(
            EntityId.parse(run_id), EntityId("attempt", key.encode().hex().ljust(64, "0")[:64]),
            total, actor="local", now=now,
        )
        budget.report_usage(
            reservation.reservation_id, 1, total, actor="local", now=now
        )
        return run_id

    def account_total(self, page, run_id: str) -> int:
        return page.evaluate(
            "id => fetch(`/api/v1/runs/${encodeURIComponent(id)}`).then(r => r.json())"
            ".then(b => b.data.budget_summary.total_microunits)",
            run_id,
        )

    def test_a_grant_made_in_the_browser_moves_the_account(self) -> None:
        """The actual gate: click the advertised button, top the account up."""

        run_id = self.exhausted_run("browser-budget-grant")
        page = self.open("en-US", path=f"/ui/#/runs/{run_id}")
        self.assertEqual(1_000, self.account_total(page, run_id))

        grant = page.locator("button", has_text="Add budget").first
        grant.wait_for(timeout=15000)
        grant.click()

        page.wait_for_selector("dialog[open]")
        page.fill("#budgetAmount", "500")
        page.click("dialog button[value=confirm]")

        page.wait_for_function(
            "id => fetch(`/api/v1/runs/${encodeURIComponent(id)}`).then(r => r.json())"
            ".then(b => b.data.budget_summary.total_microunits === 1500)",
            arg=run_id, timeout=15000,
        )

    def test_a_browser_retry_with_the_same_key_tops_up_once(self) -> None:
        """A duplicate HTTP delivery replays one command, not two grants."""

        run_id = self.exhausted_run("browser-budget-duplicate")
        page = self.open("en-US", path=f"/ui/#/runs/{run_id}")
        results = page.evaluate(
            """async id => {
              const projection = await fetch(
                `/api/v1/runs/${encodeURIComponent(id)}/responsibilities`
              ).then(r => r.json());
              const command = projection.data.responsibilities
                .flatMap(item => item.allowed_commands || [])
                .find(item => item.command === 'budget.add');
              const options = {
                method: command.method,
                headers: {
                  'content-type': 'application/json',
                  'idempotency-key': 'browser-duplicate-delivery',
                },
                body: JSON.stringify({
                  expected_version: command.expected_version,
                  amount_microunits: 500,
                }),
              };
              const first = await fetch(command.href, options);
              const second = await fetch(command.href, options);
              return [first.status, second.status];
            }""",
            run_id,
        )
        self.assertEqual([200, 200], results)
        self.assertEqual(1_500, self.account_total(page, run_id))

    def test_a_stale_advertised_command_refreshes_after_409(self) -> None:
        run_id = self.exhausted_run("browser-budget-conflict")
        page = self.open("en-US", path=f"/ui/#/runs/{run_id}")
        grant = page.locator("button", has_text="Add budget").first
        grant.wait_for(timeout=15000)
        version = page.evaluate(
            """async id => {
              const response = await fetch(
                `/api/v1/runs/${encodeURIComponent(id)}/responsibilities`
              ).then(r => r.json());
              return response.data.responsibilities
                .flatMap(item => item.allowed_commands || [])
                .find(item => item.command === 'budget.add').expected_version;
            }""",
            run_id,
        )
        BudgetService(self.db).add_budget(
            EntityId.parse(run_id), 100, expected_version=version, actor="local",
            now=datetime.now(timezone.utc), idempotency_key="budget-conflict-winner",
        )

        grant.click()
        page.fill("#budgetAmount", "500")
        page.click("dialog button[value=confirm]")
        page.wait_for_function(
            "() => document.querySelector('#liveRegion').textContent"
            ".includes('changed after you loaded it')",
            timeout=15000,
        )
        page.wait_for_function(
            "() => ![...document.querySelectorAll('button')]"
            ".some(button => button.textContent === 'Add budget')",
            timeout=15000,
        )

    def test_the_grant_button_is_translated(self) -> None:
        run_id = self.exhausted_run("browser-budget-zh")
        page = self.open("zh-CN", path=f"/ui/#/runs/{run_id}")
        grant = page.locator("button", has_text="追加预算").first
        grant.wait_for(timeout=15000)

    def test_an_exhausted_run_says_so(self) -> None:
        run_id = self.exhausted_run("browser-budget-exhausted")
        page = self.open("en-US", path=f"/ui/#/runs/{run_id}")
        page.wait_for_selector("text=Budget exhausted", timeout=15000)

    def test_the_budget_unit_is_shown_with_the_number(self) -> None:
        """A number without its unit is how microunits get read as dollars."""

        run_id = self.start_run("browser-budget-unit")
        BudgetService(self.db).open_account(
            EntityId.parse(run_id), 2_500, actor="local",
            now=datetime.now(timezone.utc),
        )
        page = self.open("en-US", path=f"/ui/#/runs/{run_id}")
        page.wait_for_selector("text=microunits")
        text = page.inner_text("#content")
        self.assertRegex(text, r"2[,.]?500")


class CancelTests(BrowserE2ETestCase):
    def parked_run(self, key: str) -> str:
        """A run waiting on a person, so a responsibility is reliably present.

        Racing a fast handler produced a test that skipped itself; parking the
        run on a HumanTask makes the responsibility deterministic.
        """

        run_id = self.start_run(key)
        HumanTaskService(self.db).create(
            EntityId.parse(run_id), HumanTaskKind.APPROVAL, {"q": key},
            actor="local", now=datetime.now(timezone.utc), participants=["local"],
        )
        return run_id

    def test_a_run_parked_on_a_person_can_still_be_called_off(self) -> None:
        run_id = self.parked_run("browser-cancel")
        page = self.open("en-US", path=f"/ui/#/runs/{run_id}")

        # The button exists only because the server advertised run.cancel.
        cancel = page.locator("button.danger").first
        cancel.wait_for(timeout=15000)
        self.assertEqual("Cancel run", cancel.inner_text())
        cancel.click()
        page.locator("dialog[open]").get_by_role(
            "button", name="Cancel run", exact=True
        ).click()

        page.wait_for_function(
            "id => fetch(`/api/v1/runs/${encodeURIComponent(id)}`)"
            ".then(r => r.json()).then(b => b.data.status === 'cancelled')",
            arg=run_id, timeout=15000,
        )

    def test_the_cancel_button_is_translated(self) -> None:
        run_id = self.parked_run("browser-cancel-zh")
        page = self.open("zh-CN", path=f"/ui/#/runs/{run_id}")
        cancel = page.locator("button.danger").first
        cancel.wait_for(timeout=15000)
        self.assertEqual("取消运行", cancel.inner_text())

    def test_scope_is_rechecked_after_a_command_was_advertised(self) -> None:
        run_id = self.parked_run("browser-cancel-forbidden")
        page = self.open("en-US", path=f"/ui/#/runs/{run_id}")
        cancel = page.locator("button.danger").first
        cancel.wait_for(timeout=15000)

        self.scopes.remove(WRITE_SCOPE)
        self.addCleanup(self.scopes.add, WRITE_SCOPE)
        cancel.click()
        page.locator("dialog[open]").get_by_role(
            "button", name="Cancel run", exact=True
        ).click()
        page.wait_for_selector("#liveRegion:not([hidden])")
        self.assertIn("not allowed", page.inner_text("#liveRegion"))


class PlanAndRecoveryTests(BrowserE2ETestCase):
    def test_the_plan_panel_keeps_definition_and_overlay_apart(self) -> None:
        run_id = self.start_run("browser-plan")
        page = self.open("en-US", path=f"/ui/#/runs/{run_id}")
        page.click('[data-run-tab="plan"]')
        page.wait_for_selector("button[data-view=definition]")

        definition = page.inner_text("button[data-view=definition] >> xpath=../..")
        self.assertIn("transform", definition)

        page.click("button[data-view=overlay]")
        page.wait_for_selector("text=/Run state for plan v/i")
        # `.eyebrow` renders uppercase, so compare case-insensitively rather
        # than pinning a presentation detail.
        overlay = page.inner_text("#content").lower()
        self.assertIn("run state for plan v1", overlay)
        self.assertIn("generation 1", overlay)

    def test_the_ops_page_reports_factual_operational_sections(self) -> None:
        page = self.open("en-US", path="/ui/#/ops")
        page.wait_for_selector("text=Integrity")
        text = page.inner_text("#content")
        self.assertIn("SQLite quick-check passed", text)
        self.assertIn("Scanned", text)
        self.assertIn("Capacity", text)
        self.assertIn("Durable state", text)

    def test_agents_are_registration_facts_not_fake_health(self) -> None:
        page = self.open("en-US", path="/ui/#/agents")
        page.wait_for_selector("text=Registered agents")
        text = page.inner_text("#content")
        self.assertIn("does not collect heartbeats", text)
        self.assertIn("No Agent handlers are registered", text)
        self.assertNotIn("transform", text)
        self.assertNotIn("Online", text)
        self.assertNotIn("P95", text)

    def test_settings_persist_refresh_interval_locally(self) -> None:
        page = self.open("en-US", path="/ui/#/settings")
        select = page.get_by_role("combobox", name="Live refresh interval")
        select.wait_for(timeout=15000)
        select.click()
        page.get_by_role("option", name="30 seconds").click()
        self.assertEqual(
            "30", page.evaluate("localStorage.getItem('orbit.refreshSeconds')")
        )
        self.assertIn("Server configuration (read-only)", page.inner_text("#content"))

    def test_a_failed_run_is_diagnosable_from_the_errors_panel(self) -> None:
        """The panel an operator opens when something breaks must say why."""

        service = RunApplicationService(self.db, self.app_service())
        run_id = service.start_run(
            workflow_id="workflow:linear", inputs={"value": "not-an-integer"},
            actor="local", idempotency_key="browser-failure",
        ).run_id

        page = self.open("en-US", path=f"/ui/#/runs/{run_id}")
        self.wait_for_status(page, run_id, "failed")
        page.reload()
        page.click('[data-run-tab="errors"]')
        page.wait_for_selector("text=is not of type", timeout=15000)

        errors = page.inner_text("#content")
        self.assertIn("validation_error", errors)
        self.assertIn("handler", errors)


class DataAndRecoverySurfaceTests(BrowserE2ETestCase):
    def test_run_data_and_lineage_are_visible(self) -> None:
        run_id = self.start_run("browser-data")
        page = self.open("en-US", path=f"/ui/#/runs/{run_id}")
        page.click('[data-run-tab="data"]')
        page.wait_for_selector("text=Data and artifacts")
        page.get_by_role("button", name="Show lineage").first.click()
        page.wait_for_selector("text=No lineage links recorded.")

    def test_an_expired_human_finding_is_applied_from_ops(self) -> None:
        run_id = self.start_run("browser-recovery-apply")
        now = datetime.now(timezone.utc)
        task_id, _token = HumanTaskService(self.db).create(
            EntityId.parse(run_id), HumanTaskKind.APPROVAL,
            {"question": "expired?"}, actor="local", now=now,
            participants=["local"], deadline_at=now - timedelta(seconds=1),
        )

        page = self.open("en-US", path="/ui/#/ops")
        apply = page.get_by_role("button", name="Apply recovery").first
        apply.wait_for(timeout=15000)
        apply.click()
        page.get_by_role("button", name="Apply", exact=True).click()
        page.wait_for_function(
            "id => fetch('/api/v1/inbox').then(r => r.json())"
            ".then(b => !b.data.items.some(item => item.task_id === id))",
            arg=str(task_id), timeout=15000,
        )

    def test_a_stale_recovery_selection_reports_partial_failure(self) -> None:
        run_id = self.start_run("browser-recovery-stale")
        now = datetime.now(timezone.utc)
        task_id, _token = HumanTaskService(self.db).create(
            EntityId.parse(run_id), HumanTaskKind.APPROVAL,
            {"question": "stale recovery?"}, actor="local", now=now,
            participants=["local"], deadline_at=now - timedelta(seconds=1),
        )

        page = self.open("en-US", path="/ui/#/ops")
        row = page.locator(".actions", has_text=str(task_id))
        apply = row.get_by_role("button", name="Apply recovery")
        apply.wait_for(timeout=15000)

        applied = page.evaluate(
            """async runId => {
              const scan = await fetch('/api/v1/recovery').then(r => r.json());
              const finding = scan.data.findings.find(item => item.run_id === runId);
              const response = await fetch('/api/v1/recovery/apply', {
                method: 'POST',
                headers: {
                  'content-type': 'application/json',
                  'idempotency-key': 'browser-recovery-winner',
                },
                body: JSON.stringify({
                  expected_version: finding.expected_version,
                  action_ids: [finding.action_id],
                }),
              });
              return response.status;
            }""",
            run_id,
        )
        self.assertEqual(200, applied)

        apply.click()
        page.get_by_role("button", name="Apply", exact=True).click()
        page.wait_for_function(
            "() => document.querySelector('#liveRegion').textContent"
            ".includes('could not be applied')",
            timeout=15000,
        )
        self.assertIn("1 of 1", page.inner_text("#liveRegion"))


class ArtifactCatalogTests(BrowserE2ETestCase):
    def artifact(self) -> str:
        from orbit.workflow.persistence.database import connect_workflow_database

        run_id = self.start_run("browser-artifact")
        receipt = self.artifact_backend.write(
            b"reviewable artifact text", max_size_bytes=1024
        )
        artifact_id = f"artifact:{receipt.checksum.value.removeprefix('sha256:')}"
        with connect_workflow_database(self.db) as connection:
            event_id = connection.execute(
                "SELECT event_id FROM run_events WHERE run_id=? ORDER BY global_position LIMIT 1",
                (run_id,),
            ).fetchone()[0]
            now = "2026-01-01T00:00:00+00:00"
            connection.execute(
                "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact_id, run_id, "workflow:linear", "attempt", "attempt:browser",
                    "node_run:browser", "report", "schema:text", "text/plain",
                    receipt.checksum.value, receipt.size_bytes, receipt.blob_key,
                    "run", run_id, "committed", now, now, event_id,
                ),
            )
            connection.execute(
                "INSERT INTO artifact_acl VALUES (?,'local','read','local',?)",
                (artifact_id, now),
            )
            connection.execute(
                "INSERT INTO artifact_links VALUES (?,?,?,?,?,?,?,?)",
                (
                    "artifact_link:browser-producer", "workflow:linear", run_id,
                    artifact_id, "producer", "attempt:browser", event_id, now,
                ),
            )
            connection.commit()
        return artifact_id

    def test_catalog_detail_lineage_preview_and_reload(self) -> None:
        artifact_id = self.artifact()
        page = self.open("en-US", path="/ui/#/artifacts")
        page.wait_for_selector("text=report")
        page.locator(".artifact-card-main").first.click()
        page.wait_for_function(
            "id => location.hash === `#/artifacts/${encodeURIComponent(id)}`",
            arg=artifact_id,
        )
        page.wait_for_selector(".artifact-detail .panel-title")
        self.assertIn("attempt:browser", page.inner_text("#content"))
        page.get_by_role("button", name="Load text preview").click()
        page.wait_for_selector("text=reviewable artifact text")
        page.reload()
        page.wait_for_selector("text=Producer and consumer lineage")
        self.assertIn(artifact_id, page.inner_text("#content"))


class RefreshTests(BrowserE2ETestCase):
    def test_agents_view_only_shows_agent_handlers(self) -> None:
        context = self.browser.new_context(locale="en-US")
        self.addCleanup(context.close)
        page = context.new_page()
        page.route(
            "**/api/v1/handler-catalog",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "schema_version": "1.0",
                    "projection_version": None,
                    "next_cursor": None,
                    "data": {
                        "handlers": [
                            {
                                "name": "transform", "version": "1.0.0",
                                "capabilities": [], "recent_attempt": None,
                            },
                            {
                                "name": "agent.codex", "version": "2.0.0",
                                "capabilities": ["agent.invoke"],
                                "recent_attempt": None,
                            },
                        ],
                        "agents": [
                            {
                                "name": "agent.codex", "agent": "codex",
                                "version": "2.0.0",
                                "capabilities": ["agent.invoke"],
                                "registration_status": "discovered",
                            },
                            {
                                "name": "agent.hermes", "agent": "hermes",
                                "version": "0.18.2",
                                "capabilities": ["agent.invoke"],
                                "registration_status": "discovered",
                            },
                        ],
                        "status_semantics": "registration_only",
                    },
                }),
            ),
        )
        page.goto(f"{self.base}/ui/#/agents")
        page.wait_for_selector(".agent-card")

        self.assertEqual(2, page.locator("#content section.panel").count())
        content = page.inner_text("#content")
        self.assertIn("Registered agents", content)
        # Name and version render on their own lines now.
        self.assertIn("agent.codex", content)
        self.assertIn("2.0.0", content)
        self.assertNotIn("transform 1.0.0", content)
        # A discovered-but-unregistered CLI is listed once, in the detected
        # panel; the registered one is not duplicated there.
        self.assertIn("Detected CLIs", content)
        self.assertEqual(1, page.locator("text=agent.hermes").count())
        self.assertEqual(1, page.locator("text=agent.codex").count())

    def test_a_reload_restores_the_page_from_the_server(self) -> None:
        """Nothing is kept client-side, so a reload must lose nothing."""

        run_id = self.start_run("browser-reload")
        page = self.open("en-US", path=f"/ui/#/runs/{run_id}")
        page.wait_for_selector("text=Open responsibilities")
        before = page.inner_text("#content")

        page.reload()
        page.wait_for_selector("text=Open responsibilities")
        after = page.inner_text("#content")

        self.assertIn(run_id, before)
        self.assertIn(run_id, after)

    def test_no_console_errors_on_any_view(self) -> None:
        for path in (
            "/ui/", "/ui/#/goals", "/ui/#/workflows", "/ui/#/runs",
            "/ui/#/inbox", "/ui/#/artifacts", "/ui/#/agents",
            "/ui/#/ops", "/ui/#/settings",
        ):
            with self.subTest(path=path):
                context = self.browser.new_context(locale="en-US")
                page = context.new_page()
                self.addCleanup(context.close)
                errors: list[str] = []
                page.on(
                    "console",
                    lambda message: (
                        errors.append(message.text)
                        if message.type == "error" else None
                    ),
                )
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.goto(f"{self.base}{path}")
                page.wait_for_selector("#content")
                page.wait_for_timeout(800)
                self.assertEqual([], errors)


class ReleaseHardeningTests(BrowserE2ETestCase):
    def test_all_primary_views_fit_the_mobile_viewport(self) -> None:
        context = self.browser.new_context(
            locale="en-US", viewport={"width": 360, "height": 800}
        )
        self.addCleanup(context.close)
        page = context.new_page()
        for view in (
            "home", "goals", "workflows", "runs", "inbox", "artifacts",
            "agents", "ops", "settings",
        ):
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

    def test_run_detail_with_long_identity_fits_mobile(self) -> None:
        run_id = self.start_run("browser-mobile-run-detail")
        context = self.browser.new_context(
            locale="en-US", viewport={"width": 360, "height": 800}
        )
        self.addCleanup(context.close)
        page = context.new_page()
        page.goto(f"{self.base}/ui/#/runs/{run_id}")
        page.wait_for_selector(".run-hero")
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth"
        )
        self.assertLessEqual(overflow, 1, f"Run detail overflows by {overflow}px")

    def test_keyboard_closes_dialog_and_restores_focus(self) -> None:
        page = self.open("en-US")
        trigger = page.locator("#newRun")
        trigger.focus()
        page.keyboard.press("Enter")
        page.wait_for_selector("dialog[open]")
        page.keyboard.press("Escape")
        page.wait_for_selector("dialog", state="detached")
        self.assertEqual("newRun", page.evaluate("document.activeElement.id"))

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

        page.route("**/api/v1/runs?*", network)
        page.click('[data-view="runs"]')
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
        page = self.open("en-US", path="/ui/#/runs")
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
        page.wait_for_selector("#content .home-hero")


if __name__ == "__main__":
    unittest.main()
