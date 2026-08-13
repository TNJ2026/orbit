"""The graph editor, driven by a real browser.

Two things in the editor cannot be reached by testing its modules: an edge
only exists once somebody has dragged one, and a publish conflict only happens
when two writers race. Both were verified by hand while the editor was built,
which is not the same as verified.

playwright is a test-only dependency and the editor bundle is a build artifact,
so this skips rather than fails when either is absent — a plain checkout still
runs green.
"""

from __future__ import annotations

from importlib import resources
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
import uuid

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - exercised by the skip
    sync_playwright = None

from orbit.web.app import create_app
from orbit.web.api_v1 import Authorizer
from orbit.web.local_identity import LOCAL_ACTOR, LOCAL_SCOPES, loopback_authenticator
from orbit.workflow.artifacts.local_cas import LocalCASBackend
from orbit.workflow.api.routes import RateLimiter
from orbit.web.app import HandlerRegistration
from orbit.workflow.catalogs import HandlerManifest
from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.handlers import ResourceProfile
from orbit.workflow.handlers import TransformHandler
from tests.test_web_composition import SCHEMAS, transform_registration


def schema_bearing_registration() -> HandlerRegistration:
    """A Handler whose config schema is the shape a discovered Agent carries.

    The real one comes from `agent_discovery`, which needs a CLI installed;
    this declares the same schema so the form has something to render from.
    """

    manifest = HandlerManifest(
        "agent.fake", "1.0.0", ("action",),
        {"value": "example://integer/1.0"},
        {"value": "example://integer/1.0"},
        {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "timeout_seconds": {
                    "type": "integer", "minimum": 1, "maximum": 1800,
                    "description": "How long this step may run.",
                },
            },
            "additionalProperties": False,
        },
        ExecutionSafety.REPLAY_SAFE,
        ResourceProfile(100, 100, 5, 60, 1_000_000, "test"),
        "schema://object/1.0", (), (), True, True,
    )
    return HandlerRegistration(manifest, TransformHandler(), "agent.fake@1.0.0")


EDITOR_BUILT = resources.files("orbit").joinpath(
    "static/workflow-editor/index.html"
).is_file()

WORKFLOW_ID = "workflow:editable"

SOURCE = {
    "dsl_version": "1.3",
    "metadata": {"id": "editable", "name": "Editable"},
    "nodes": [
        {
            "id": "work", "kind": "action", "label": "Transform",
            "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
            "outputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
            "handler": {"name": "transform", "version": "1.0.0"},
        },
        {
            "id": "done", "kind": "terminal", "label": "Finished",
            "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
        },
    ],
    "edges": [{
        "id": "flow",
        "from": {"node": "work", "port": "value"},
        "to": {"node": "done", "port": "value"},
    }],
    "entry": ["work"],
    "terminals": ["done"],
    "result": {"node": "work", "port": "value"},
    "policies": [{
        "id": "complete", "kind": "completion",
        "config": {"required_terminal_count": 1},
    }],
}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@unittest.skipUnless(sync_playwright, "playwright is not installed")
@unittest.skipUnless(EDITOR_BUILT, "editor bundle is not built in this checkout")
class EditorBrowserTests(unittest.TestCase):
    """One server and one browser for the class; a page per test."""

    @classmethod
    def setUpClass(cls) -> None:
        import uvicorn

        cls.temp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.temp.name) / "runtime.db"
        app = create_app(
            cls.db,
            handlers=[transform_registration(), schema_bearing_registration()],
            schemas=SCHEMAS,
            worker_count=1, poll_seconds=0.02,
            authenticator=loopback_authenticator,
            authorizer=Authorizer(
                lambda actor: tuple(LOCAL_SCOPES) if actor == LOCAL_ACTOR else ()
            ),
            artifact_backend=LocalCASBackend(Path(cls.temp.name) / "artifacts"),
            rate_limiter=RateLimiter(requests=10_000),
            serve_ui=True,
            # The editor and the workflows API it lives on are both here only.
            workflow_ui_mode="multi-agent",
        )
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

    # -- server-side helpers ----------------------------------------------

    def post(self, path: str, body: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base}{path}", data=json.dumps(body).encode(),
            headers={
                "content-type": "application/json",
                "idempotency-key": str(uuid.uuid4()),
            },
        )
        with urllib.request.urlopen(request) as response:
            return json.load(response)["data"]

    def get(self, path: str) -> dict:
        with urllib.request.urlopen(f"{self.base}{path}") as response:
            return json.load(response)["data"]

    def latest(self) -> int:
        return next(
            item["latest_version"] for item in self.get("/api/v1/workflows")["workflows"]
            if item["workflow_id"] == WORKFLOW_ID
        )

    def publish(self, source: dict, expected: int) -> dict:
        return self.post(
            f"/api/v1/workflows/{WORKFLOW_ID}/versions",
            {"source": json.dumps(source), "expected_version": expected},
        )

    def setUp(self) -> None:
        # Each test starts from a workflow of its own, so a publish in one
        # cannot decide what another sees.
        self.workflow = json.loads(json.dumps(SOURCE))
        self.workflow["metadata"]["id"] = f"editable_{uuid.uuid4().hex[:8]}"
        self.workflow_id = f"workflow:{self.workflow['metadata']['id']}"
        self.post(
            f"/api/v1/workflows/{self.workflow_id}/versions",
            {"source": json.dumps(self.workflow), "expected_version": 0},
        )

    def open_editor(self):
        context = self.browser.new_context()
        page = context.new_page()
        self.addCleanup(context.close)
        self.errors: list[str] = []
        page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        page.goto(f"{self.base}/editor/")
        page.wait_for_selector("select[aria-label='Workflow']")
        page.select_option("select[aria-label='Workflow']", self.workflow_id)
        page.wait_for_selector(".react-flow__node")
        return page

    @staticmethod
    def node(page, node_id: str):
        """Locate by node id, not by label.

        A node's label lives in an input, so its text is not in the DOM's text
        content and nothing that reads text can find it.
        """

        return page.locator(f'.react-flow__node:has([data-node-id="{node_id}"])')

    def connect(self, page, source_id: str, target_id: str) -> None:
        """Drag from one node's output handle to another's input handle.

        React Flow starts a connection on `pointerdown` over a handle and
        finishes it on `pointerup` over another, so this has to be a real
        pointer path rather than a click on each end. The intermediate move is
        what makes the library treat it as a drag at all.
        """

        start = self.node(page, source_id).locator(
            ".react-flow__handle-right"
        ).bounding_box()
        end = self.node(page, target_id).locator(
            ".react-flow__handle-left"
        ).bounding_box()
        page.mouse.move(start["x"] + start["width"] / 2, start["y"] + start["height"] / 2)
        page.mouse.down()
        page.mouse.move(
            end["x"] + end["width"] / 2, end["y"] + end["height"] / 2, steps=20,
        )
        page.mouse.up()
        page.wait_for_timeout(300)

    # -- tests -------------------------------------------------------------

    def test_an_edge_can_be_deleted_and_drawn_again(self) -> None:
        """The drag is what has no substitute in a module test.

        Removing the only edge and drawing it back is the sharpest form: the
        document is valid at both ends, and what the drag produced has to be
        the same connection that was there — the same two ports, in the same
        direction — for it to publish at all.
        """

        page = self.open_editor()
        self.assertEqual(1, page.locator(".react-flow__edge").count())

        page.locator(".react-flow__edge").click()
        page.keyboard.press("Delete")
        page.wait_for_timeout(300)
        self.assertEqual(0, page.locator(".react-flow__edge").count())

        self.connect(page, "work", "done")
        self.assertEqual(1, page.locator(".react-flow__edge").count())

        page.click("button:has-text('Publish')")
        page.wait_for_timeout(2500)
        self.assertIn("published v2", page.locator(".notice").inner_text())

        stored = json.loads(self.get(f"/api/v1/workflows/{self.workflow_id}")["source"])
        self.assertEqual(1, len(stored["edges"]))
        drawn = stored["edges"][0]
        self.assertEqual({"node": "work", "port": "value"}, drawn["from"])
        self.assertEqual({"node": "done", "port": "value"}, drawn["to"])
        self.assertEqual([], self.errors)

    def test_a_connection_between_mismatched_schemas_cannot_be_dropped(self) -> None:
        page = self.open_editor()
        page.locator(".react-flow__edge").click()
        page.keyboard.press("Delete")
        page.wait_for_timeout(300)

        # Give the terminal a second input whose schema does not match.
        self.node(page, "done").click()
        page.wait_for_selector(".inspector .port-add")
        inputs = page.locator(".inspector fieldset:has(.port-add)").nth(0)
        inputs.locator("input").nth(0).fill("other")
        inputs.locator("input").nth(1).fill("example://other/1.0")
        inputs.locator("button:has-text('add')").click()
        page.wait_for_timeout(400)

        start = self.node(page, "work").locator(
            ".react-flow__handle-right"
        ).bounding_box()
        end = self.node(page, "done").locator(
            ".react-flow__handle-left"
        ).last.bounding_box()
        page.mouse.move(start["x"] + start["width"] / 2, start["y"] + start["height"] / 2)
        page.mouse.down()
        page.mouse.move(end["x"] + end["width"] / 2, end["y"] + end["height"] / 2, steps=20)
        page.mouse.up()
        page.wait_for_timeout(400)

        # Refused while dragging, so it cannot be dropped there at all — the
        # author is stopped before there is anything to undo.
        self.assertEqual(0, page.locator(".react-flow__edge").count())
        self.assertEqual([], self.errors)

    def test_a_stale_tab_is_refused_and_recovers_by_reopening(self) -> None:
        page = self.open_editor()
        self.assertEqual("v1", page.locator(".bar .version").inner_text())

        # Somebody else publishes while this tab is open.
        elsewhere = json.loads(json.dumps(self.workflow))
        elsewhere["nodes"][1]["label"] = "Finished elsewhere"
        self.post(
            f"/api/v1/workflows/{self.workflow_id}/versions",
            {"source": json.dumps(elsewhere), "expected_version": 1},
        )
        latest = next(
            item["latest_version"] for item in self.get("/api/v1/workflows")["workflows"]
            if item["workflow_id"] == self.workflow_id
        )
        self.assertEqual(2, latest)

        page.locator("input[aria-label='Label for work']").fill("Renamed while stale")
        page.wait_for_timeout(200)
        page.click("button:has-text('Publish')")
        page.wait_for_timeout(2500)

        notice = page.locator(".notice").inner_text()
        self.assertIn("publish conflict: expected 1, actual 2", notice)
        self.assertIn("reopen", notice)
        # Refused, not applied: the store still holds what the other writer put
        # there, and Publish stops inviting a refusal it will get again.
        self.assertTrue(page.locator("button:has-text('Publish')").is_disabled())
        stored = json.loads(self.get(f"/api/v1/workflows/{self.workflow_id}")["source"])
        self.assertEqual("Finished elsewhere", stored["nodes"][1]["label"])
        self.assertEqual("Transform", stored["nodes"][0]["label"])

        page.select_option("select[aria-label='Workflow']", self.workflow_id)
        page.wait_for_timeout(1200)
        self.assertEqual("v2", page.locator(".bar .version").inner_text())
        self.assertFalse(page.locator("button:has-text('Publish')").is_disabled())
        self.assertEqual([], self.errors)

    def drag_node(self, page, node_id: str, dx: int, dy: int) -> None:
        # By the kind label, not the header: the header is mostly the label
        # input, which carries `nodrag` so that its text can be selected.
        box = self.node(page, node_id).locator(".kind").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(
            box["x"] + box["width"] / 2 + dx,
            box["y"] + box["height"] / 2 + dy,
            steps=15,
        )
        page.mouse.up()
        page.wait_for_timeout(300)

    def stored_layout(self, page):
        """The arrangement as persisted, in graph coordinates.

        Not the on-screen box: dragging a node can pan the viewport, so screen
        positions move for reasons that have nothing to do with the layout.
        """

        return page.evaluate(
            "key => JSON.parse(localStorage.getItem(key) || 'null')",
            f"orbit.editor.layout.{self.workflow_id}",
        )

    def test_an_arrangement_survives_reopening_without_touching_the_definition(
        self,
    ) -> None:
        """Positions are how a drawing is read, not what it means.

        `definition_hash` is the workflow's version identity, so a coordinate
        inside it would make nudging a node publish a new version. Moving one
        has to persist, and the definition has to be untouched by it.
        """

        page = self.open_editor()
        computed = self.stored_layout(page)
        self.drag_node(page, "work", 0, 180)
        arranged = self.stored_layout(page)
        # In graph units, which the viewport's zoom scales the drag down into.
        self.assertGreater(arranged["work"]["y"], computed["work"]["y"] + 30)

        # Reopened from the store, not from a page that still had it in hand.
        page.reload()
        page.wait_for_selector("select[aria-label='Workflow']")
        page.select_option("select[aria-label='Workflow']", self.workflow_id)
        page.wait_for_selector(".react-flow__node")
        # Equal, not merely present: an editor that ignored what was stored
        # would draw the computed layout and persist that instead.
        self.assertEqual(arranged, self.stored_layout(page))

        # And the definition never learned about any of it.
        page.click("button:has-text('Publish')")
        page.wait_for_timeout(2500)
        self.assertIn("unchanged", page.locator(".notice").inner_text())
        stored = json.loads(self.get(f"/api/v1/workflows/{self.workflow_id}")["source"])
        self.assertEqual(self.workflow["nodes"], stored["nodes"])
        self.assertEqual([], self.errors)

    def test_resetting_the_layout_returns_to_the_computed_one(self) -> None:
        page = self.open_editor()
        computed = self.stored_layout(page)
        self.drag_node(page, "work", 0, 180)
        self.assertNotEqual(computed, self.stored_layout(page))

        page.click("button:has-text('Reset layout')")
        page.wait_for_timeout(500)
        self.assertEqual(computed, self.stored_layout(page))
        self.assertEqual([], self.errors)

    def test_the_delete_button_removes_a_node_and_the_edges_that_named_it(self) -> None:
        page = self.open_editor()
        self.assertEqual(2, page.locator(".react-flow__node").count())
        self.assertEqual(1, page.locator(".react-flow__edge").count())

        self.node(page, "done").click()
        page.wait_for_selector(".inspector h2")
        page.click(".bar button:has-text('Delete')")
        page.wait_for_timeout(400)

        # The edge goes with it: one naming a node that is gone cannot compile.
        self.assertEqual(1, page.locator(".react-flow__node").count())
        self.assertEqual(0, page.locator(".react-flow__edge").count())
        self.assertEqual([], self.errors)

    def test_a_handler_schema_becomes_a_form_that_enforces_its_bounds(self) -> None:
        """The schema is the Handler's own, and it already says all of this.

        Asking an author to hand-type JSON the Runtime could describe to them
        is asking them to guess at something they were already told — and the
        bounds it carries are refused here rather than at publish.
        """

        agent = json.loads(json.dumps(self.workflow))
        agent["nodes"][0]["handler"] = {"name": "agent.fake", "version": "1.0.0"}
        agent["nodes"][0]["config"] = {"prompt": "do it", "timeout_seconds": 600}
        self.post(
            f"/api/v1/workflows/{self.workflow_id}/versions",
            {"source": json.dumps(agent), "expected_version": 1},
        )

        page = self.open_editor()
        self.node(page, "work").click()
        page.wait_for_selector(".inspector fieldset")
        legends = page.locator(".inspector fieldset legend").all_inner_texts()
        self.assertIn("Config", legends)

        number = page.locator(".inspector input[type=number]")
        self.assertEqual("600", number.input_value())
        self.assertEqual("1", number.get_attribute("min"))
        self.assertEqual("1800", number.get_attribute("max"))

        number.fill("9999")
        page.wait_for_timeout(300)
        self.assertIn("at most 1800", page.locator(".inspector .problem").first.inner_text())

        number.fill("900")
        page.wait_for_timeout(300)
        page.click("button:has-text('Publish')")
        page.wait_for_timeout(2500)
        self.assertIn("published", page.locator(".notice").inner_text())

        stored = json.loads(self.get(f"/api/v1/workflows/{self.workflow_id}")["source"])
        # A number, not the text of one, and nothing else disturbed.
        self.assertEqual(
            {"prompt": "do it", "timeout_seconds": 900}, stored["nodes"][0]["config"]
        )
        self.assertEqual([], self.errors)

    def test_a_handler_without_a_schema_keeps_the_raw_editor(self) -> None:
        """`transform` takes whatever it likes; a form would say otherwise."""

        page = self.open_editor()
        self.node(page, "work").click()
        page.wait_for_selector(".inspector")
        self.assertNotIn(
            "Config", page.locator(".inspector fieldset legend").all_inner_texts()
        )
        self.assertIn(
            "declares no config schema",
            page.locator(".inspector label:has(span:text('Config')) .hint").inner_text(),
        )
        self.assertEqual([], self.errors)

    def test_what_decides_a_route_is_readable_without_clicking(self) -> None:
        """A conditional edge and a plain one used to be the same grey curve.

        The route and the condition are what decide where a run goes, so
        reading a graph meant opening every edge in it to find out.
        """

        routed = json.loads(json.dumps(self.workflow))
        routed["nodes"][0]["route_mode"] = "parallel"
        routed["nodes"].append({
            "id": "other", "kind": "terminal", "label": "Other",
            "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
        })
        routed["terminals"] = ["done", "other"]
        routed["edges"][0]["condition"] = "source.value > 5"
        routed["edges"].append({
            "id": "on_error", "route": "error", "priority": 1,
            "from": {"node": "work", "port": "value"},
            "to": {"node": "other", "port": "value"},
        })
        self.post(
            f"/api/v1/workflows/{self.workflow_id}/versions",
            {"source": json.dumps(routed), "expected_version": 1},
        )

        page = self.open_editor()
        page.wait_for_selector(".edge-label")
        labels = page.locator(".edge-label").all_inner_texts()
        self.assertEqual(2, len(labels), labels)
        self.assertTrue(any("source.value > 5" in text for text in labels), labels)
        self.assertTrue(any("error" in text.lower() for text in labels), labels)
        self.assertEqual([], self.errors)

    def test_an_ordinary_edge_carries_no_label(self) -> None:
        """`success` with no condition is the default; saying it everywhere
        would bury the edges that are not."""

        page = self.open_editor()
        page.wait_for_selector(".react-flow__edge")
        page.wait_for_timeout(400)
        self.assertEqual(1, page.locator(".react-flow__edge").count())
        self.assertEqual(0, page.locator(".edge-label").count())
        self.assertEqual([], self.errors)

    def test_a_workflow_published_without_a_source_cannot_be_edited(self) -> None:
        from tests.test_web_composition import publish_linear_workflow

        publish_linear_workflow(self.db)
        page = self.open_editor()
        page.select_option("select[aria-label='Workflow']", "workflow:linear")
        page.wait_for_timeout(800)
        self.assertIn(
            "without an authored source", page.locator(".notice").inner_text()
        )
        self.assertEqual([], self.errors)


if __name__ == "__main__":
    unittest.main()
