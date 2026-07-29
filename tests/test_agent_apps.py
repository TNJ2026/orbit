from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from orbit.__main__ import _runtime_db_path
from orbit.agent_apps.host import AgentAppHost, AgentAppHostError
from orbit.agent_apps.event_bridge import AgentAppEventBridge, EventInbox
from orbit.agent_apps.manifest import ManifestError, load_manifest
from orbit.agent_apps.mcp_proxy import serve_proxy
from orbit.platform.projects import project_db_path


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
        self.temp = tempfile.TemporaryDirectory()
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


class _Process:
    pid = 12345
    returncode = None

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


class HostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
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
        with tempfile.TemporaryDirectory() as temporary:
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
        self.temp = tempfile.TemporaryDirectory()
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
        self.temp = tempfile.TemporaryDirectory()
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
