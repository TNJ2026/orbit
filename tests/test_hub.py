from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orbit.hub import (
    WorkspaceRegistry, WorkspaceRuntimeManager, create_hub_app, workspace_urls,
)
from orbit.platform.projects import project_id
from orbit.platform.runtime_ownership import DiscoveredRuntime
from tests.test_web_composition import AsgiHarness


class WorkspaceRegistryTests(unittest.TestCase):
    def test_production_manifest_uses_the_fixed_global_hub(self) -> None:
        manifest = json.loads(
            (Path(__file__).resolve().parents[1] / "agent-app.json").read_text()
        )

        self.assertEqual("global", manifest["scope"])
        self.assertEqual(["uv", "run", "--project", "{manifest_dir}", "orbit", "hub", "serve"], manifest["service"]["command"])
        self.assertEqual("http://127.0.0.1:8848/health/ready", manifest["service"]["ready_url"])
        self.assertEqual("http://127.0.0.1:8848/mcp", manifest["mcp"]["url"])

    def test_registration_is_stable_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "project"
            registry = WorkspaceRegistry(root / "workspaces.json")
            identifier, resolved = registry.register(workspace)

            self.assertEqual(project_id(workspace), identifier)
            self.assertEqual(workspace.resolve(), resolved)
            self.assertEqual(workspace.resolve(), WorkspaceRegistry(registry.path).resolve(identifier))

    def test_default_workspace_is_created_on_first_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            default = root / "default"
            registry = WorkspaceRegistry(root / "workspaces.json")
            with mock.patch.dict(
                "os.environ", {"ORBIT_DEFAULT_WORKSPACE": str(default)}, clear=False,
            ):
                self.assertEqual(default.resolve(), registry.resolve(None))
            self.assertTrue(default.is_dir())

    def test_workspace_urls_are_namespaced(self) -> None:
        self.assertEqual(
            {
                "workspace_id": "abc",
                "mcp_url": "http://127.0.0.1:8848/workspaces/abc/mcp",
                "ui_url": "http://127.0.0.1:8848/workspaces/abc/ui/",
                "events_url": "ws://127.0.0.1:8848/workspaces/abc/events",
            },
            workspace_urls("abc"),
        )


class WorkspaceRuntimeManagerTests(unittest.TestCase):
    def test_existing_runtime_for_the_workspace_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = WorkspaceRegistry(root / "workspaces.json")
            identifier, workspace = registry.register(root / "project")
            runtime = DiscoveredRuntime(root / "owner.lock", {
                "project_root": str(workspace), "base_url": "http://127.0.0.1:41001",
            })
            launches = []
            manager = WorkspaceRuntimeManager(
                registry=registry, runtime_discovery=lambda: (runtime,),
                health_check=lambda _url: True,
                launcher=lambda path: launches.append(path),
            )

            self.assertEqual("http://127.0.0.1:41001", manager.ensure(identifier))
            self.assertEqual([], launches)

    def test_two_workspaces_start_and_resolve_independent_runtimes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = WorkspaceRegistry(root / "workspaces.json")
            first_id, first = registry.register(root / "first")
            second_id, second = registry.register(root / "second")
            active = {}

            def discover():
                return tuple(
                    DiscoveredRuntime(root / f"{identifier}.lock", {
                        "project_root": str(workspace), "base_url": url,
                    })
                    for identifier, (workspace, url) in active.items()
                )

            def launch(workspace):
                identifier = project_id(workspace)
                active[identifier] = (workspace, f"http://127.0.0.1:{41000 + len(active)}")

            manager = WorkspaceRuntimeManager(
                registry=registry, runtime_discovery=discover,
                health_check=lambda _url: True, launcher=launch, sleep=lambda _seconds: None,
            )

            first_url = manager.ensure(first_id)
            second_url = manager.ensure(second_id)
            self.assertNotEqual(first_url, second_url)
            self.assertEqual({first.resolve(), second.resolve()}, {item[0] for item in active.values()})


class HubHttpTests(unittest.TestCase):
    class Manager:
        def __init__(self):
            self.identifiers = []

        def ensure(self, identifier=None):
            self.identifiers.append(identifier)
            return "http://127.0.0.1:41001"

    def test_initialize_is_owned_by_the_hub_without_starting_a_runtime(self) -> None:
        manager = self.Manager()
        with AsgiHarness(create_hub_app(manager)) as client:
            response = client.request("POST", "/mcp", body={
                "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
            })

        self.assertEqual("orbit", response.json()["result"]["serverInfo"]["name"])
        self.assertTrue(response.headers["mcp-session-id"])
        self.assertEqual([], manager.identifiers)

    def test_default_and_named_tool_requests_use_the_internal_backend(self) -> None:
        manager = self.Manager()
        app = create_hub_app(manager)
        answer = {"result": {"tools": [{"name": "list_runs"}]}}
        with mock.patch(
            "orbit.hub._forward",
            return_value=(200, json.dumps(answer).encode(), "application/json"),
        ) as forward, AsgiHarness(app) as client:
            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            default = client.request("POST", "/mcp", body=request)
            named = client.request("POST", "/workspaces/project-a/mcp", body=request)

        for response in (default, named):
            names = [item["name"] for item in response.json()["result"]["tools"]]
            self.assertEqual("list_runs", names[0])
            self.assertIn("list_workspaces", names)
            self.assertIn("select_workspace", names)
        self.assertEqual([None, "project-a"], manager.identifiers)
        self.assertEqual(
            "http://127.0.0.1:41001/internal/v1/agent-tools",
            forward.call_args_list[0].args[0],
        )
        self.assertEqual(
            {"operation": "list"},
            json.loads(forward.call_args_list[0].args[1]),
        )

    def test_a_session_can_select_a_workspace_by_readable_name(self) -> None:
        manager = self.Manager()

        class Registry:
            def list(self):
                return [{
                    "workspace_id": "project-a", "name": "Orbit Project",
                    "path": "/projects/orbit", "kind": "registered",
                }]

            def select(self, **selection):
                self.selection = selection
                return "project-a"

        manager.registry = Registry()
        backend = {"result": {"tools": []}}
        with mock.patch(
            "orbit.hub._forward",
            return_value=(200, json.dumps(backend).encode(), "application/json"),
        ), AsgiHarness(create_hub_app(manager)) as client:
            initialized = client.request("POST", "/mcp", body={
                "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
            })
            session_id = initialized.headers["mcp-session-id"]
            headers = {"mcp-session-id": session_id}
            selected = client.request("POST", "/mcp", headers=headers, body={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {
                    "name": "select_workspace",
                    "arguments": {"name": "Orbit Project"},
                },
            })
            client.request("POST", "/mcp", headers=headers, body={
                "jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {},
            })

        self.assertFalse(selected.json()["result"]["isError"])
        self.assertEqual({"path": None, "name": "Orbit Project"}, manager.registry.selection)
        self.assertEqual(["project-a"], manager.identifiers)

    def test_ui_redirects_to_the_selected_runtime(self) -> None:
        manager = self.Manager()
        with AsgiHarness(create_hub_app(manager)) as client:
            response = client.get("/workspaces/project-a/ui/assets/app.js?theme=dark")

        self.assertEqual(307, response.status_code)
        self.assertEqual(
            "http://127.0.0.1:41001/ui/assets/app.js?theme=dark",
            response.headers["location"],
        )
        self.assertEqual(["project-a"], manager.identifiers)


if __name__ == "__main__":
    unittest.main()
