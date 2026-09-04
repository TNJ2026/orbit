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
import re
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from urllib.parse import quote
import uuid

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
        # moment would have missed it entirely, so the run is held past a tick
        # at the end. The interval used to be settable and this test used to
        # set it to five seconds; it is a fixed fifteen now, so the hold has
        # to clear that instead.
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
            ("#/history", "#content"),
            (f"#/history/{run_id}", ".page-modal-panel .goal-detail"),
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
        # so one hold past a tick covers it — sixteen seconds once, rather than
        # sixteen times five.
        page.wait_for_timeout(16_000)
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

    def test_starting_a_goal_opens_it_over_an_emptied_composer(self) -> None:
        """The box is for drafts, and a sent goal is not one.

        It used to keep the text while the run it started was live, from where
        it was read beside the run drawn under this page. The run is a modal
        now and carries the goal at its own top, so what was left behind was a
        submitted goal sitting in a box that invites editing.

        Only on success: a refusal leaves the text where it was typed, which
        the test below holds.
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
        self.assertEqual("", page.input_value("#simplifiedGoal"))
        # The picker still says which workflow is running: it is locked, not
        # emptied, and it is the one fact about the run this page keeps.
        self.assertEqual("workflow:linear", page.input_value("#simplifiedWorkflow"))
        self.assertTrue(page.locator(".run-modal-panel").is_visible())
        self.assertTrue(page.locator(".simplified-run-hero").is_visible())
        self.assertTrue(page.locator("#liveRegion").is_hidden())

    def test_a_refused_start_leaves_the_typed_goal_alone(self) -> None:
        """The other half of emptying the box: only a sent goal is sent.

        Clearing a line earlier would lose the paragraph somebody wrote every
        time the Runtime said no — and a refusal is exactly the moment they
        need it back to try again.
        """

        page = self.open("en-US")

        def advertise_goal_ready_workflow(route):
            response = route.fetch()
            payload = response.json()
            entry = next(
                item for item in payload["data"]["workflows"]
                if item["workflow_id"] == "workflow:linear"
            )
            entry["goal_readiness"] = "ready"
            route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps(payload),
            )

        page.route("**/api/v1/workflows", advertise_goal_ready_workflow)
        # A plain refusal. `active_goal_exists` and `handler_unavailable` are
        # deliberately not it: both navigate somewhere on purpose, and what is
        # under test here is the branch that stays put and reports.
        page.route("**/api/v1/langgraph-runs", lambda route: route.fulfill(
            status=400, content_type="application/json",
            body=json.dumps({
                "schema_version": "1.0",
                "error": {
                    "code": "invalid_request",
                    "message": "the Runtime declined this goal",
                },
            }),
        ) if route.request.method == "POST" else route.continue_())
        page.reload()
        page.wait_for_selector(".simplified-workspace-composer")
        page.locator("#simplifiedWorkflow").evaluate(
            "node => { node.value = 'workflow:linear';"
            " node.dispatchEvent(new Event('change', {bubbles: true})); }"
        )
        page.fill("#simplifiedGoal", "A paragraph worth keeping")
        page.click("#newGoalStart")
        page.wait_for_timeout(800)
        self.assertFalse(page.evaluate("location.hash.startsWith('#/runs/')"))

        # Away and back, because the box still holding the text proves nothing
        # on its own: a refusal redraws nothing, so the DOM keeps whatever was
        # typed either way. What is under test is the state the composer is
        # rebuilt from, and this is what reads it.
        page.click('.nav-button[data-view="workflows"]')
        page.wait_for_selector(".workflow-grid")
        page.click('.nav-button[data-view="home"]')
        page.wait_for_selector(".simplified-workspace-composer")

        self.assertEqual(
            "A paragraph worth keeping", page.input_value("#simplifiedGoal"),
        )

    def test_only_a_named_file_is_offered_for_download(self) -> None:
        """A port's value is something to read; a file is something to save.

        Every artifact these workflows commit is a port value, so the button
        was on every one of them and meant something on none. `filename` is
        how the producer says which kind this is, and the dialog's own title
        already leans on the same fact.
        """

        run_id = self.start_run("artifact-download")

        def artifact(filename):
            body = {
                "artifact_id": "langgraph_artifact:deadbeef",
                "run_id": run_id, "node_id": "n1", "attempt_id": None,
                "port_id": "result", "schema_id": "schema:text",
                "content_type": "text/markdown", "size_bytes": 12,
                "status": "committed", "filename": filename,
            }
            return body

        # Only Download is a word here now: the way out of an overlay is the
        # corner cross every other overlay uses, so it carries its name in
        # `aria-label` rather than in print. Checked below, once, as itself.
        for label, filename, expected in (
            ("a port value", None, []),
            ("a named file", "report.md", ["Download"]),
        ):
            with self.subTest(artifact=label):
                context = self.browser.new_context(locale="en-US")
                self.addCleanup(context.close)
                page = context.new_page()
                def envelope(payload):
                    return json.dumps({
                        "schema_version": "1.0", "projection_version": None,
                        "data": payload, "next_cursor": None,
                    })

                # One parameter each, deliberately: a two-parameter handler is
                # given the Request as its second argument, so a default there
                # is not a closure — it is overwritten on every call.
                def serve_list(route):
                    route.fulfill(
                        status=200, content_type="application/json",
                        body=envelope({"artifacts": [artifact(filename)]}),
                    )

                def serve_one(route):
                    if "/content" in route.request.url:
                        route.fulfill(
                            status=200, content_type="text/markdown",
                            body="# hello",
                        )
                        return
                    route.fulfill(
                        status=200, content_type="application/json",
                        body=envelope(artifact(filename)),
                    )

                # A glob would read the `?` as a wildcard; these have to tell
                # a list apart from one artifact by it.
                page.route(re.compile(r"/api/v1/langgraph-artifacts\?"), serve_list)
                page.route(re.compile(r"/api/v1/langgraph-artifacts/"), serve_one)
                page.goto(f"{self.base}/ui/#/runs/{quote(run_id, safe='')}")
                page.wait_for_selector(".artifact-card-main")
                page.click(".artifact-card-main")
                page.wait_for_selector(".artifact-dialog .actions")
                written = [
                    line for line in
                    page.locator(".artifact-dialog .actions").inner_text().split("\n")
                    if line.strip()
                ]
                self.assertEqual(expected, written)
                # And the way out is there, named for a reader who cannot see
                # the mark.
                self.assertEqual(1, page.locator(
                    ".artifact-dialog .actions .icon-close[aria-label='Close']"
                ).count())
                page.close()

    def test_run_detail_has_no_runtime_tabs(self) -> None:
        run_id = self.start_goal("simplified-run", "Prepare a concise report")
        page = self.open("en-US", f"/ui/#/runs/{run_id}")
        page.wait_for_selector(".simplified-run-hero")

        self.assertEqual(0, page.locator(".run-tabs").count())
        self.assertEqual(0, page.locator(".why-panel").count())
        self.assertEqual(0, page.get_by_role("button", name="Run again").count())
        self.assertTrue(page.locator(".simplified-run-hero").is_visible())
        # A modal over the composer that started it, rather than a page
        # reached by navigating away from it. The composer behind is the page
        # this opened over, and dismissing lands back on it — which is why it
        # is present here where it used to be asserted absent.
        self.assertTrue(page.locator(".run-modal-panel").is_visible())
        self.assertEqual(1, page.locator(
            ".run-modal-panel .simplified-run-hero"
        ).count())
        self.assertEqual(1, page.locator(".simplified-workspace-composer").count())
        # Which run this is and how it went. The goal is read above, in the
        # modal's own head; a paragraph of it in here pushed the rest down.
        hero = page.inner_text(".simplified-run-hero")
        self.assertIn("workflow:linear", hero)
        self.assertIn(run_id, hero)
        self.assertNotIn("Prepare a concise report", hero)
        self.assertIn(
            "Prepare a concise report", page.inner_text(".run-modal-head"),
        )

    def test_a_step_that_printed_nothing_offers_no_log_to_view(self) -> None:
        """A fold that opens on "nothing yet" is a promise the row could keep.

        These steps are transforms with no attempt journal, so they print
        nothing and there is no log behind the summary. The goal detail
        already withheld it; the run page offered it on every row.
        """

        run_id = self.start_run("steps-no-output")
        page = self.open("en-US", f"/ui/#/runs/{run_id}")
        page.wait_for_selector(".step-row")
        page.wait_for_timeout(800)

        self.assertGreater(page.locator(".step-row").count(), 0)
        self.assertEqual(0, page.locator(".simplified-step-output:visible").count())
        self.assertEqual(0, page.locator(".simplified-step-prompt:visible").count())

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
        # All three rectangles from one layout. Read one at a time they
        # straddled the frame loading and a scrollbar arriving, and the boxes
        # disagreed by a pixel or two — enough to fail a delta of one, and
        # only when the machine was busy enough for the reads to land either
        # side of it.
        boxes = page.evaluate('''() => {
          const rect = (sel) => {
            const {x, y, width, height} = document.querySelector(sel).getBoundingClientRect();
            return {x, y, width, height};
          };
          return {
            heading: rect(".simplified-steps-head"),
            steps: rect(".simplified-steps"),
            frame: rect("iframe.workflow-graph-frame"),
          };
        }''')
        self.assertAlmostEqual(
            8,
            boxes["steps"]["y"] - boxes["heading"]["y"] - boxes["heading"]["height"],
            delta=1,
        )
        self.assertAlmostEqual(
            boxes["steps"]["width"], boxes["frame"]["width"], delta=1,
        )
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

    def test_the_console_is_not_downloaded_to_find_out_it_exists(self) -> None:
        """Withholding an empty fold costs a question per step. Keep it small.

        Whether a step said anything can only be answered by asking, so every
        executed row asks — and the answer is the same for one chunk as for
        two hundred. The log itself is fetched when somebody opens it.
        """

        run_id = self.start_run("console-view")
        page = self.open("en-US", f"/ui/#/runs/{run_id}")
        page.wait_for_selector(".step-row")

        requested = []
        page.on("request", lambda request: (
            requested.append(request.url) if "/output" in request.url else None
        ))
        page.reload()
        page.wait_for_selector(".step-row")
        page.wait_for_timeout(800)

        # These steps print nothing, so every ask is a probe and every probe
        # is for one chunk.
        self.assertTrue(requested)
        for url in requested:
            self.assertIn("limit=1&", url + "&")
            self.assertIn("node_id=", url)
        # And nothing was drawn, so no fold was offered.
        self.assertEqual(0, page.locator(".simplified-step-output:visible").count())
        self.assertEqual(0, page.locator(
            ".simplified-run-hero .simplified-step-output"
        ).count())

    def test_the_more_menu_carries_no_refresh_interval(self) -> None:
        """The interval was a setting nobody moved off its default.

        It is one constant in the shell now, so the menu offers theme and
        language and nothing else — and `#/settings`, which the interval
        outlived by a while, still has nowhere to go.
        """

        page = self.open("en-US")
        page.get_by_role("button", name="More", exact=True).click()
        page.get_by_role("combobox", name="Change language").wait_for()
        self.assertEqual(0, page.locator("#refreshInterval").count())
        self.assertEqual(
            0,
            page.get_by_role("combobox", name="Live refresh interval").count(),
        )

        page.goto(f"{self.base}/ui/#/settings")
        page.wait_for_function("() => location.hash === '#/home'")
        page.wait_for_selector(".simplified-workspace-composer")

    def test_history_lists_finished_goals_only(self) -> None:
        finished = self.start_goal("simplified-history", "Summarise the quarter")
        self.wait_for_status(self.open("en-US"), finished, "completed")

        page = self.open("en-US", "/ui/#/history")
        page.wait_for_selector(".history-goal-row")
        rows = page.locator(".history-goal-row")
        self.assertIn("Summarise the quarter", rows.first.inner_text())
        self.assertIn("Artifacts", rows.first.inner_text())
        self.assertTrue(page.locator(".history-day-heading").first.is_visible())
        self.assertEqual(4, page.locator(".history-status-filter").count())
        self.assertEqual(0, page.locator(".history-goal-row .status-dot").count())
        # The verdict is the last thing in the row and it is the whole of the
        # right-hand side: the chevron that used to follow it said "this
        # opens", which the row already says by being a button, and it pushed
        # the one thing worth lining up off the row's edge.
        self.assertEqual(0, page.locator(".history-goal-chevron").count())
        self.assertEqual(
            "Completed", rows.first.locator(".history-goal-tail").inner_text().strip()
        )
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
        self.assertEqual("#/history", page.evaluate("location.hash"))
        self.assertEqual(0, page.locator(".simplified-workspace-composer").count())
        self.assertEqual(0, page.get_by_role("button", name="Run again").count())

    def test_history_rows_carry_the_run_id(self) -> None:
        """The id is what a row is found by again, so the row shows it.

        A run with no goal is the exception: its title already is the id, and
        printing it twice in one row says nothing the first line did not.
        """

        named = self.start_goal("simplified-history-id", "Reconcile the ledger")
        page = self.open("en-US")
        self.wait_for_status(page, named, "completed")
        anonymous = self.start_goal("simplified-history-anon", "")
        self.wait_for_status(page, anonymous, "completed")

        page = self.open("en-US", "/ui/#/history")
        page.wait_for_selector(".history-goal-row")
        rows = page.locator(".history-goal-row")

        titled = rows.filter(has_text="Reconcile the ledger").first
        self.assertIn(named, titled.inner_text())
        self.assertEqual(named, titled.locator(".history-goal-id").inner_text())

        untitled = rows.filter(has_text=anonymous).first
        self.assertEqual(anonymous, untitled.locator(
            ".history-goal-title"
        ).inner_text())
        self.assertEqual(0, untitled.locator(".history-goal-id").count())

    def test_history_goal_detail_closes_where_it_opened(self) -> None:
        """A click opens the modal in place; closing it changes nothing."""

        finished = self.start_goal("simplified-history-dismiss", "Read a goal back")
        self.wait_for_status(self.open("en-US"), finished, "completed")

        page = self.open("en-US", "/ui/#/history")
        page.wait_for_selector(".history-goal-row")
        page.locator(".history-goal-row").first.click()
        page.wait_for_selector(".page-drawer-panel .goal-text-card")
        self.assertEqual("#/history", page.evaluate("location.hash"))

        page.get_by_role("button", name="Close").click()
        page.wait_for_function("() => !document.querySelector('.page-modal-root')")
        page.wait_for_selector(".history-goal-list")
        self.assertEqual("#/history", page.evaluate("location.hash"))

        # A pasted address still opens the detail, and from there the route
        # is what Escape walks back.
        page.goto(f"{self.base}/ui/#/history/{quote(finished, safe='')}")
        page.wait_for_selector(".page-drawer-panel .goal-text-card")
        page.keyboard.press("Escape")
        page.wait_for_function("() => location.hash === '#/history'")
        page.wait_for_function("() => !document.querySelector('.page-modal-root')")

    def test_history_follows_the_cursor_to_the_end_on_its_own(self) -> None:
        """No Load more: the whole history is here before anybody types."""

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
        page.goto(f"{self.base}/ui/#/history")
        page.wait_for_function(
            "() => document.querySelectorAll('.history-goal-row').length === 2"
        )
        self.assertIn("Second page Goal", page.locator(".history-goal-row").last.inner_text())
        self.assertEqual(0, page.get_by_role("button", name="Load more").count())

    def test_history_search_narrows_the_list_as_it_is_typed(self) -> None:
        """The box filters what is already here, and gives it all back empty."""

        context = self.browser.new_context(locale="en-US")
        self.addCleanup(context.close)
        page = context.new_page()

        def history_page(route):
            def run(key, goal):
                return {
                    "run_id": f"langgraph_run:{key}",
                    "workflow_id": "workflow:linear", "workflow_version": 1,
                    "display_name": goal, "goal": goal,
                    "status": "completed", "artifact_count": 0,
                    "created_at": "2026-07-24T08:00:00Z",
                    "updated_at": "2026-07-24T08:02:00Z",
                }
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "schema_version": "1.0", "projection_version": None,
                "data": {"runs": [
                    run("a", "Translate the release notes"),
                    run("b", "Summarise the incident"),
                    run("c", "Translate the changelog"),
                ]},
                "next_cursor": None,
            }))

        page.route("**/api/v1/langgraph-runs?*", history_page)
        page.goto(f"{self.base}/ui/#/history")
        page.wait_for_function(
            "() => document.querySelectorAll('.history-goal-row').length === 3"
        )
        # No round trip: the request count must not move while typing.
        requests: list[str] = []
        page.on("request", lambda request: requests.append(request.url))

        search = page.locator(".history-filter-bar input")
        search.fill("translate")
        page.wait_for_function(
            "() => document.querySelectorAll('.history-goal-row').length === 2"
        )
        self.assertNotIn(
            "Summarise", page.locator(".history-goal-list").inner_text()
        )
        self.assertEqual(
            [], [url for url in requests if "/api/v1/langgraph-runs" in url]
        )

        # A term nothing matches says so rather than showing a bare list.
        search.fill("nothing here matches this")
        page.wait_for_function(
            "() => document.querySelectorAll('.history-goal-row').length === 0"
        )
        self.assertTrue(page.locator(".empty").is_visible())

        search.fill("")
        page.wait_for_function(
            "() => document.querySelectorAll('.history-goal-row').length === 3"
        )

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

    def test_home_follows_a_run_started_outside_the_ui(self) -> None:
        """A short MCP Run must not disappear between Home's live polls."""

        context = self.browser.new_context(locale="en-US")
        self.addCleanup(context.close)
        page = context.new_page()
        page.goto(f"{self.base}/ui/#/home")
        page.wait_for_selector(".simplified-workspace-composer")
        page.wait_for_function(
            "() => window.performance.getEntriesByType('resource')"
            ".some(entry => entry.name.includes('/api/v1/live'))",
        )

        run_id = self.engine().start(
            "workflow:human", {"value": 1},
            idempotency_key="external-home-run", actor=LOCAL_ACTOR,
            goal="Started by another Agent App",
        ).run_id
        self.addCleanup(self.cancel_goal, run_id)

        # The first poll is immediate and has already gone by; this Run can
        # only be discovered by the next one, a fixed fifteen seconds later.
        page.wait_for_function(
            "id => location.hash === `#/runs/${encodeURIComponent(id)}`",
            arg=run_id, timeout=25_000,
        )
        page.wait_for_selector(".simplified-run-hero")
        self.assertIn(run_id, page.locator(".simplified-run-hero").inner_text())

    def test_a_settling_run_re_reads_itself_rather_than_the_whole_history(
        self,
    ) -> None:
        """History holds every run, so a live tick must not fetch every run.

        The list is loaded in full on arrival — that is what lets the search
        box match a workflow's name, which the server's query cannot. It made
        every background refresh a full re-page of the history, and the cost
        of that grows with the archive. `/live` names the runs that moved, and
        a run already drawn keeps its place, so each one is re-read on its own.
        """

        waiting = self.start_goal(
            "history-incremental", "Wait to be settled", "workflow:human",
        )
        page = self.open("en-US", "/ui/#/history")
        page.wait_for_selector(".history-goal-row")
        # Drawn once, from the full read this test is about avoiding a repeat
        # of. Everything counted below happens after that.
        page.wait_for_timeout(500)
        paged: list[str] = []
        single: list[str] = []
        page.on("request", lambda request: (
            paged.append(request.url) if "/langgraph-runs?" in request.url
            else single.append(request.url)
            if "/langgraph-runs/" in request.url
            and request.url.endswith(quote(waiting, safe=""))
            else None
        ))

        self.cancel_goal(waiting)
        # One tick of the shell's own chain, plus room for the read it makes.
        page.wait_for_timeout(19_000)

        self.assertTrue(
            single, "the settled run was never re-read on its own"
        )
        self.assertEqual(
            [], paged,
            "a run that was already on the page re-paged the whole history",
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
        page.goto(f"{self.base}/ui/#/history")
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

        # The reading order, pinned. A watcher arrives to learn whether it is
        # still working; everything under that answer is context for it.
        self.assertEqual(
            ["workflow-generation-head", "workflow-generation-request",
             "workflow-generation-stages", "workflow-generation-output",
             "workflow-generation-outcome"],
            page.locator(".workflow-generation-progress").evaluate(
                "node => [...node.children].map(c => c.className.split(' ')[0])"
            ),
        )

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
        # cross into it rather than read this document. The canvas is swapped
        # when the revision lands, so the frame is a new one to wait for.
        canvas = page.frame_locator(".workflow-graph-frame").locator(".react-flow")
        canvas.locator(".react-flow__node").first.wait_for(timeout=15_000)
        self.assertIn("Fact Check", canvas.inner_text())

    def test_a_modification_reads_top_to_bottom_as_one_account(self) -> None:
        """State, what was asked, who ran it, what it said — in that order.

        A running modification used to show none of the middle two: the
        textarea was replaced by the job, and the instruction and the Agent
        survived only in a closure. What it printed was there, with the
        Runtime's own `orbit-progress` control lines printed alongside it.
        """

        page = self.open("en-US", "/ui/#/workflows")
        self.card(page).locator(".upgrade-workflow").click()
        page.wait_for_selector(".workflow-editor textarea")
        page.get_by_role("button", name="Revise").click()

        progress = page.locator(".workflow-editor .workflow-generation-progress")
        progress.wait_for(timeout=30_000)
        self.assertEqual(
            ["workflow-generation-head", "workflow-generation-request",
             "workflow-generation-stages", "workflow-generation-output",
             "workflow-generation-outcome"],
            progress.evaluate(
                "node => [...node.children].map(c => c.className.split(' ')[0])"
            ),
        )
        # The instruction and the Agent, read back rather than remembered.
        self.assertIn(
            "Upgrade this workflow",
            page.locator(".workflow-generation-prompt-value").inner_text(),
        )
        self.assertTrue(page.locator(".workflow-generation-agent").is_visible())
        self.assertEqual(4, page.locator(".workflow-generation-step").count())

    def test_the_modify_console_never_shows_the_runtimes_own_markers(
        self,
    ) -> None:
        """`\x1eorbit-progress:` drives the stepper; it is not Agent output.

        The panel here printed every chunk verbatim, so the Runtime's own
        control lines were dumped into the console as raw JSON.
        """

        page = self.open("en-US", "/ui/#/workflows")
        self.card(page).locator(".upgrade-workflow").click()
        page.wait_for_selector(".workflow-editor textarea")
        page.get_by_role("button", name="Revise").click()
        page.wait_for_selector("#confirmRevision", timeout=30_000)

        console = page.locator(".workflow-generation-console").inner_text()
        self.assertNotIn("orbit-progress", console)

    def test_a_finished_modification_keeps_what_it_was_asked(self) -> None:
        """A settled job is gone from every payload the page is built from.

        So the account of what just changed exists only in this panel: throw
        it away when the job finishes and the question "what did that do?" has
        nowhere left to be answered.
        """

        page = self.open("en-US", "/ui/#/workflows")
        self.card(page).locator(".upgrade-workflow").click()
        page.wait_for_selector(".workflow-editor textarea")
        page.get_by_role("button", name="Revise").click()
        page.wait_for_selector("#confirmRevision", timeout=30_000)

        self.assertIn(
            "Upgrade this workflow",
            page.locator(".workflow-generation-prompt-value").inner_text(),
        )
        self.assertTrue(page.locator(".workflow-editor .change-summary").is_visible())
        # The picture below has already caught up — a summary is read against
        # the thing it describes — while the account itself waits to be
        # dismissed.
        page.frame_locator(".workflow-graph-frame").locator(
            ".react-flow__node"
        ).first.wait_for(timeout=15_000)
        # Still saying it revised, not that it generated something.
        self.assertNotIn(
            "Generat",
            page.locator(".workflow-generation-head").inner_text(),
        )

        # Dismissing it is what brings the instruction box back.
        page.click("#confirmRevision")
        page.wait_for_selector(".workflow-editor textarea")


    def test_a_background_change_does_not_take_the_account_away(self) -> None:
        """The live tick redraws the page; this one must not be redrawn.

        Publishing a revision is itself a change, and so is anything else
        happening in the Runtime meanwhile — so the account of what just
        changed was on screen only until the next tick, which is why it
        seemed to keep the confirmation button for a while and then move on
        without being asked. The tick is answered here rather than waited
        for, so the change is certain instead of incidental.
        """

        import json as json_module

        page = self.open("en-US", "/ui/#/workflows")
        self.card(page).locator(".upgrade-workflow").click()
        page.wait_for_selector(".workflow-editor textarea")
        page.get_by_role("button", name="Revise").click()
        page.wait_for_selector("#confirmRevision", timeout=30_000)

        ticks = []
        page.route("**/api/v1/live*", lambda route: (
            ticks.append(1),
            route.fulfill(status=200, content_type="application/json",
                          body=json_module.dumps({
                              "schema_version": "1.0", "projection_version": None,
                              "next_cursor": None,
                              "data": {"changed": True, "cursor": f"c{len(ticks)}"},
                          })),
        ))
        page.wait_for_timeout(18_000)

        # The tick happened and said something changed …
        self.assertGreaterEqual(len(ticks), 1)
        # … and the account is still on screen to be dismissed. Asserted on
        # what a reader would see rather than on requests: the canvas is
        # refreshed once when the revision lands, so a request counter here
        # would be counting the wrong thing.
        self.assertEqual(1, page.locator("#confirmRevision").count())
        self.assertIn(
            "Upgrade this workflow",
            page.locator(".workflow-generation-prompt-value").inner_text(),
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
        # A key per call, not per (workflow, goal length): two tests picking
        # eight-letter goals replayed each other's run, and the second one
        # then watched a run the first had already cancelled.
        key = f"start-{workflow_id}-{uuid.uuid4().hex}"
        return page.evaluate(
            """([id, goal, key]) => fetch("/api/v1/langgraph-runs", {
                 method: "POST",
                 headers: {
                   "content-type": "application/json",
                   "idempotency-key": key,
                 },
                 body: JSON.stringify({
                   workflow_id: id, input: {value: 1}, goal,
                 }),
               }).then((r) => r.json()).then((b) => b.data.run)""",
            [workflow_id, goal, key],
        )

    def test_a_long_goal_folds_and_offers_the_way_out_of_the_fold(self) -> None:
        """A goal is frequently a paragraph, and the goal page is where it is read.

        It keeps its line breaks, folds at five lines and unfolds in place. The
        measurement that decides whether the fold needs a toggle once ran in
        a single animation frame — before the block was in the document, so
        it compared 0 against 0 and hid the toggle on a goal that needed it.
        """

        page = self.open("en-US")
        page.wait_for_selector(".simplified-workspace-composer")
        workflow_id = self.publish(page, self.runnable(human=False))
        goal = "\n".join(f"line {index} of a long pasted goal" for index in range(30))
        run = self.start(page, workflow_id, goal=goal)

        page.goto(f"{self.base}/ui/#/history/{quote(run['run_id'], safe='')}")
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
        self.assertTrue(toggle.is_visible())
        self.assertEqual("Collapse", toggle.inner_text())
        self.assertEqual(
            clamped.bounding_box()["height"],
            card.locator(".goal-text").bounding_box()["height"],
        )

        page.goto(f"{self.base}/ui/#/runs/{quote(run['run_id'], safe='')}")
        self.assertEqual("Execution", page.locator(".run-modal-title").inner_text())
        self.assertEqual(
            "auto", page.locator(".run-modal-scroll").evaluate(
                "node => getComputedStyle(node).overflowY"
            ),
        )
        self.assertEqual(
            "Goal", page.locator(".run-modal-goal-title").inner_text(),
        )
        self.assertEqual(
            1, page.locator(".run-modal-goal-card .goal-text-clamp").count(),
        )
        # Both widths in one frame. Read one after the other they straddled the
        # moment a scrollbar appeared — the modal is still settling — and the
        # two answers came from layouts 1.5px apart. Measured together they
        # come from the same layout, whichever one it is.
        goal_width, steps_width = page.evaluate('''() => [
          document.querySelector(".run-modal-goal-card").getBoundingClientRect().width,
          document.querySelector(".simplified-steps").getBoundingClientRect().width,
        ]''')
        self.assertAlmostEqual(goal_width, steps_width, delta=1)
        modal_toggle = page.locator(".run-modal-head .goal-expand-toggle")
        modal_toggle.wait_for()
        self.assertEqual("Expand", modal_toggle.inner_text())
        modal_toggle.click()
        self.assertTrue(modal_toggle.is_visible())
        self.assertEqual("Collapse", modal_toggle.inner_text())

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

        meta = hero.locator(".simplified-run-meta-card")
        ids = meta.locator(".simplified-run-ids").inner_text()
        self.assertIn("Workflow name", ids)
        self.assertIn("Workflow", ids)
        self.assertIn("LangGraph Run", ids)
        self.assertIn(workflow_id, ids)
        self.assertIn(run["run_id"], ids)
        # The goal is read on the goal page; this card is not where it goes.
        self.assertNotIn("a goal long enough", hero.inner_text())

        state = meta.locator(".simplified-run-state")
        self.assertNotIn("Status", state.inner_text())
        self.assertEqual(1, state.locator(".pill").count())
        cancel = state.locator("button.danger")
        self.assertTrue(cancel.is_visible())
        # Above the run's own output, not after it. The raw interrupts JSON
        # this hero card used to carry is gone now — the same array already
        # drives the Approve/Reject button above, so the Steps section is the
        # next thing the run actually produced.
        steps_section = page.locator(".simplified-steps-section").first
        steps_section.wait_for()
        self.assertLess(
            cancel.bounding_box()["y"],
            steps_section.bounding_box()["y"],
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
        # An approval is the two answers it has, named as a person would say
        # them — not one button borrowing the command's own label.
        self.assertEqual("通过", state.locator("button.primary").first.inner_text())
        self.assertEqual(
            "拒绝", state.locator("button", has_text="拒绝").first.inner_text(),
        )

        # The browser's own confirm would arrive as a `dialog` event and
        # never reach the page; this one is the page.
        native = []
        page.on("dialog", lambda dialog: (native.append(dialog.message), dialog.dismiss()))
        state.locator("button.danger").click()
        asked = page.locator("dialog.app-dialog")
        asked.wait_for()

        self.assertEqual([], native)
        text = asked.inner_text()
        self.assertNotIn("explicit", text)
        self.assertIn("停止", text)
        # Declining leaves the run alone and takes the dialog away.
        asked.get_by_role("button", name="取消", exact=True).click()
        asked.wait_for(state="detached")
        self.assertEqual(
            "取消执行", state.locator("button.danger").inner_text(),
        )

    def test_the_run_card_does_not_duplicate_the_modals_close_control(self) -> None:
        """The fixed title bar owns the one way to close this modal."""

        page = self.open("zh-CN")
        page.wait_for_selector(".simplified-workspace-composer")
        workflow_id = self.publish(page, self.runnable(human=True))

        # While it is still running there is a stop, but no textual Close.
        running = self.start(page, workflow_id, goal="仍在执行")
        page.goto(f"{self.base}/ui/#/runs/{quote(running['run_id'], safe='')}")
        page.wait_for_selector(".simplified-run-hero")
        state = page.locator(".simplified-run-state")
        self.assertIn("取消执行", state.inner_text())
        self.assertEqual(0, state.get_by_role(
            "button", name="关闭", exact=True,
        ).count())

        # One goal at a time per actor, so this one has to be let go of before
        # the next can be started.
        self.cancel(page, running)
        finished = self.start(
            page, self.publish(page, self.runnable(human=False)), goal="已经跑完",
        )
        self.wait_for_status(page, finished["run_id"], "completed")
        page.goto(f"{self.base}/ui/#/runs/{quote(finished['run_id'], safe='')}")
        page.wait_for_selector(".simplified-run-hero")

        self.assertEqual(0, page.locator(".simplified-run-state").get_by_role(
            "button", name="关闭", exact=True,
        ).count())
        self.assertEqual(1, page.locator(".run-modal-titlebar").get_by_role(
            "button", name="关闭", exact=True,
        ).count())

    def test_watching_a_run_does_not_redraw_the_page_around_it(self) -> None:
        """A tick changes which steps got where, so that is what it changes.

        Watching used to redraw everything every few seconds: the fold
        somebody had opened closed, the graph frame reloaded its bundle and
        drew again, and the page moved under the reader. The live endpoint is
        answered here rather than waited on, so the tick is deterministic;
        what is measured is what the tick did to the page.
        """

        import json as json_module

        page = self.open("en-US")
        page.wait_for_selector(".simplified-workspace-composer")
        workflow_id = self.publish(page, self.runnable(human=True))
        run = self.start(page, workflow_id, goal="watching")
        self.addCleanup(self.cancel, page, run)

        ticks = []
        page.route("**/api/v1/live*", lambda route: (
            ticks.append(1),
            route.fulfill(status=200, content_type="application/json",
                          body=json_module.dumps({
                              "schema_version": "1.0", "projection_version": None,
                              "next_cursor": None,
                              "data": {"changed": True, "cursor": f"c{len(ticks)}"},
                          })),
        ))
        page.goto(f"{self.base}/ui/#/runs/{quote(run['run_id'], safe='')}")
        page.wait_for_selector(".step-row")
        page.wait_for_timeout(600)

        # Marked so a rebuild is detectable, and a fold left open: the fold
        # and the graph frame are what a reader actually loses when the page
        # is thrown away and drawn again.
        page.evaluate("""() => {
          document.querySelector('.step-list').dataset.probe = 'kept';
          document.querySelector('iframe').dataset.probe = 'kept';
          document.querySelector('details').open = true;
        }""")
        steps = []
        page.on("request", lambda request: (
            steps.append(1) if "/steps" in request.url else None
        ))

        # The default cadence, waited out rather than reconfigured: the
        # interval control is a custom widget over a hidden native select, and
        # driving it would be testing that instead of this.
        page.wait_for_timeout(18000)

        self.assertGreaterEqual(len(ticks), 1)
        # The tick did happen — the steps were re-read …
        self.assertGreaterEqual(len(steps), 1)
        # … and the page around them was left alone.
        self.assertEqual({"list": True, "frame": True, "open": True},
                         page.evaluate("""() => ({
                           list: document.querySelector('.step-list')
                                 ?.dataset.probe === 'kept',
                           frame: document.querySelector('iframe')
                                  ?.dataset.probe === 'kept',
                           open: document.querySelector('details')?.open === true,
                         })"""))

    def test_answering_a_step_is_asked_in_the_page(self) -> None:
        """A rejection asks why, in the page, in this UI's words.

        Answering an approval used to mean editing JSON — first in the
        browser's own prompt box, which carried none of this UI's words, and
        then in a labelled field that still asked a reviewer to know the port
        to reply on and the `decision` field the branches test. Approving is
        now a button; rejecting is the one that still asks something, because
        why is the part the UI cannot supply.
        """

        page = self.open("zh-CN")
        page.wait_for_selector(".simplified-workspace-composer")
        workflow_id = self.publish(page, self.runnable(human=True))
        run = self.start(page, workflow_id, goal="answering")
        self.addCleanup(self.cancel, page, run)

        page.goto(f"{self.base}/ui/#/runs/{quote(run['run_id'], safe='')}")
        approve = page.locator(".simplified-run-state button.primary").first
        approve.wait_for()
        self.assertEqual("通过", approve.inner_text().strip())
        native = []
        page.on("dialog", lambda dialog: (native.append(dialog.message), dialog.dismiss()))
        reject = page.locator(".simplified-run-state button", has_text="拒绝").first
        reject.click()

        dialog = page.locator("dialog.app-dialog")
        dialog.wait_for()
        self.assertEqual([], native)
        # A labelled field for the reason — empty, because it is the
        # reviewer's sentence and not a shape to edit.
        field = dialog.locator("textarea")
        self.assertTrue(field.is_visible())
        self.assertEqual("", field.input_value())
        self.assertIn("驳回原因", dialog.inner_text())

    def test_approving_asks_nothing_and_sends_what_the_ui_encoded(self) -> None:
        """Approve is the whole answer; nobody is shown the submission.

        Both facts a reply needs — the port to answer on and the `decision`
        field the branches test — are in the interrupt the run is stopped on,
        so the button builds the submission and sends it. Nothing is asked,
        by this UI or by the browser, and what the run settles on is what the
        UI encoded rather than something a reviewer retyped.
        """

        page = self.open("zh-CN")
        page.wait_for_selector(".simplified-workspace-composer")
        workflow_id = self.publish(page, self.runnable(human=True))
        run = self.start(page, workflow_id, goal="approving")

        page.goto(f"{self.base}/ui/#/runs/{quote(run['run_id'], safe='')}")
        approve = page.locator(".simplified-run-state button.primary").first
        approve.wait_for()
        native = []
        page.on("dialog", lambda dialog: (native.append(dialog.message), dialog.dismiss()))
        approve.click()
        self.wait_for_status(page, run["run_id"], "completed")

        self.assertEqual([], native)
        self.assertEqual(0, page.locator("dialog.app-dialog").count())
        # This workflow's result *is* the submission, so the run holds the
        # exact object the button encoded — approved, and with the `value`
        # the Runtime requires present and empty.
        settled = page.evaluate(
            "id => fetch(`/api/v1/langgraph-runs/${encodeURIComponent(id)}`)"
            ".then(r => r.json()).then(b => b.data.result)",
            run["run_id"],
        )
        self.assertEqual({"decision": "approve", "value": None}, settled)

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
        # The lock is on the two things that would start a second goal. The
        # primary button is no longer a disabled sentence about the run: the
        # goal page stopped drawing the run underneath itself, so this is the
        # way to it, and the only one this page has.
        self.assertTrue(page.locator("#simplifiedGoal").is_disabled())
        self.assertTrue(page.locator("#simplifiedWorkflow").is_disabled())
        self.assertFalse(page.locator("#newGoalStart").is_disabled())

        page.goto(f"{self.base}/ui/#/home")
        page.wait_for_selector(".simplified-workspace-composer")
        page.click("#newGoalStart")
        page.wait_for_selector(".run-modal-panel")
        self.assertIn(quote(run["run_id"], safe=""), page.evaluate("location.hash"))

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

        long_prompt = "\n".join(f"instruction line {index}" for index in range(1, 8))

        def serve_long_prompt(route):
            response = route.fetch()
            payload = response.json()
            payload["data"]["steps"][0]["prompt"] = long_prompt
            route.fulfill(response=response, json=payload)

        page.route("**/api/v1/langgraph-runs/*/steps", serve_long_prompt)

        page.goto(f"{self.base}/ui/#/runs/"
                  f"{quote(run['data']['run']['run_id'], safe='')}")
        page.wait_for_selector(".simplified-step-output summary")
        page.locator(".simplified-step-output summary").first.click()
        prompt = page.locator(".simplified-step-prompt").first
        prompt.wait_for()

        self.assertIn("instruction line 7", prompt.inner_text())
        toggle = page.locator(".step-prompt-toggle").first
        toggle.wait_for()
        self.assertLess(prompt.evaluate("node => node.clientHeight"),
                        prompt.evaluate("node => node.scrollHeight"))
        self.assertEqual("Expand", toggle.inner_text())
        toggle.click()
        self.assertEqual("true", toggle.get_attribute("aria-expanded"))
        self.assertEqual(prompt.evaluate("node => node.clientHeight"),
                         prompt.evaluate("node => node.scrollHeight"))
        self.assertEqual("Collapse", toggle.inner_text())
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
    """Installed Agent names stay available across CLI upgrades.

    Both steps carry stale version and fingerprint pins, but their logical
    Agent names are installed. They therefore need neither substitution nor a
    missing-Agent notice.
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

    def test_stale_cli_versions_do_not_show_a_missing_agent_notice(self) -> None:

        page = self.open("en-US")
        page.goto(f"{self.base}/ui/#/workflows/workflow:two-substitutes")
        page.locator(".workflow-detail").wait_for()

        self.assertEqual(
            0, page.locator(".workflow-agent-binding p").count(),
        )
