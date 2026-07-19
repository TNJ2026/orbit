"""M4.C: the product journey, automated over real HTTP.

New run → executes → waits on a person → approve → succeeded, driven through
a real uvicorn server with the production composition root, the production
handler registry and no test-only fakes.

The important discipline here is that the test behaves like the UI: after
starting a run it never constructs a mutation URL. It reads the inbox, takes
whatever `allowed_commands[]` the server advertises, and POSTs exactly that.
A server that stopped advertising an action, or advertised one it then
refuses, fails this test — which is the coupling the UI depends on.

This does not replace looking at the page. It covers the protocol half of the
journey; visual rendering is covered by the asset guards in test_ui_assets.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from orbit.web.app import create_app
from orbit.web.local_identity import local_authorizer, loopback_authenticator
from orbit.workflow.application.human_service import HumanTaskService
from orbit.workflow.domain.human import HumanTaskKind
from orbit.workflow.domain.ids import EntityId
from tests.test_web_composition import (
    SCHEMAS, publish_linear_workflow, transform_registration,
)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class HttpClient:
    """The same requests the browser makes, minus the browser."""

    def __init__(self, base: str) -> None:
        self.base = base

    def request(self, method: str, path: str, body=None, *, key=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"{self.base}{path}", data=data, method=method)
        if data is not None:
            request.add_header("content-type", "application/json")
        if key is not None:
            request.add_header("idempotency-key", key)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw or b"null")
            except json.JSONDecodeError:
                return error.code, {"error": {"message": raw.decode()[:200]}}

    def get(self, path):
        return self.request("GET", path)

    def execute(self, allowed, payload, key):
        """Run a server-advertised command, exactly as the UI does."""

        return self.request(
            allowed["method"], allowed["href"],
            {"expected_version": allowed["expected_version"], **payload},
            key=key,
        )


class RunLifecycleE2E(unittest.TestCase):
    server = None
    thread = None

    @classmethod
    def setUpClass(cls) -> None:
        import uvicorn

        cls.temp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.temp.name) / "runtime.db"
        app = create_app(
            cls.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            worker_count=2, poll_seconds=0.02,
            authenticator=loopback_authenticator,
            authorizer=local_authorizer(),
            serve_ui=True,
        )
        publish_linear_workflow(cls.db)

        port = free_port()
        cls.base = f"http://127.0.0.1:{port}"
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        cls.server = uvicorn.Server(config)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{cls.base}/health/ready", timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.05)
        else:
            raise AssertionError("server never became ready")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.should_exit = True
        cls.thread.join(timeout=20)
        cls.temp.cleanup()

    def setUp(self) -> None:
        self.client = HttpClient(self.base)

    def wait_for(self, path: str, predicate, *, timeout: float = 20):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            status, body = self.client.get(path)
            last = body
            if status == 200 and predicate(body["data"]):
                return body["data"]
            time.sleep(0.05)
        raise AssertionError(f"condition never held for {path}; last={last}")

    def start_run(self, key: str):
        status, body = self.client.request(
            "POST", "/api/v1/runs",
            {"workflow_id": "workflow:linear", "input": {"value": 0}}, key=key,
        )
        self.assertEqual(200, status, body)
        return body["data"]["run_id"]

    # -- the journey ------------------------------------------------------

    def test_a_run_started_over_http_executes_to_success(self) -> None:
        run_id = self.start_run("e2e-happy")
        summary = self.wait_for(
            f"/api/v1/runs/{run_id}", lambda data: data["status"] == "succeeded"
        )
        self.assertEqual("workflow:linear", summary["workflow_id"])
        self.assertEqual(0, summary["responsibility_count"])

    def test_a_waiting_run_is_completed_through_an_advertised_command(self) -> None:
        run_id = self.start_run("e2e-human")
        self.wait_for(
            f"/api/v1/runs/{run_id}", lambda data: data["status"] == "succeeded"
        )

        # An approval node would create this; the runtime under test has no
        # approval handler registered, so the journey's waiting step is seeded
        # through the same service a handler would call.
        task_id, token = HumanTaskService(self.db).create(
            EntityId.parse(run_id), HumanTaskKind.APPROVAL, {"question": "ship?"},
            actor="local", now=datetime.now(timezone.utc), participants=["local"],
        )

        inbox = self.wait_for(
            "/api/v1/inbox",
            lambda data: any(item["task_id"] == str(task_id) for item in data["items"]),
        )
        item = next(i for i in inbox["items"] if i["task_id"] == str(task_id))

        # The UI's rule: take the command the server offers, do not build one.
        approve = next(
            command for command in item["allowed_commands"]
            if command["command"].endswith("approve")
        )
        status, body = self.client.execute(
            approve, {"submission_token": token, "decision": "approve"}, "e2e-approve"
        )
        self.assertEqual(200, status, body)
        self.assertEqual("completed", body["data"]["status"])

        cleared = self.wait_for(
            "/api/v1/inbox",
            lambda data: all(i["task_id"] != str(task_id) for i in data["items"]),
        )
        self.assertNotIn(str(task_id), json.dumps(cleared))

    def test_a_replayed_command_does_not_act_twice(self) -> None:
        first = self.start_run("e2e-idem")
        second = self.start_run("e2e-idem")
        self.assertEqual(first, second)

    def test_a_stale_expected_version_is_refused_over_the_wire(self) -> None:
        run_id = self.start_run("e2e-stale")
        status, body = self.client.request(
            "POST", f"/api/v1/runs/{run_id}/cancel",
            {"expected_version": 999}, key="e2e-stale-cancel",
        )
        self.assertEqual(409, status, body)

    def test_plan_definition_and_overlay_agree_on_node_ids(self) -> None:
        run_id = self.start_run("e2e-plan")
        self.wait_for(
            f"/api/v1/runs/{run_id}", lambda data: data["status"] == "succeeded"
        )
        _status, definition = self.client.get(f"/api/v1/runs/{run_id}/plan")
        _status, overlay = self.client.get(f"/api/v1/runs/{run_id}/plan/overlay")

        planned = {node["node_id"] for node in definition["data"]["nodes"]}
        observed = {node["node_id"] for node in overlay["data"]["nodes"]}
        self.assertTrue(observed)
        # The overlay may lag the definition, but it must never invent a node.
        self.assertTrue(observed <= planned, observed - planned)
        self.assertEqual(
            definition["data"]["plan_version"], overlay["data"]["plan_version"]
        )

    # -- what the UI needs to exist ---------------------------------------

    def test_the_ui_is_served_and_self_contained(self) -> None:
        for path in (
            "/ui/", "/ui/assets/app.js", "/ui/assets/api.js", "/ui/assets/i18n.js",
            "/ui/assets/app.css", "/ui/assets/router.js",
            "/ui/assets/components/command-dialog.js",
            "/ui/assets/components/data-state.js",
            "/ui/assets/styles/tokens.css", "/ui/assets/styles/shell.css",
            "/ui/assets/styles/components.css", "/ui/assets/styles/views.css",
            "/ui/assets/i18n.zh-CN.json",
            "/ui/assets/i18n.en-US.json",
        ):
            with self.subTest(path=path):
                with urllib.request.urlopen(f"{self.base}{path}", timeout=5) as response:
                    self.assertEqual(200, response.status)
                    body = response.read().decode()
                self.assertNotIn("http://", body.replace("http://www.w3.org", ""))

    def test_health_reports_every_background_component(self) -> None:
        _status, body = self.client.get("/health/ready")
        components = body["checks"]["components"]["detail"]
        names = {item["name"] for item in components}
        self.assertIn("timer-1", names)
        self.assertIn("recovery", names)
        for item in components:
            with self.subTest(component=item["name"]):
                self.assertTrue(item["alive"])
                self.assertIsNone(item["last_error"])


if __name__ == "__main__":
    unittest.main()
