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

from orbit.web.app import HandlerRegistration, create_app
from orbit.workflow.handlers import TransformHandler
from orbit.web.api_v1 import Authorizer, WRITE_SCOPE
from orbit.web.local_identity import LOCAL_ACTOR, LOCAL_SCOPES, loopback_authenticator
from orbit.workflow.artifacts.local_cas import LocalCASBackend
from orbit.workflow.api.routes import RateLimiter
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
    def extra_handlers(cls) -> list:
        """Subclass hook for Handlers to register beside the transform.

        Separate from `extra_app_kwargs` because `handlers` is passed by the
        base composition: a subclass returning it there would be a duplicate
        keyword argument rather than an addition.
        """
        return []

    @classmethod
    def setUpClass(cls) -> None:
        import uvicorn

        cls.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.db = Path(cls.temp.name) / "runtime.db"
        cls.artifact_backend = LocalCASBackend(Path(cls.temp.name) / "artifacts")
        # Mutable only inside this test process: it lets a browser load an
        # advertised command and then lose authority before submission, which
        # proves the server re-checks scope at the command boundary.
        cls.scopes = set(LOCAL_SCOPES)
        app = create_app(
            cls.db,
            handlers=[transform_registration(), *cls.extra_handlers()],
            schemas=SCHEMAS,
            poll_seconds=0.02,
            authenticator=loopback_authenticator,
            authorizer=Authorizer(
                lambda actor: tuple(cls.scopes) if actor == LOCAL_ACTOR else ()
            ),
            artifact_backend=cls.artifact_backend,
            rate_limiter=RateLimiter(requests=1_000),
            serve_ui=True,
            langgraph_state_directory=Path(cls.temp.name) / "langgraph",
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
        return self.engine().start(
            "workflow:linear", {"value": 1},
            idempotency_key=key, actor=LOCAL_ACTOR,
        ).run_id

    def start_goal(
        self, key: str, goal: str, workflow_id: str = "workflow:linear",
    ) -> str:
        return self.engine().start(
            workflow_id, {"value": 1},
            idempotency_key=key, actor=LOCAL_ACTOR, goal=goal,
        ).run_id

    def cancel_goal(self, run_id: str) -> None:
        """Stop a run this test started, so the next one starts from quiet.

        The class shares one server. A run left in flight is an active goal
        for every test after it, and the ones that assert an empty workspace
        then fail for a reason that has nothing to do with them.
        """

        run = self.engine().get(run_id)
        self.engine().cancel(
            run_id, expected_revision=run.revision, actor=LOCAL_ACTOR,
            idempotency_key=f"cleanup-{run_id}",
        )

    def engine(self):
        return self.app.state.langgraph_service

    def wait_for_status(self, page, run_id: str, status: str, timeout: float = 20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            payload = page.evaluate(
                "id => fetch(`/api/v1/langgraph-runs/${encodeURIComponent(id)}`)"
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
        # Nothing this test asserts is about the public internet. The page
        # asks a font CDN for its typefaces, and that request failing — which
        # it does, roughly one run in six — was reported here as the Runtime
        # raising an error on every view at once. Served empty instead, so the
        # net catches this Runtime's faults and only those.
        context.route(
            "**/*",
            lambda route: route.continue_()
            if route.request.url.startswith(self.base)
            else route.fulfill(status=200, body="", content_type="text/plain"),
        )
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on(
            "console",
            lambda message: errors.append(f"console.error: {message.text}")
            if message.type == "error" else None,
        )
        # The browser's own console says only "Failed to load resource", which
        # names neither the request nor the status. Recording the response
        # beside it is the difference between a rerun and a diagnosis.
        page.on(
            "response",
            lambda response: errors.append(
                f"{response.status} {response.url}"
            ) if response.status >= 400 else None,
        )

        views = (
            ("#/workflows", "#content"),
            ("#/goals", "#content"),
            (f"#/goals/{run_id}", ".page-modal-panel .goal-detail"),
            (f"#/runs/{run_id}", ".simplified-run-hero"),
            ("#/home", ".simplified-workspace-composer"),
        )
        for fragment, ready in views:
            with self.subTest(view=fragment):
                page.goto(f"{self.base}/ui/{fragment}")
                page.wait_for_selector(ready)
                page.wait_for_timeout(300)
                # Drained per view: one view's fault used to fail all four and
                # leave every report naming the wrong page.
                seen, errors[:] = list(errors), []
                self.assertEqual([], seen, f"{fragment} raised {seen}")

        # The polling chain belongs to the shell rather than to any one view,
        # so one hold past a tick covers it — and costs six seconds instead of
        # six times four.
        page.wait_for_timeout(6_000)
        self.assertEqual([], errors, f"the refresh chain raised {errors}")

    def test_the_editor_page_is_absent(self) -> None:
        page = self.open("en-US")
        page.wait_for_selector(".simplified-workspace-composer")
        self.assertEqual(0, page.locator("#editorNav").count())
        response = page.goto(f"{self.base}/editor/")
        self.assertEqual(404, response.status)

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
        """The composer keeps what was typed while the run it started is live.

        It is withheld once that run is over — you are looking at a finished
        run, not composing — so the workflow started here has to be one that
        waits. With `workflow:linear`, which finishes in milliseconds, this
        raced: under the load of a full suite the browser rendered slowly
        enough for the run to reach `succeeded` first, the composer was
        correctly withheld, and reading the goal back failed. A human step
        makes "still running" a fact rather than a hope.
        """

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
            entry["langgraph_compatibility"] = {"compatible": True}
            entry["allowed_commands"] = [{
                "command": "langgraph_run.start", "label": "Start goal",
                "method": "POST", "href": "/api/v1/langgraph-runs",
                "expected_version": 0,
            }]
            route.fulfill(response=response, json=payload)

        def start_goal(route):
            if route.request.method != "POST":
                return route.continue_()
            body = route.request.post_data_json
            goal = body.get("goal") or json.dumps(body.get("input") or {})
            run_id = self.start_goal(
                "simplified-workspace-start", goal, "workflow:human",
            )
            started["run_id"] = run_id
            # A human step waits for ever by design, which is what makes this
            # test deterministic and what would otherwise leave every test
            # after it looking at an active goal.
            self.addCleanup(self.cancel_goal, run_id)
            route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({
                    "schema_version": "1.0", "projection_version": None,
                    "data": {"run": {"run_id": run_id}}, "next_cursor": None,
                }),
            )

        page.route("**/api/v1/workflows", advertise_goal_ready_workflow)
        page.route("**/api/v1/langgraph-runs", start_goal)
        page.reload()
        page.wait_for_selector(".simplified-workspace-composer")
        page.locator("#simplifiedWorkflow").evaluate(
            "node => { node.value = 'workflow:linear';"
            " node.dispatchEvent(new Event('change', {bubbles: true})); }"
        )
        page.fill("#simplifiedGoal", "Prepare a concise report")
        page.click("#newGoalStart")
        page.wait_for_function(
            "() => location.hash.startsWith('#/runs/langgraph_run%3A')"
        )
        page.wait_for_selector(".simplified-run-hero")

        self.assertTrue(started["run_id"])
        self.assertIn("Prepare a concise report", page.input_value("#simplifiedGoal"))
        self.assertEqual("workflow:linear", page.input_value("#simplifiedWorkflow"))
        self.assertTrue(page.locator(".simplified-run-hero").is_visible())

    def test_run_detail_has_no_runtime_tabs(self) -> None:
        run_id = self.start_goal("simplified-run", "Prepare a concise report")
        page = self.open("en-US", f"/ui/#/runs/{run_id}")
        page.wait_for_selector(".simplified-run-hero")

        self.assertEqual(0, page.locator(".run-tabs").count())
        self.assertEqual(0, page.locator(".why-panel").count())
        self.assertEqual(0, page.locator(".simplified-workspace-composer").count())
        self.assertEqual(0, page.get_by_role("button", name="Run again").count())
        self.assertTrue(page.locator(".simplified-run-hero").is_visible())
        # Which run this is and how it went. The goal is read on the goal
        # page; a paragraph of it here pushed the rest of the card down.
        hero = page.inner_text(".simplified-run-hero")
        self.assertIn("workflow:linear", hero)
        self.assertIn(run_id, hero)
        self.assertNotIn("Prepare a concise report", hero)

    def test_a_step_with_no_instruction_shows_no_instruction(self) -> None:
        """These steps are transforms: nobody wrote them a prompt."""

        run_id = self.start_run("steps-no-prompt")
        page = self.open("en-US", f"/ui/#/runs/{run_id}")
        page.wait_for_selector(".simplified-step-output summary")
        page.locator(".simplified-step-output summary").first.click()
        page.wait_for_timeout(400)

        self.assertEqual(0, page.locator(".simplified-step-prompt").count())

    def test_the_run_page_shows_every_step_and_where_it_got_to(self) -> None:
        """The rows are the definition's, so what is left is on the page too."""

        run_id = self.start_run("steps-view")
        page = self.open("en-US", f"/ui/#/runs/{run_id}")
        page.wait_for_selector(".step-row")
        rows = page.locator(".step-row")
        self.assertGreater(rows.count(), 1)
        # This fixture finishes in milliseconds, so by now every step has run.
        text = page.inner_text(".simplified-steps")
        self.assertIn("Done", text)
        self.assertNotIn("Not started", text)

    def test_the_graph_is_a_second_view_of_the_same_run(self) -> None:
        """A list says how far along; a graph says which branches there were.

        Neither summarises the other, so the canvas sits beside the steps in
        a card of its own, open from the first paint.
        """

        run_id = self.start_run("canvas-view")
        # The frame fetches itself on first paint, so the ear has to be on
        # before the page is.
        context = self.browser.new_context(locale="en-US")
        self.addCleanup(context.close)
        page = context.new_page()
        drawn = []
        page.on("request", lambda request: drawn.append(request.url))
        page.goto(f"{self.base}/ui/#/runs/{run_id}")
        page.wait_for_selector(".run-canvas")
        page.wait_for_selector("iframe.workflow-graph-frame")
        frame = page.frame_locator("iframe.workflow-graph-frame")
        frame.locator(".node").first.wait_for(timeout=15000)
        # The run is drawn on the definition: every node carries the state it
        # reached, spelled as well as coloured.
        classes = frame.locator(".node").first.get_attribute("class")
        self.assertIn("node-run", classes)
        self.assertIn("done", frame.locator(".node").first.inner_text().lower())
        self.assertTrue(
            any(url.endswith("/graph") for url in drawn),
            "the canvas asked the workflow catalog instead of the run",
        )

    def test_a_fork_says_which_way_the_run_went(self) -> None:
        """The report the server derives, rendered.

        Served here rather than published, because what is under test is the
        view: that only forks appear, that each answer arrives as a word, and
        that the branch not taken is on the page rather than merely absent.
        The derivation itself is tested where it is written.
        """

        run_id = self.start_run("branches-view")
        page = self.open("en-US", f"/ui/#/runs/{run_id}")
        page.route("**/edges", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({
                "schema_version": "1.0", "projection_version": 1,
                "next_cursor": None,
                "data": {"edges": [
                    {"edge_id": "e1", "source_node": "transform",
                     "target_node": "urgent", "route": "success", "priority": 0,
                     "back_edge": False, "default": False,
                     "status": "not_taken", "visits": 1, "detail": None},
                    {"edge_id": "e2", "source_node": "transform",
                     "target_node": "done", "route": "success", "priority": 1,
                     "back_edge": False, "default": True,
                     "status": "taken", "visits": 1, "detail": None},
                    {"edge_id": "e3", "source_node": "done",
                     "target_node": "end", "route": "success", "priority": 0,
                     "back_edge": False, "default": True,
                     "status": "taken", "visits": 1, "detail": None},
                ]},
            }),
        ))
        page.reload()
        page.wait_for_selector(".run-branches")

        # `done` has one outgoing edge and so decided nothing; listing it
        # would bury the node that did.
        self.assertEqual(1, page.locator(".run-branch-group").count())
        page.locator(".run-branches summary").click()
        text = page.inner_text(".run-branches")
        self.assertIn("Followed", text)
        self.assertIn("Condition was false", text)
        self.assertIn("urgent", text)

    def test_a_run_without_a_fork_has_nothing_to_explain(self) -> None:
        run_id = self.start_run("no-branches-view")
        page = self.open("en-US", f"/ui/#/runs/{run_id}")
        page.wait_for_selector(".step-row")
        self.assertEqual(0, page.locator(".run-branches").count())

    def test_the_console_is_closed_until_asked_for(self) -> None:
        """Each executed step owns a lazy, node-filtered output panel."""

        run_id = self.start_run("console-view")
        page = self.open("en-US", f"/ui/#/runs/{run_id}")
        page.wait_for_selector(".step-row .simplified-step-output")

        requested = []
        page.on("request", lambda request: (
            requested.append(request.url) if "/output" in request.url else None
        ))
        panel = page.locator(".simplified-step-output").first
        self.assertEqual(0, page.locator(
            ".simplified-run-hero .simplified-step-output"
        ).count())
        page.wait_for_timeout(200)
        panel.wait_for()
        # Closed: the largest thing on the page has not been fetched.
        self.assertEqual([], requested)

        # Waiting on the response rather than on the text: this run prints
        # nothing, so the panel's settled message is the same one it started
        # with and no wait on it could tell the two apart.
        with page.expect_response("**/output*") as answered:
            panel.locator("summary").click()
        self.assertEqual(200, answered.value.status)
        self.assertIn("node_id=", answered.value.url)
        # A Handler that printed nothing says so rather than staying blank.
        self.assertIn(
            "Nothing printed", page.inner_text(".simplified-step-output-body"),
        )

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
        self.wait_for_status(self.open("en-US"), finished, "completed")

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
        # The detail slides in as a right-hand page: the goal card renders
        # inside the drawer, the list stays behind it, and the address never
        # changes.
        page.wait_for_selector(".page-drawer-panel .goal-text-card")
        self.assertIn(
            "Summarise the quarter",
            page.locator(".page-drawer-panel .goal-text-card").inner_text(),
        )
        page.wait_for_timeout(300)
        drawer_box = page.locator(".page-drawer-panel").bounding_box()
        viewport = page.viewport_size
        self.assertIsNotNone(drawer_box)
        self.assertAlmostEqual(
            viewport["width"], drawer_box["x"] + drawer_box["width"], delta=1,
        )
        self.assertTrue(page.locator(".history-goal-list").is_visible())
        self.assertEqual("#/goals", page.evaluate("location.hash"))
        self.assertEqual(0, page.locator(".simplified-workspace-composer").count())
        self.assertEqual(0, page.get_by_role("button", name="Run again").count())

    def test_history_goal_detail_closes_where_it_opened(self) -> None:
        """A click opens the modal in place; closing it changes nothing."""

        finished = self.start_goal("simplified-history-dismiss", "Read a goal back")
        self.wait_for_status(self.open("en-US"), finished, "completed")

        page = self.open("en-US", "/ui/#/goals")
        page.wait_for_selector(".history-goal-row")
        page.locator(".history-goal-row").first.click()
        page.wait_for_selector(".page-drawer-panel .goal-text-card")
        self.assertEqual("#/goals", page.evaluate("location.hash"))

        page.get_by_role("button", name="Close").click()
        page.wait_for_function("() => !document.querySelector('.page-modal-root')")
        page.wait_for_selector(".history-goal-list")
        self.assertEqual("#/goals", page.evaluate("location.hash"))

        # A pasted address still opens the detail, and from there the route
        # is what Escape walks back.
        page.goto(f"{self.base}/ui/#/goals/{quote(finished, safe='')}")
        page.wait_for_selector(".page-drawer-panel .goal-text-card")
        page.keyboard.press("Escape")
        page.wait_for_function("() => location.hash === '#/goals'")
        page.wait_for_function("() => !document.querySelector('.page-modal-root')")

    def test_history_loads_the_next_cursor_page(self) -> None:
        context = self.browser.new_context(locale="en-US")
        self.addCleanup(context.close)
        page = context.new_page()

        def history_page(route):
            second_page = "cursor=next" in route.request.url
            run = {
                "run_id": f"langgraph_run:{'second' if second_page else 'first'}",
                "workflow_id": "workflow:linear", "workflow_version": 1,
                "display_name": "Second page Goal" if second_page else "First page Goal",
                "goal": "Second page Goal" if second_page else "First page Goal",
                "status": "completed", "artifact_count": 1,
                "created_at": "2026-07-24T08:00:00Z",
                "updated_at": "2026-07-24T08:02:00Z",
            }
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "schema_version": "1.0", "projection_version": None,
                "data": {"runs": [run]},
                "next_cursor": None if second_page else "next",
            }))

        page.route("**/api/v1/langgraph-runs?*", history_page)
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
        self.wait_for_status(self.open("en-US"), finished, "completed")

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

    def test_a_branch_the_runs_never_entered_is_named_where_it_is_edited(
        self,
    ) -> None:
        """The finding the run page cannot make.

        On any single run one branch of a fork is taken and the rest are not,
        so only the tally across runs can call one dead — which makes it a
        fact about the definition, shown where the definition is changed.
        """

        page = self.open("en-US", "/ui/#/workflows")
        page.route("**/branches**", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({
                "schema_version": "1.0", "projection_version": None,
                "next_cursor": None,
                "data": {
                    "workflow_id": "workflow:research", "workflow_version": 1,
                    "runs": 12,
                    "edges": [
                        {"edge_id": "e1", "source_node": "classify",
                         "target_node": "urgent", "route": "success",
                         "default": False, "decided": 12, "taken": 0,
                         "not_taken": 12, "shadowed": 0, "other_route": 0,
                         "not_reached": 0, "undecidable": 0,
                         "verdict": "never_taken"},
                        {"edge_id": "e2", "source_node": "classify",
                         "target_node": "normal", "route": "success",
                         "default": True, "decided": 12, "taken": 12,
                         "not_taken": 0, "shadowed": 0, "other_route": 0,
                         "not_reached": 0, "undecidable": 0,
                         "verdict": "taken"},
                    ],
                },
            }),
        ))
        page.goto(f"{self.base}/ui/#/workflows/workflow:research/edit")
        page.wait_for_selector(".workflow-branch-findings")

        # Only the finding. The branch every run took is normal operation and
        # would bury it.
        self.assertEqual(1, page.locator(".workflow-branch-finding").count())
        text = page.inner_text(".workflow-branch-findings")
        self.assertIn("classify", text)
        self.assertIn("urgent", text)
        self.assertNotIn("normal", text)
        self.assertIn("12", text)

    def test_a_definition_with_nothing_to_report_shows_no_panel(self) -> None:
        page = self.open("en-US", "/ui/#/workflows")
        page.route("**/branches**", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({
                "schema_version": "1.0", "projection_version": None,
                "next_cursor": None,
                "data": {
                    "workflow_id": "workflow:research", "workflow_version": 1,
                    "runs": 0, "edges": [],
                },
            }),
        ))
        page.goto(f"{self.base}/ui/#/workflows/workflow:research/edit")
        page.wait_for_selector(".workflow-editor textarea")
        self.assertEqual(0, page.locator(".workflow-branch-findings").count())

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

        page.route("**/api/v1/langgraph-runs*", network)
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

        page.route("**/api/v1/langgraph-runs*", unavailable)
        page.click('[data-view="home"]')
        page.wait_for_selector("#content .data-state.error")
        self.assertIn("projection is rebuilding", page.inner_text("#content"))
        failing["value"] = False
        page.get_by_role("button", name="Try again").click()
        page.wait_for_selector("#content .simplified-workspace-composer")


class GoalHomeTests(BrowserE2ETestCase):
    """The Goal home starts published Workflows, not built-in templates.

    It offered a fixed list of built-in templates instead, and the Workflow
    catalog — the page its own Agent publishes into — was hidden from the
    navigation entirely, so a Workflow it had just generated had nowhere to be
    opened and no way to be started.

    This is also the only browser coverage of the LangGraph home screen; the
    rest of the suite runs the pre-LangGraph composer, which is why the
    template-only home went unnoticed.
    """

    @classmethod
    def extra_app_kwargs(cls) -> dict:
        from tests.test_workflow_authoring_jobs import dsl
        from orbit.workflow.authoring import ExternalAuthoringBroker

        broker = ExternalAuthoringBroker(presence_seconds=3600)
        broker.claim(actor=LOCAL_ACTOR, client="Codex")
        broker.claim(actor=LOCAL_ACTOR, client="Remote")

        return {
            "single_goal_mode": True,
            # Named as discovery names a CLI, with no `app:` prefix. The
            # prefix used to be required: single-agent mode took only `app:`
            # writers, so an install with working CLIs advertised them and
            # then refused to generate. A fixture that spelled it the way the
            # code demanded was how that went unnoticed.
            "workflow_generators": {
                "codex": lambda _prompt: json.dumps(
                    dsl(name="Generated by the single Agent")
                ),
                "Codex": lambda _prompt: json.dumps(
                    dsl(name="Generated by the connected Agent")
                ),
                "Remote": lambda _prompt: json.dumps(
                    dsl(name="Generated by another connected Agent")
                ),
            },
            "authoring_broker": broker,
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

    def start(self, page, workflow_id: str, goal: str = "") -> dict:
        return page.evaluate(
            """([id, goal]) => fetch("/api/v1/langgraph-runs", {
                 method: "POST",
                 headers: {
                   "content-type": "application/json",
                   "idempotency-key": "start-" + id + goal.length,
                 },
                 body: JSON.stringify({
                   workflow_id: id, input: {value: 1}, goal,
                 }),
               }).then((r) => r.json()).then((b) => b.data.run)""",
            [workflow_id, goal],
        )

    def test_a_long_goal_folds_and_offers_the_way_out_of_the_fold(self) -> None:
        """A goal is frequently a paragraph, and the goal page is where it is read.

        It keeps its line breaks, folds at 120px and unfolds in place. The
        measurement that decides whether the fold needs a toggle once ran in
        a single animation frame — before the block was in the document, so
        it compared 0 against 0 and hid the toggle on a goal that needed it.
        """

        page = self.open("en-US")
        page.wait_for_selector(".simplified-workspace-composer")
        workflow_id = self.publish(page, self.runnable(human=False))
        goal = "\n".join(f"line {index} of a long pasted goal" for index in range(30))
        run = self.start(page, workflow_id, goal=goal)

        page.goto(f"{self.base}/ui/#/goals/{quote(run['run_id'], safe='')}")
        card = page.locator(".goal-text-card")
        card.wait_for()

        toggle = card.locator(".goal-expand-toggle")
        toggle.wait_for()
        self.assertTrue(toggle.is_visible())
        clamped = card.locator(".goal-text-clamp")
        self.assertLess(
            clamped.bounding_box()["height"],
            card.locator(".goal-text").bounding_box()["height"],
        )

        toggle.click()
        self.assertEqual(
            clamped.bounding_box()["height"],
            card.locator(".goal-text").bounding_box()["height"],
        )

    def test_the_run_card_carries_identity_status_and_the_way_to_stop(self) -> None:
        """What a watcher came for, above everything the run produced.

        The card led with the goal — frequently a paragraph — and put Cancel
        underneath the interrupts and the result, so the control for stopping
        a run in flight was below the fold of the run in flight.
        """

        page = self.open("en-US")
        page.wait_for_selector(".simplified-workspace-composer")
        workflow_id = self.publish(page, self.runnable(human=True))
        run = self.start(page, workflow_id, goal="a goal long enough to have pushed this down")
        self.addCleanup(self.cancel, page, run)

        page.goto(f"{self.base}/ui/#/runs/{quote(run['run_id'], safe='')}")
        hero = page.locator(".simplified-run-hero")
        hero.wait_for()

        ids = hero.locator(".simplified-run-ids").inner_text()
        self.assertIn(workflow_id, ids)
        self.assertIn(run["run_id"], ids)
        # The goal is read on the goal page; this card is not where it goes.
        self.assertNotIn("a goal long enough", hero.inner_text())

        state = hero.locator(".simplified-run-state")
        self.assertEqual(1, state.locator(".pill").count())
        cancel = state.locator("button.danger")
        self.assertTrue(cancel.is_visible())
        # Above the run's own output, not after it.
        self.assertLess(
            cancel.bounding_box()["y"],
            hero.locator(".code-block").first.bounding_box()["y"],
        )

    def test_the_run_controls_speak_the_readers_language(self) -> None:
        """A Runtime names its commands for every client; this one is a person.

        `Resume LangGraph workflow` says which engine and which surface,
        which is right in an API contract and wrong on a button somebody
        presses. And the cancel prompt asked them to confirm the word
        `explicit` — the contract's term for *that* a confirmation is
        required, rendered as the sentence asking for one.
        """

        page = self.open("zh-CN")
        page.wait_for_selector(".simplified-workspace-composer")
        workflow_id = self.publish(page, self.runnable(human=True))
        # A goal of its own: the idempotency key is built from the workflow
        # and the goal's length, so sharing both with another test replays
        # that test's run rather than starting one.
        run = self.start(page, workflow_id, goal="controls")
        self.addCleanup(self.cancel, page, run)

        page.goto(f"{self.base}/ui/#/runs/{quote(run['run_id'], safe='')}")
        state = page.locator(".simplified-run-state")
        state.locator("button.danger").wait_for()

        self.assertEqual("取消执行", state.locator("button.danger").inner_text())
        self.assertEqual("继续", state.locator("button.primary").first.inner_text())

        asked = []
        page.on("dialog", lambda dialog: (asked.append(dialog.message), dialog.dismiss()))
        state.locator("button.danger").click()
        page.wait_for_timeout(400)
        self.assertEqual(1, len(asked))
        self.assertNotIn("explicit", asked[0])
        self.assertIn("停止", asked[0])

    def test_a_run_still_in_flight_keeps_the_composer_locked(self) -> None:
        """The other half: `interrupted` is not over, and must still lock."""

        page = self.open("en-US")
        page.wait_for_selector(".simplified-workspace-composer")
        workflow_id = self.publish(page, self.runnable(human=True))
        run = self.start(page, workflow_id)
        self.assertEqual("interrupted", run["status"])

        # Cancelled when this test is done: the class shares one server, and
        # a run left in flight locks the composer for every test after it.
        self.addCleanup(self.cancel, page, run)

        page.goto(f"{self.base}/ui/#/runs/{quote(run['run_id'], safe='')}")
        page.wait_for_selector(".simplified-workspace-composer")
        self.assertTrue(page.locator("#newGoalStart").is_disabled())

    def cancel(self, page, run) -> None:
        page.evaluate(
            """([id, revision]) => fetch(
                 `/api/v1/langgraph-runs/${encodeURIComponent(id)}/cancel`,
                 {
                   method: "POST",
                   headers: {
                     "content-type": "application/json",
                     "idempotency-key": "cancel-" + id,
                   },
                   body: JSON.stringify({expected_version: revision}),
                 },
               ).then((r) => r.json())""",
            [run["run_id"], run["revision"]],
        )

    def test_start_is_withheld_where_the_goal_has_nowhere_to_go(self) -> None:
        """Compiling and being startable from a goal are separate questions.

        Only the first used to be asked. `workflow:linear` compiles under
        LangGraph, so Start was enabled; its `goal_readiness` is not `ready`,
        so `bindGoalInput` returned the input unchanged — there was no binding
        to write into — and the server refused the run for an input the person
        had in fact typed.
        """

        page = self.open("en-US")
        page.wait_for_selector(".simplified-workspace-composer")
        readiness = page.evaluate(
            """() => fetch("/api/v1/workflows").then((r) => r.json())
                 .then((b) => Object.fromEntries(b.data.workflows.map((w) => [
                   w.workflow_id,
                   [w.goal_readiness, w.langgraph_compatibility.compatible],
                 ])))"""
        )
        # The premise: compilable, and not bindable from a goal.
        self.assertEqual(["needs_migration", True], readiness["workflow:linear"])

        page.fill("#simplifiedGoal", "do the thing")
        self.assertTrue(page.locator("#newGoalStart").is_disabled())

        # The same catalog with readiness flipped, and nothing else changed:
        # what enables Start is the answer to "can a goal be bound", which is
        # the question that was not being asked.
        def as_ready(route):
            response = route.fetch()
            body = response.json()
            for item in body["data"]["workflows"]:
                item["goal_readiness"] = "ready"
                item["readiness_reason"] = None
                item["goal_binding"] = {
                    "source": "run.goal", "node_id": "transform",
                    "input_id": "prompt", "property": "goal",
                    "value_shape": "object",
                }
            route.fulfill(response=response, json=body)

        page.route("**/api/v1/workflows", as_ready)
        page.reload()
        page.wait_for_selector(".simplified-workspace-composer")
        page.fill("#simplifiedGoal", "do the thing")

        self.assertFalse(page.locator("#newGoalStart").is_disabled())

    def test_a_card_offers_new_goal_only_where_a_goal_can_start_it(self) -> None:
        """The card and the composer must agree, or the dialog contradicts it.

        The button rendered on the start command alone, so a workflow that
        compiles but has nowhere to put a goal advertised "New goal" — and
        `newRunDialog` then declined to preselect it, because it checks
        readiness. The person clicked a named workflow and got an empty
        picker, with nothing saying why.
        """

        page = self.open("en-US")
        page.locator('[data-view="workflows"]').click()
        page.wait_for_selector(".workflow-card")

        # Nothing in this catalog is goal-ready, and all of it compiles.
        self.assertTrue(page.locator(".workflow-card").count() > 0)
        # `exact`, because the default is a case-insensitive substring and the
        # card body itself says "…before starting a new goal".
        self.assertEqual(
            0,
            page.get_by_role("button", name="New goal", exact=True).count(),
        )
        # The affordance a non-ready workflow does get, so the assertion above
        # is not passing because the cards rendered no buttons at all.
        self.assertTrue(page.locator(".upgrade-workflow, .edit-workflow").count() > 0)

    def test_the_author_can_choose_which_agent_writes(self) -> None:
        """Every writer the Runtime has, not the subset one product allowed.

        The choice used to be narrowed to Apps connected over MCP, because
        single-Agent authoring was deliberately stricter than the rest of the
        Runtime. With one product there is one answer: whoever can write.
        """

        page = self.open("en-US")
        page.locator('[data-view="workflows"]').click()
        page.wait_for_selector(".simplified-workflow-generator")

        writer = page.locator("#workflowGenerateAgent")
        options = writer.locator("option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        )
        self.assertIn("Codex", options)
        self.assertIn("Remote", options)
        page.get_by_role("combobox", name="Written by").click()
        page.get_by_role("option", name="Remote", exact=True).click()
        self.assertEqual("Remote", writer.input_value())
        self.assertFalse(page.locator("#generateWorkflow").is_disabled())

    def test_every_page_the_shell_offers_is_reachable(self) -> None:
        """The Agents page used to be hidden by the mode. Nothing hides it now."""

        page = self.open("en-US")
        page.wait_for_selector(".simplified-workspace-composer")

        self.assertTrue(page.locator('[data-view="agents"]').is_visible())


if __name__ == "__main__":
    unittest.main()


class AgentSubstitutionNoticeTests(BrowserE2ETestCase):
    """The page names one Agent per step and another one runs it. Say so.

    The definition list names the Handler each step was published against,
    node by node. A step whose Agent this machine has never had is startable
    anyway, on a different one — so the page has to state the substitution
    rather than leave the reader to conclude, correctly and wrongly, that it
    will run on what it says.
    """

    @classmethod
    def extra_handlers(cls) -> list:
        from tests.test_agent_binding import manifest_for

        cls.agent = manifest_for("claude")
        return [HandlerRegistration(
            cls.agent, TransformHandler(),
            f"{cls.agent.name}@{cls.agent.version}",
        )]

    @classmethod
    def extra_app_kwargs(cls) -> dict:
        return {}

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from orbit.workflow.domain.definitions import CompiledWorkflow
        from orbit.workflow.domain.serialization import definition_hash
        from orbit.workflow.persistence.workflow_versions import (
            SQLiteWorkflowVersionStore,
        )
        from tests.test_agent_binding import agent_step, single_step_workflow

        # Published against an Agent this Runtime does not have, which is the
        # whole case: in multi-Agent mode it is unstartable drift. The config
        # is the stand-in Handler's, not an Agent's — the transform registered
        # under the Agent's manifest is what plays the CLI here, and it has to
        # be told to answer on the port the graph declares.
        ir = single_step_workflow(agent_step(config={
            "prompt": "do the thing",
            "operation": "build_object",
            "value": {"result": {"ok": True}},
        }))
        store = SQLiteWorkflowVersionStore(cls.db)
        store.publish(
            CompiledWorkflow(ir, definition_hash(ir), "test", "sha256:" + "c" * 64),
            expected_latest_version=0, source_format="json", source_text="{}",
            actor="test:author", dsl_version="1.3",
        )
        # Its Agent is not here, so it has to be carried — and the result it
        # declares is not one the Agent it would be carried to produces. Its
        # binding reads rebound and its entry port is the one a goal binds to,
        # so it reads ready: every signal the card draws says this workflow is
        # fine, and it cannot start.
        from tests.test_agent_binding import port

        odd = single_step_workflow(
            agent_step(outputs=(port("result", "example://integer/1.0"),)),
            workflow_id="workflow:ports",
        )
        store.publish(
            CompiledWorkflow(odd, definition_hash(odd), "test", "sha256:" + "c" * 64),
            expected_latest_version=0, source_format="json", source_text="{}",
            actor="test:author", dsl_version="1.3",
        )
    def test_the_definition_shows_both_the_published_and_the_bound_agent(self) -> None:
        """The picture must agree with the definition *and* with the run.

        The definition list names the Handler each step was published
        against, and in this mode not one of them is what will run. Showing
        only the published name is wrong about the run; replacing it is wrong
        about the definition. Both, with the substitution marked.
        """

        page = self.open("en-US")
        page.goto(f"{self.base}/ui/#/workflows/workflow:single")
        # The drawing is the default pane; the list is the other tab.
        failures: list[str] = []
        page.on("pageerror", lambda error: failures.append(str(error)))
        page.wait_for_selector(".workflow-graph-frame")

        canvas = page.frame_locator(".workflow-graph-frame")
        canvas.locator(".node .handler").first.wait_for()
        self.assertEqual(
            "agent.absent", canvas.locator(".superseded").first.inner_text(),
        )
        self.assertIn(
            "agent.claude", canvas.locator(".rebound").first.inner_text(),
        )

        # The list is a second picture of the same definition and has to say
        # the same thing.
        page.click('[data-workflow-tab="definition"]')
        page.wait_for_selector(".defn-handler")
        self.assertEqual(
            "absent", page.locator('.defn-handler[data-rebound="true"]').inner_text(),
        )
        self.assertEqual(
            "claude", page.locator(".defn-handler-bound").inner_text(),
        )
        self.assertEqual([], failures)

    def test_the_action_editor_offers_the_agents_that_are_installed(self) -> None:
        """A pick here is honoured: the fallback only moves a stranded step.

        While one Agent ran everything, this control republished a choice the
        Runtime then discarded, and the dialog reported the binding instead.
        Now the Agent a step names is the Agent that runs it wherever it
        exists, so choosing one decides something again.
        """

        page = self.open("en-US")
        page.goto(f"{self.base}/ui/#/workflows/workflow:single/edit")
        page.wait_for_selector('[data-workflow-tab="definition"]')
        page.click('[data-workflow-tab="definition"]')
        page.click(".defn-edit")
        page.wait_for_selector(".action-editor-dialog")

        dialog = page.locator(".action-editor-dialog")
        self.assertEqual(1, dialog.locator("select").count())
        self.assertIn(
            "claude",
            dialog.locator("select option").first.inner_text(),
        )
        self.assertEqual(1, dialog.locator("textarea").count())

    def test_the_canvas_follows_the_page_and_not_the_operating_system(self) -> None:
        """The frame is a document of its own and had no way to know.

        It followed `prefers-color-scheme` while the page followed the theme
        the operator picked here, so anyone whose system disagreed with their
        choice got a white canvas in the middle of a dark page. The page posts
        its palette in; this browser is told the system is light while the UI
        defaults to dark, which is exactly that disagreement.
        """

        context = self.browser.new_context(locale="en-US", color_scheme="light")
        page = context.new_page()
        self.addCleanup(context.close)
        page.goto(f"{self.base}/ui/#/workflows/workflow:single")
        page.wait_for_selector(".workflow-graph-frame")

        self.assertEqual("dark", page.evaluate(
            "document.documentElement.dataset.theme"
        ))
        canvas = page.frame_locator(".workflow-graph-frame")
        canvas.locator(".node").first.wait_for()
        self.assertEqual("dark", canvas.locator(":root").get_attribute("data-theme"))

    def test_the_canvas_follows_a_theme_change_after_it_is_drawn(self) -> None:
        """The choice outlives one drawing, so one message at load is not enough."""

        page = self.open("en-US")
        page.goto(f"{self.base}/ui/#/workflows/workflow:single")
        page.wait_for_selector(".workflow-graph-frame")
        canvas = page.frame_locator(".workflow-graph-frame")
        canvas.locator(".node").first.wait_for()

        # The theme control lives behind the overflow menu.
        page.click("#moreButton")
        page.click("#themeLight")

        page.wait_for_function(
            "document.querySelector('.workflow-graph-frame')"
            ".contentDocument.documentElement.dataset.theme === 'light'"
        )

    def test_a_card_with_nothing_wrong_on_it_still_says_why(self) -> None:
        """Ready to bind a goal, current Handler, and the engine refuses.

        Every signal the card draws says this workflow is fine, so nothing
        was drawn — and the Start button's absence was the only hint that
        anything was wrong.
        """

        page = self.open("en-US")
        page.goto(f"{self.base}/ui/#/workflows")
        card = page.locator('.workflow-card[data-workflow-id="workflow:ports"]')
        card.wait_for()

        # The premise: nothing else on the card is complaining.
        self.assertEqual(0, card.locator(".pill.failed").count())
        self.assertEqual(0, card.locator(".pill.waiting").count())
        self.assertEqual(0, card.locator("#rebindWorkflow").count())

        self.assertIn(
            "does not fit the Agent",
            card.locator(".workflow-card-blocked").inner_text(),
        )

    def test_the_run_log_shows_what_the_step_was_asked(self) -> None:
        """A log without the instruction that produced it is half a transcript.

        The drawer is opened to find out what happened at a step, and what it
        was asked is the other half of that — it lived only in the workflow's
        definition, a page away from the run being read.
        """

        page = self.open("en-US")
        run = page.evaluate(
            """() => fetch("/api/v1/langgraph-runs", {
                 method: "POST",
                 headers: {
                   "content-type": "application/json",
                   "idempotency-key": "start-prompt-drawer",
                 },
                 body: JSON.stringify({
                   workflow_id: "workflow:single", input: {prompt: {goal: "x"}},
                 }),
               }).then((r) => r.json())""",
        )
        self.assertIn("data", run, run)

        page.goto(f"{self.base}/ui/#/runs/"
                  f"{quote(run['data']['run']['run_id'], safe='')}")
        page.wait_for_selector(".simplified-step-output summary")
        page.locator(".simplified-step-output summary").first.click()
        prompt = page.locator(".simplified-step-prompt").first
        prompt.wait_for()

        self.assertIn("do the thing", prompt.inner_text())
        # Above what the step said back, not after it.
        self.assertLess(
            prompt.bounding_box()["y"],
            page.locator(".simplified-step-output-body > :nth-child(2)")
            .first.bounding_box()["y"],
        )

    def test_the_run_page_names_the_agent_that_ran_it(self) -> None:
        """Recorded on the run, so it still answers after the binding moves.

        The run's own steps carry no Handler name on this page, and the
        definition it points at names the Agent that did *not* run it — so
        without this line a finished goal has nothing that says who did it.
        """

        page = self.open("en-US")
        run = page.evaluate(
            """() => fetch("/api/v1/langgraph-runs", {
                 method: "POST",
                 headers: {
                   "content-type": "application/json",
                   "idempotency-key": "start-single",
                 },
                 body: JSON.stringify({
                   workflow_id: "workflow:single", input: {prompt: {goal: "x"}},
                 }),
               }).then((r) => r.json())""",
        )
        self.assertIn("data", run, run)
        run_id = run["data"]["run"]["run_id"]
        self.assertEqual("agent.claude@1.2.3", run["data"]["run"]["agent_binding"])

        page.goto(f"{self.base}/ui/#/runs/{quote(run_id, safe='')}")
        line = page.locator(".run-agent-binding")
        line.wait_for()

        self.assertIn("claude", line.inner_text())


class EngineRefusalNoticeTests(BrowserE2ETestCase):
    """A workflow the engine will not run has to say so on the page.

    This is the silent state single-Agent mode could reach: a definition
    whose Agent is not installed, on a Runtime with two Agents registered and
    no Agent App connected to say which one is current. The catalog answered
    `goal_readiness: ready`, the handler binding answered `current`, and the
    Start button was simply absent — the engine's reason sat unread in the
    payload that drew the card.
    """

    @classmethod
    def extra_handlers(cls) -> list:
        from tests.test_agent_binding import manifest_for

        return [
            HandlerRegistration(
                manifest, TransformHandler(),
                f"{manifest.name}@{manifest.version}",
            )
            # Two, so no single Agent is unambiguous and nothing is rebound.
            for manifest in (manifest_for("claude"), manifest_for("codex"))
        ]

    @classmethod
    def extra_app_kwargs(cls) -> dict:
        return {}

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from orbit.workflow.domain.definitions import CompiledWorkflow
        from orbit.workflow.domain.serialization import definition_hash
        from orbit.workflow.persistence.workflow_versions import (
            SQLiteWorkflowVersionStore,
        )
        from tests.test_agent_binding import agent_step, single_step_workflow

        ir = single_step_workflow(agent_step())
        SQLiteWorkflowVersionStore(cls.db).publish(
            CompiledWorkflow(ir, definition_hash(ir), "test", "sha256:" + "c" * 64),
            expected_latest_version=0, source_format="json", source_text="{}",
            actor="test:author", dsl_version="1.3",
        )

    def test_the_page_carries_the_runtimes_own_sentence(self) -> None:
        """The reason is translated; the detail is the Runtime's, verbatim.

        Including the way out, which only the Runtime is in a position to
        know: this definition becomes startable the moment an Agent App says
        which Agent it is.
        """

        page = self.open("en-US")
        page.goto(f"{self.base}/ui/#/workflows/workflow:single")
        notice = page.locator(".workflow-blocked")
        notice.wait_for()

        text = notice.inner_text()
        self.assertIn("The engine cannot run this definition.", text)
        self.assertIn("agent.absent", text)
        self.assertIn("no Agent App has introduced itself", text)


class TwoSubstitutesNoticeTests(BrowserE2ETestCase):
    """A graph can be carried to two Agents at once, and must say both.

    Each Agent step is decided on its own, so a graph with two stale pins
    lands on two installed builds. Both Agents are registered here and none is
    connected, which is exactly the state where a step is carried to its *own*
    Agent's build rather than to whichever Agent is current.
    """

    @classmethod
    def extra_handlers(cls) -> list:
        from tests.test_agent_binding import manifest_for

        return [
            HandlerRegistration(
                manifest, TransformHandler(),
                f"{manifest.name}@{manifest.version}",
            )
            for manifest in (manifest_for("claude"), manifest_for("codex"))
        ]

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from orbit.workflow.domain.definitions import (
            CompiledWorkflow, IRHandlerRef, IRResult, WorkflowIR,
        )
        from orbit.workflow.domain.serialization import definition_hash
        from orbit.workflow.persistence.workflow_versions import (
            SQLiteWorkflowVersionStore,
        )
        from tests.test_agent_binding import agent_step, port

        pair = WorkflowIR(
            "1.3", "workflow:two-substitutes", "Two substitutes", "", {},
            (port("prompt"),), (port("result"),),
            (
                agent_step("first", handler=IRHandlerRef(
                    "agent.claude", "0.0.1", "sha256:" + "a" * 64,
                )),
                agent_step("second", handler=IRHandlerRef(
                    "agent.codex", "0.0.1", "sha256:" + "b" * 64,
                )),
            ),
            (),
            ("first",), ("second",), (), (), {}, IRResult("second", "result"),
        )
        SQLiteWorkflowVersionStore(cls.db).publish(
            CompiledWorkflow(pair, definition_hash(pair), "test", "sha256:" + "c" * 64),
            expected_latest_version=0, source_format="json", source_text="{}",
            actor="test:author", dsl_version="1.3",
        )

    def test_the_notice_names_every_agent_a_step_lands_on(self) -> None:
        """A substitution is per step, so a graph can land on two Agents.

        The notice used to read the engine's one-line summary of them —
        `"agent.a@1, agent.b@2"` — and strip from the first `@` to the end,
        which reported the first Agent as the answer for all of it. On a
        nine-step graph with two stale pins that is a sentence saying four
        steps run somewhere three of them do not.
        """

        page = self.open("en-US")
        page.goto(f"{self.base}/ui/#/workflows/workflow:two-substitutes")
        notice = page.locator(".workflow-agent-binding p")
        notice.wait_for()

        text = notice.inner_text()
        self.assertIn("claude", text)
        self.assertIn("codex", text)
        self.assertIn("2 steps", text)
