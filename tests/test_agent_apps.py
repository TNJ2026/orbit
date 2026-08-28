from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from orbit.__main__ import _runtime_db_path
from orbit.agent_apps import host as host_module
from orbit.agent_apps.host import AgentAppHost, AgentAppHostError
from orbit.agent_apps.event_bridge import AgentAppEventBridge, EventInbox
from orbit.agent_apps.manifest import ManifestError, load_manifest
from orbit.agent_apps.mcp_proxy import forward_http, serve_proxy
from orbit.platform.projects import project_db_path
from orbit.platform.runtime_ownership import DiscoveredRuntime


def write_manifest(root: Path, **overrides) -> Path:
    payload = {
        "schema_version": 1,
        "id": "sample-app",
        "scope": "workspace",
        "service": {
            "command": ["sample", "--workspace", "{workspace}"],
            "cwd": ".",
            "environment": ["PATH"],
            "ready_url": "http://127.0.0.1:9911/health/ready",
            "timeout_seconds": 2,
        },
        "ui": {"url": "http://127.0.0.1:9911/ui/"},
        "mcp": {"transport": "http-jsonrpc", "url": "http://127.0.0.1:9911/mcp"},
    }
    for key, value in overrides.items():
        payload[key] = value
    path = root / "agent-app.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_manifest_resolves_plugin_cwd_and_validates_loopback_urls(self) -> None:
        manifest = load_manifest(write_manifest(self.root))
        self.assertEqual(self.root.resolve(), manifest.service.cwd)
        self.assertEqual("sample-app", manifest.app_id)

    def test_manifest_rejects_non_loopback_service(self) -> None:
        path = write_manifest(self.root)
        payload = json.loads(path.read_text())
        payload["service"]["ready_url"] = "https://example.com/ready"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ManifestError, "loopback"):
            load_manifest(path)

    def test_manifest_rejects_cwd_escape(self) -> None:
        path = write_manifest(self.root)
        payload = json.loads(path.read_text())
        payload["service"]["cwd"] = ".."
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ManifestError, "stay inside"):
            load_manifest(path)

    def test_manifest_accepts_only_loopback_websocket_events(self) -> None:
        manifest = load_manifest(write_manifest(self.root, events={
            "transport": "websocket", "url": "ws://127.0.0.1:9911/events",
        }))
        self.assertEqual("ws://127.0.0.1:9911/events", manifest.events.url)
        path = write_manifest(self.root, events={
            "transport": "websocket", "url": "wss://example.com/events",
        })
        with self.assertRaisesRegex(ManifestError, "loopback"):
            load_manifest(path)

    def test_manifest_accepts_declared_orbit_runtime_discovery(self) -> None:
        path = write_manifest(self.root)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["service"]["discovery"] = "orbit-runtime"
        path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertEqual(
            "orbit-runtime", load_manifest(path).service.discovery,
        )


class _Process:
    pid = 12345
    returncode = None

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


class HostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.manifest_path = write_manifest(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_ready_instance_is_reused_without_launching(self) -> None:
        health = iter((False, False, True))
        first = AgentAppHost(
            state_root=self.root / "state",
            health_check=lambda _url: next(health),
            launcher=lambda *_args: _Process(),
            sleep=lambda _seconds: None,
        )
        first.ensure(self.manifest_path, workspace=self.workspace)
        launched = []
        second = AgentAppHost(
            state_root=self.root / "state",
            health_check=lambda _url: True,
            launcher=lambda *_args: launched.append(True),
            process_exists=lambda _pid: True,
        )
        ensured = second.ensure(self.manifest_path, workspace=self.workspace)
        self.assertFalse(ensured.started)
        self.assertEqual([], launched)

    def test_discovered_workspace_runtime_is_reused_at_its_published_port(self) -> None:
        path = write_manifest(self.root)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["service"]["discovery"] = "orbit-runtime"
        payload["events"] = {
            "transport": "websocket", "url": "ws://127.0.0.1:9911/events",
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        runtime = DiscoveredRuntime(Path("owner.lock"), {
            "base_url": "http://127.0.0.1:51325",
            "project_root": str(self.workspace.resolve()),
        })
        launched = []
        host = AgentAppHost(
            state_root=self.root / "state",
            health_check=lambda url: url == "http://127.0.0.1:51325/health/ready",
            launcher=lambda *_args: launched.append(True),
            runtime_discovery=lambda: (runtime,),
        )

        ensured = host.ensure(path, workspace=self.workspace)

        self.assertFalse(ensured.started)
        self.assertEqual([], launched)
        self.assertEqual("http://127.0.0.1:51325/ui/", ensured.manifest.ui_url)
        self.assertEqual("http://127.0.0.1:51325/mcp", ensured.manifest.mcp.url)
        self.assertEqual("ws://127.0.0.1:51325/events", ensured.manifest.events.url)

    def test_active_workspace_can_be_recovered_from_runtime_discovery(self) -> None:
        path = write_manifest(self.root)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["service"]["discovery"] = "orbit-runtime"
        path.write_text(json.dumps(payload), encoding="utf-8")
        runtime = DiscoveredRuntime(Path("owner.lock"), {
            "base_url": "http://127.0.0.1:51325",
            "project_root": str(self.workspace.resolve()),
        })
        host = AgentAppHost(
            state_root=self.root / "state",
            health_check=lambda url: url == "http://127.0.0.1:51325/health/ready",
            runtime_discovery=lambda: (runtime,),
        )

        self.assertEqual(
            self.workspace.resolve(), host.active_workspace(path),
        )

    def test_ready_owner_compares_canonical_workspace_paths(self) -> None:
        health = iter((False, False, True))
        first = AgentAppHost(
            state_root=self.root / "state",
            health_check=lambda _url: next(health),
            launcher=lambda *_args: _Process(),
            sleep=lambda _seconds: None,
        )
        ensured = first.ensure(self.manifest_path, workspace=self.workspace)
        pid_path = ensured.state_dir / "pid.json"
        payload = json.loads(pid_path.read_text(encoding="utf-8"))
        payload["workspace"] = str(self.workspace / ".." / "workspace")
        pid_path.write_text(json.dumps(payload), encoding="utf-8")

        second = AgentAppHost(
            state_root=self.root / "state", health_check=lambda _url: True,
            process_exists=lambda _pid: True,
        )
        self.assertFalse(
            second.ensure(self.manifest_path, workspace=self.workspace).started
        )

    def test_ready_endpoint_owned_by_another_workspace_is_rejected(self) -> None:
        health = iter((False, False, True))
        first = AgentAppHost(
            state_root=self.root / "state",
            health_check=lambda _url: next(health),
            launcher=lambda *_args: _Process(),
            sleep=lambda _seconds: None,
        )
        first.ensure(self.manifest_path, workspace=self.workspace)
        other = self.root / "other-workspace"
        other.mkdir()
        second = AgentAppHost(
            state_root=self.root / "state",
            health_check=lambda _url: True,
            process_exists=lambda _pid: True,
        )
        with self.assertRaisesRegex(AgentAppHostError, "different Agent App scope"):
            second.ensure(self.manifest_path, workspace=other)

    def test_active_workspace_reuses_the_only_live_managed_scope(self) -> None:
        health = iter((False, False, True))
        first = AgentAppHost(
            state_root=self.root / "state",
            health_check=lambda _url: next(health),
            launcher=lambda *_args: _Process(),
            sleep=lambda _seconds: None,
        )
        first.ensure(self.manifest_path, workspace=self.workspace)
        finder = AgentAppHost(
            state_root=self.root / "state", process_exists=lambda _pid: True,
        )
        self.assertEqual(
            self.workspace.resolve(), finder.active_workspace(self.manifest_path),
        )

    def test_active_workspace_fails_when_no_managed_scope_is_live(self) -> None:
        host = AgentAppHost(state_root=self.root / "state")
        with self.assertRaisesRegex(AgentAppHostError, "open the App first"):
            host.active_workspace(self.manifest_path)

    def test_workspace_scope_launches_once_and_expands_declared_placeholder(self) -> None:
        health = iter((False, False, True))
        launches = []

        def launcher(manifest, state_dir, workspace):
            launches.append((manifest, state_dir, workspace))
            return _Process()

        host = AgentAppHost(
            state_root=self.root / "state",
            health_check=lambda _url: next(health),
            launcher=launcher,
            sleep=lambda _seconds: None,
        )
        ensured = host.ensure(self.manifest_path, workspace=self.workspace)
        self.assertTrue(ensured.started)
        self.assertEqual(self.workspace.resolve(), launches[0][2])
        self.assertTrue((ensured.state_dir / "pid.json").exists())

    def test_default_launcher_expands_workspace_in_argv(self) -> None:
        health = iter((False, False, True))
        host = AgentAppHost(
            state_root=self.root / "state",
            health_check=lambda _url: next(health),
            sleep=lambda _seconds: None,
        )
        with mock.patch(
            "orbit.agent_apps.host.subprocess.Popen", return_value=_Process()
        ) as launch:
            host.ensure(self.manifest_path, workspace=self.workspace)
        arguments, keyword_arguments = launch.call_args
        self.assertIn(str(self.workspace.resolve()), arguments[0])
        self.assertEqual(self.workspace.resolve(), keyword_arguments["cwd"])

    def test_timeout_stops_the_process_tree(self) -> None:
        host = AgentAppHost(
            state_root=self.root / "state",
            health_check=lambda _url: False,
            launcher=lambda *_args: _Process(),
            clock=iter((0.0, 3.0)).__next__,
            sleep=lambda _seconds: None,
        )
        with mock.patch("orbit.agent_apps.host.descendant_pids", return_value=[]), mock.patch(
            "orbit.agent_apps.host.terminate_pid_tree"
        ) as terminate:
            with self.assertRaisesRegex(AgentAppHostError, "did not become ready"):
                host.ensure(self.manifest_path, workspace=self.workspace)
        terminate.assert_called_once_with(_Process.pid)


class ProjectRootTests(unittest.TestCase):
    def test_explicit_project_root_selects_default_runtime_database(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            project_root = Path(temporary)
            (project_root / "pyproject.toml").write_text(
                "[project]\nname='sample'\nversion='0'\n", encoding="utf-8"
            )
            self.assertEqual(
                str(project_db_path(project_root)),
                _runtime_db_path(None, project_root=project_root),
            )


class McpProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.manifest = load_manifest(write_manifest(self.root))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_proxy_forwards_payload_and_preserves_response(self) -> None:
        seen = []
        source = io.StringIO('{"jsonrpc":"2.0","id":7,"method":"ping"}\n')
        sink = io.StringIO()

        def forward(url, message):
            seen.append((url, message))
            return {"jsonrpc": "2.0", "id": 7, "result": {}}

        serve_proxy(self.manifest, stdin=source, stdout=sink, forward=forward)
        self.assertEqual(self.manifest.mcp.url, seen[0][0])
        self.assertEqual("ping", seen[0][1]["method"])
        self.assertEqual({}, json.loads(sink.getvalue())["result"])

    def test_proxy_maps_transport_failure_to_json_rpc_error(self) -> None:
        source = io.StringIO('{"jsonrpc":"2.0","id":"x","method":"ping"}\n')
        sink = io.StringIO()
        serve_proxy(
            self.manifest, stdin=source, stdout=sink,
            forward=lambda _url, _message: (_ for _ in ()).throw(RuntimeError("offline")),
        )
        response = json.loads(sink.getvalue())
        self.assertEqual(-32603, response["error"]["code"])
        self.assertEqual("x", response["id"])

    def test_a_transport_failure_says_what_to_do_and_keeps_what_it_was(self) -> None:
        """The message is a sentence; the exception text rides beside it.

        `MCP endpoint is unavailable: [Errno 61] Connection refused` is true and
        is not an instruction.  A reader needs to know whether to start
        something, wait, or go and read a log — and still needs the original,
        because the reading is a guess and a guess nobody can check is worse
        than none.
        """

        source = io.StringIO('{"jsonrpc":"2.0","id":"x","method":"ping"}\n')
        sink = io.StringIO()
        raw = "MCP endpoint is unavailable: [Errno 61] Connection refused"
        serve_proxy(
            self.manifest, stdin=source, stdout=sink,
            forward=lambda _url, _message: (_ for _ in ()).throw(RuntimeError(raw)),
        )
        error = json.loads(sink.getvalue())["error"]
        self.assertIn("not listening", error["message"])
        self.assertNotIn("Errno", error["message"])
        self.assertEqual(raw, error["data"]["detail"])

    def test_an_unreadable_failure_is_not_dressed_up_as_a_known_one(self) -> None:
        """A wrong diagnosis sends somebody to fix the wrong thing."""

        source = io.StringIO('{"jsonrpc":"2.0","id":"x","method":"ping"}\n')
        sink = io.StringIO()
        serve_proxy(
            self.manifest, stdin=source, stdout=sink,
            forward=lambda _url, _message: (_ for _ in ()).throw(RuntimeError("weird")),
        )
        error = json.loads(sink.getvalue())["error"]
        self.assertIn("does not recognise", error["message"])
        self.assertEqual("weird", error["data"]["detail"])

    def test_orbits_own_error_is_forwarded_rather_than_reworded(self) -> None:
        """Orbit answered the question; a proxy that rewrote it would be
        putting words in the Runtime's mouth."""

        source = io.StringIO('{"jsonrpc":"2.0","id":"x","method":"ping"}\n')
        sink = io.StringIO()
        answered = {
            "jsonrpc": "2.0", "id": "x",
            "error": {"code": -32001, "message": "valid actor credentials are required"},
        }
        serve_proxy(
            self.manifest, stdin=source, stdout=sink,
            forward=lambda _url, _message: answered,
        )
        self.assertEqual(answered, json.loads(sink.getvalue()))

    def test_proxy_rejects_malformed_input_without_calling_service(self) -> None:
        source = io.StringIO("not json\n")
        sink = io.StringIO()
        serve_proxy(
            self.manifest, stdin=source, stdout=sink,
            forward=lambda _url, _message: self.fail("must not forward malformed input"),
        )
        self.assertEqual(-32700, json.loads(sink.getvalue())["error"]["code"])

    def test_proxy_captures_events_and_exposes_local_inbox_tools(self) -> None:
        manifest = load_manifest(write_manifest(self.root, events={
            "transport": "websocket", "url": "ws://127.0.0.1:9911/events",
        }))
        source = io.StringIO("\n".join((
            '{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
            '{"jsonrpc":"2.0","id":2,"method":"tools/call",'
            '"params":{"name":"list_app_events","arguments":{}}}',
            '{"jsonrpc":"2.0","id":3,"method":"tools/call",'
            '"params":{"name":"ack_app_event","arguments":{"event_id":"event:e1"}}}',
        )) + "\n")
        sink = io.StringIO()

        class Bridge:
            def __init__(self, _url, inbox):
                self.inbox = inbox

            def start(self):
                self.inbox.accept({
                    "type": "runtime_event", "event_id": "event:e1",
                    "event_type": "run_started", "cursor": "cursor-1",
                })

            def stop(self):
                return None

        serve_proxy(
            manifest, stdin=source, stdout=sink, state_dir=self.root / "state",
            bridge_factory=Bridge,
            forward=lambda _url, message: {
                "jsonrpc": "2.0", "id": message["id"],
                "result": {"tools": [{"name": "remote", "inputSchema": {}}]},
            },
        )
        responses = [json.loads(line) for line in sink.getvalue().splitlines()]
        names = [tool["name"] for tool in responses[0]["result"]["tools"]]
        self.assertEqual(
            ["remote", "wait_app_event", "list_app_events", "ack_app_event"], names,
        )
        listed = json.loads(responses[1]["result"]["content"][0]["text"])
        self.assertEqual("event:e1", listed["events"][0]["event_id"])
        acknowledged = json.loads(responses[2]["result"]["content"][0]["text"])
        self.assertTrue(acknowledged["acknowledged"])


class EventBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.inbox = EventInbox(Path(self.temp.name) / "events.db")

    def test_inbox_deduplicates_persists_and_acknowledges(self) -> None:
        frame = {
            "type": "runtime_event", "event_id": "event:e1",
            "event_type": "run_started", "cursor": "cursor-1",
        }
        self.assertTrue(self.inbox.accept(frame))
        self.assertFalse(self.inbox.accept(frame))
        reopened = EventInbox(self.inbox.path)
        self.assertEqual("cursor-1", reopened.cursor())
        self.assertEqual([frame], reopened.pending())
        self.assertTrue(reopened.acknowledge("event:e1"))
        self.assertEqual([], reopened.pending())

    def test_wait_does_not_miss_an_event_inserted_before_condition_wait(self) -> None:
        frame = {
            "type": "runtime_event", "event_id": "event:race",
            "event_type": "run_started", "cursor": "cursor-race",
        }
        original_pending = self.inbox.pending
        first = True

        def racing_pending(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                self.inbox.accept(frame)
                return []
            return original_pending(*args, **kwargs)

        self.inbox.pending = racing_pending
        self.assertEqual(frame, self.inbox.wait(timeout_seconds=0.1))

    def test_bridge_resumes_from_persisted_cursor(self) -> None:
        self.inbox.accept({"type": "ready", "cursor": "resume-me"})
        opened = []

        class Socket:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return None

            def recv(self, timeout=None):
                if not hasattr(self, "sent"):
                    self.sent = True
                    return json.dumps({
                        "type": "runtime_event", "event_id": "event:e2",
                        "event_type": "run_succeeded", "cursor": "cursor-2",
                    })
                raise TimeoutError

        def connect(url):
            opened.append(url)
            return Socket()

        bridge = AgentAppEventBridge(
            "ws://127.0.0.1:9911/events", self.inbox,
            connector=connect, reconnect_seconds=0.01,
        )
        bridge.start()
        deadline = __import__("time").monotonic() + 2
        while not self.inbox.pending() and __import__("time").monotonic() < deadline:
            __import__("time").sleep(0.01)
        bridge.stop()
        self.assertIn("cursor=resume-me", opened[0])
        self.assertEqual("event:e2", self.inbox.pending()[0]["event_id"])


if __name__ == "__main__":
    unittest.main()


class HostHelperTests(unittest.TestCase):
    """The small decisions the host makes before it starts anything.

    Each is a fallback for an environment that answers differently, so a run
    that works walks past all of them.
    """

    def manifest(self, scope: str) -> object:
        import types

        return types.SimpleNamespace(scope=scope)

    def test_a_global_app_shares_one_scope_whatever_the_workspace(self) -> None:
        key = host_module._scope_key(self.manifest("global"), None)
        self.assertEqual("global", key)
        self.assertEqual(
            key, host_module._scope_key(self.manifest("global"), Path("/somewhere")),
        )

    def test_a_workspace_app_keys_on_the_workspace(self) -> None:
        first = host_module._scope_key(self.manifest("workspace"), Path("/a"))
        second = host_module._scope_key(self.manifest("workspace"), Path("/b"))
        self.assertNotEqual(first, second)
        self.assertEqual(
            first, host_module._scope_key(self.manifest("workspace"), Path("/a")),
        )
        # Hashed, so a path never becomes a directory name of its own.
        self.assertRegex(first, r"^[0-9a-f]{16}$")

    def test_a_workspace_app_without_a_workspace_is_refused(self) -> None:
        with self.assertRaisesRegex(AgentAppHostError, "requires a workspace"):
            host_module._scope_key(self.manifest("workspace"), None)

    def test_the_state_root_follows_the_environment_when_set(self) -> None:
        with mock.patch.dict(
            "os.environ", {"AGENT_APP_STATE_DIR": "~/elsewhere"}, clear=False,
        ):
            self.assertEqual(
                Path("~/elsewhere").expanduser(), host_module.default_state_root(),
            )

    def test_the_state_root_has_a_default(self) -> None:
        import os

        environment = {k: v for k, v in os.environ.items() if k != "AGENT_APP_STATE_DIR"}
        with mock.patch.dict("os.environ", environment, clear=True):
            self.assertTrue(str(host_module.default_state_root()).endswith("agent-apps"))

    def test_a_process_check_is_false_for_nothing_and_true_for_this_one(self) -> None:
        import os

        for pid in (0, -1):
            with self.subTest(pid=pid):
                self.assertFalse(host_module._process_exists(pid))
        self.assertTrue(host_module._process_exists(os.getpid()))

    def test_health_is_false_when_nothing_answers(self) -> None:
        """A closed port, not a slow one: the check must not hang the host."""

        self.assertFalse(
            host_module._health_check("http://127.0.0.1:9/health", timeout=0.5),
        )

    def test_health_reads_the_status_and_not_the_body(self) -> None:
        import contextlib
        import types

        def answer(status):
            @contextlib.contextmanager
            def opener(request, timeout=None):
                yield types.SimpleNamespace(status=status)
            return opener

        for status, expected in ((200, True), (302, True), (404, False), (500, False)):
            with self.subTest(status=status):
                with mock.patch.object(host_module, "urlopen", answer(status)):
                    self.assertEqual(
                        expected, host_module._health_check("http://example/health"),
                    )


class _Endpoint:
    """A real HTTP MCP endpoint on a port nobody chose in advance.

    The proxy's transport was only ever tested with `forward` replaced by a
    lambda, which is a test of the loop around it and of nothing it actually
    does: the status handling, the body-on-error path, and every failure the
    socket can produce were unexercised. This is small enough to be worth
    having and real enough to have caught them.
    """

    def __init__(self, respond) -> None:
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
                length = int(self.headers.get("content-length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                endpoint.seen.append(json.loads(body))
                # Lower-cased on the way in: HTTP header names are
                # case-insensitive and `urllib` capitalises what it sends, so
                # an assertion on the literal key is about urllib, not about
                # the proxy.
                endpoint.headers.append(
                    {key.lower(): value for key, value in self.headers.items()}
                )
                status, payload = respond(json.loads(body))
                raw = payload.encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_args) -> None:
                """Silence: a test's output is its assertions."""

        self.seen: list = []
        self.headers: list = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/mcp"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class McpProxyTransportTests(unittest.TestCase):
    """`forward_http` against a socket, rather than against a stand-in."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.endpoints: list[_Endpoint] = []

    def tearDown(self) -> None:
        for endpoint in self.endpoints:
            endpoint.close()
        self.temp.cleanup()

    def _endpoint(self, respond) -> _Endpoint:
        endpoint = _Endpoint(respond)
        self.endpoints.append(endpoint)
        return endpoint

    def test_it_round_trips_one_message_over_a_socket(self) -> None:
        endpoint = self._endpoint(lambda message: (
            200, json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"ok": True}}),
        ))
        answer = forward_http(endpoint.url, {"jsonrpc": "2.0", "id": 3, "method": "ping"})
        self.assertEqual({"ok": True}, answer["result"])
        self.assertEqual("ping", endpoint.seen[0]["method"])
        self.assertEqual("application/json", endpoint.headers[0]["content-type"])

    def test_an_error_orbit_answered_with_is_forwarded_not_swallowed(self) -> None:
        """A 4xx carrying a JSON-RPC error is Orbit's answer, not a transport
        failure. Raising here would replace what Orbit said with what the proxy
        guessed, and the caller would never see the reason it was refused."""

        refusal = {
            "jsonrpc": "2.0", "id": 4,
            "error": {"code": -32001, "message": "valid actor credentials are required"},
        }
        endpoint = self._endpoint(lambda _message: (400, json.dumps(refusal)))
        self.assertEqual(refusal, forward_http(endpoint.url, {"jsonrpc": "2.0", "id": 4}))

    def test_a_status_with_no_body_is_a_transport_failure(self) -> None:
        """Nothing to forward, so the status is all there is to report."""

        endpoint = self._endpoint(lambda _message: (502, ""))
        with self.assertRaises(RuntimeError) as caught:
            forward_http(endpoint.url, {"jsonrpc": "2.0", "id": 5})
        self.assertIn("HTTP 502", str(caught.exception))

    def test_an_unreadable_body_is_named_as_such(self) -> None:
        endpoint = self._endpoint(lambda _message: (200, "{not json"))
        with self.assertRaises(RuntimeError) as caught:
            forward_http(endpoint.url, {"jsonrpc": "2.0", "id": 6})
        self.assertIn("invalid JSON", str(caught.exception))

    def test_an_empty_answer_is_not_a_failure(self) -> None:
        """A notification is answered with nothing, and nothing is correct."""

        endpoint = self._endpoint(lambda _message: (200, ""))
        self.assertIsNone(forward_http(endpoint.url, {"jsonrpc": "2.0", "method": "note"}))

    def test_a_port_nobody_is_listening_on_reads_as_unavailable(self) -> None:
        endpoint = self._endpoint(lambda _message: (200, "{}"))
        url = endpoint.url
        endpoint.close()
        self.endpoints.remove(endpoint)
        with self.assertRaises(RuntimeError) as caught:
            forward_http(url, {"jsonrpc": "2.0", "id": 7})
        self.assertIn("unavailable", str(caught.exception))

    def test_a_service_that_never_answers_is_bounded(self) -> None:
        """The wedge this timeout exists for: a Runtime holding the socket open
        and saying nothing. Given its own short deadline so the test does not
        wait out the five-and-a-half minute one the proxy ships with."""

        held = threading.Event()
        self.addCleanup(held.set)

        def respond(_message):
            held.wait(timeout=30)
            return 200, "{}"

        endpoint = self._endpoint(respond)
        with self.assertRaises(RuntimeError) as caught:
            forward_http(endpoint.url, {"jsonrpc": "2.0", "id": 8}, timeout=0.25)
        self.assertIn("unavailable", str(caught.exception))
        held.set()


class McpProxyEndToEndTests(unittest.TestCase):
    """The whole pipe: a line of stdin becomes a request on a socket."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _manifest(self, endpoint: _Endpoint):
        return load_manifest(write_manifest(
            self.root, mcp={"transport": "http-jsonrpc", "url": endpoint.url},
        ))

    def test_stdin_reaches_the_socket_and_the_answer_reaches_stdout(self) -> None:
        endpoint = _Endpoint(lambda message: (
            200, json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"tools": []}}),
        ))
        self.addCleanup(endpoint.close)
        sink = io.StringIO()
        serve_proxy(
            self._manifest(endpoint),
            stdin=io.StringIO('{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'),
            stdout=sink,
        )
        self.assertEqual({"tools": []}, json.loads(sink.getvalue())["result"])
        self.assertEqual("tools/list", endpoint.seen[0]["method"])

    def test_a_runtime_that_dies_mid_session_is_explained_not_echoed(self) -> None:
        """Two messages with a shutdown between them, ordered by the generator
        rather than by a sleep: the first is answered, the Runtime goes away,
        and the second has to say what happened in words somebody can act on
        while keeping the text they would quote."""

        endpoint = _Endpoint(lambda message: (
            200, json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {}}),
        ))

        def lines():
            yield '{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
            endpoint.close()
            yield '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'

        sink = io.StringIO()
        serve_proxy(self._manifest(endpoint), stdin=lines(), stdout=sink)
        first, second = [json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertEqual({}, first["result"])
        error = second["error"]
        self.assertEqual(2, second["id"])
        # A sentence, and not the exception's own wording.
        self.assertIn("not listening", error["message"])
        self.assertNotIn("Errno", error["message"])
        # And the wording it replaced, for whoever has to fix it.
        self.assertIn("unavailable", error["data"]["detail"])
