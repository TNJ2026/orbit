from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orbit.hub import (
    HubError, MultipleRuntimesError, WorkspaceRegistry,
    WorkspaceRuntimeManager, create_hub_app, workspace_urls,
)
from orbit.global_control import WorkflowTemplateStore
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
            workspace.mkdir()
            registry = WorkspaceRegistry(root / "workspaces.json")
            identifier, resolved = registry.register(workspace)

            self.assertEqual(project_id(workspace), identifier)
            self.assertEqual(workspace.resolve(), resolved)
            self.assertEqual(workspace.resolve(), WorkspaceRegistry(registry.path).resolve(identifier))

    def test_a_registration_can_be_taken_back(self) -> None:
        """Registering made a directory routable; nothing was ever the reverse.

        So a directory opened once stayed in the list for good, and a suite
        that opens a throwaway directory per run added one every time.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "project"
            workspace.mkdir()
            registry = WorkspaceRegistry(root / "workspaces.json")
            identifier, _ = registry.register(workspace)

            self.assertTrue(registry.forget(identifier))
            self.assertNotIn(
                identifier,
                {item["workspace_id"] for item in registry.list()},
            )
            self.assertTrue(workspace.is_dir(), "forgetting is not deleting")
            self.assertFalse(registry.forget(identifier), "and it is idempotent")

    def test_prune_takes_only_what_is_gone_and_unserved(self) -> None:
        """Offline is not the same as gone, and a live Runtime outranks a stat."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = WorkspaceRegistry(root / "workspaces.json")
            kept, removed, served = (root / name for name in ("kept", "removed", "served"))
            for directory in (kept, removed, served):
                directory.mkdir()
            ids = {
                name: registry.register(directory)[0]
                for name, directory in (
                    ("kept", kept), ("removed", removed), ("served", served),
                )
            }
            shutil.rmtree(removed)
            shutil.rmtree(served)

            forgotten = registry.prune(live={ids["served"]})

            self.assertEqual([ids["removed"]], forgotten)
            remaining = {item["workspace_id"] for item in registry.list()}
            self.assertIn(ids["kept"], remaining, "offline is not gone")
            self.assertIn(ids["served"], remaining, "a Runtime answers for it")
            self.assertNotIn(ids["removed"], remaining)

    def test_the_registry_can_be_pointed_somewhere_else(self) -> None:
        """Registering runs through the CLI, so only an env var can redirect it.

        Without that a test suite writes its throwaway directories into the
        developer's own registry, which is how twenty-four of them got there.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.dict(os.environ, {"ORBIT_HUB_ROOT": str(root)}):
                self.assertEqual(root / "workspaces.json", WorkspaceRegistry().path)
            self.assertNotEqual(root / "workspaces.json", WorkspaceRegistry().path)

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

    def test_explicit_registration_does_not_create_a_misspelled_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "misspelled" / "project"
            registry = WorkspaceRegistry(Path(temporary) / "workspaces.json")

            with self.assertRaisesRegex(HubError, "not an existing directory"):
                registry.register(missing)

            self.assertFalse(missing.exists())

    def test_a_deleted_workspace_stays_registered_but_is_not_selectable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "project"
            workspace.mkdir()
            registry = WorkspaceRegistry(root / "workspaces.json")
            identifier, _ = registry.register(workspace)
            workspace.rmdir()

            with mock.patch.dict(
                "os.environ", {"ORBIT_DEFAULT_WORKSPACE": str(root / "default")},
                clear=False,
            ):
                listed = next(
                    item for item in registry.list()
                    if item["workspace_id"] == identifier
                )
                self.assertFalse(listed["available"])
                with self.assertRaisesRegex(HubError, "workspace is unavailable"):
                    registry.select(name="project")

            self.assertEqual(str(workspace.resolve()), listed["path"])
            with self.assertRaisesRegex(HubError, "workspace is unavailable"):
                registry.resolve(identifier)

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
            (root / "project").mkdir()
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
            (root / "first").mkdir()
            (root / "second").mkdir()
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

    def test_restart_overlap_waits_for_two_runtimes_to_converge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "project"
            workspace.mkdir()
            registry = WorkspaceRegistry(root / "workspaces.json")
            identifier, workspace = registry.register(workspace)
            old = DiscoveredRuntime(root / "old.lock", {
                "project_root": str(workspace), "base_url": "http://127.0.0.1:41001",
            })
            new = DiscoveredRuntime(root / "new.lock", {
                "project_root": str(workspace), "base_url": "http://127.0.0.1:41002",
            })
            observations = iter(((old, new), (old, new), (new,)))
            launches = []
            manager = WorkspaceRuntimeManager(
                registry=registry,
                runtime_discovery=lambda: next(observations),
                health_check=lambda _url: True,
                launcher=lambda path: launches.append(path),
                sleep=lambda _seconds: None,
            )

            self.assertEqual("http://127.0.0.1:41002", manager.ensure(identifier))
            self.assertEqual([], launches)

    def test_startup_timeout_reports_runtime_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "project"
            workspace.mkdir()
            registry = WorkspaceRegistry(root / "workspaces.json")
            identifier, _ = registry.register(workspace)
            now = [0.0]
            manager = WorkspaceRuntimeManager(
                registry=registry, runtime_discovery=lambda: (),
                health_check=lambda _url: False, launcher=lambda _path: None,
                timeout_seconds=0.25, clock=lambda: now[0],
                sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
            )

            with self.assertRaisesRegex(HubError, "did not become ready"):
                manager.ensure(identifier)

    def test_multiple_runtimes_are_reported_only_after_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "project"
            workspace.mkdir()
            registry = WorkspaceRegistry(root / "workspaces.json")
            identifier, workspace = registry.register(workspace)
            runtimes = tuple(
                DiscoveredRuntime(root / f"{index}.lock", {
                    "project_root": str(workspace),
                    "base_url": f"http://127.0.0.1:{41000 + index}",
                })
                for index in (1, 2)
            )
            now = [0.0]
            launches = []
            manager = WorkspaceRuntimeManager(
                registry=registry, runtime_discovery=lambda: runtimes,
                health_check=lambda _url: True,
                launcher=lambda path: launches.append(path),
                timeout_seconds=0.25, clock=lambda: now[0],
                sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
            )

            with self.assertRaisesRegex(
                MultipleRuntimesError, "remained live.*after 0.25s",
            ):
                manager.ensure(identifier)
            self.assertEqual([], launches)


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
        self.assertEqual(
            {"content-type": "application/json"},
            forward.call_args_list[0].args[2],
        )

    def test_explicit_actor_is_forwarded_to_the_internal_backend(self) -> None:
        manager = self.Manager()
        answer = {"result": {"tools": []}}
        with mock.patch(
            "orbit.hub._forward",
            return_value=(200, json.dumps(answer).encode(), "application/json"),
        ) as forward, AsgiHarness(create_hub_app(manager)) as client:
            client.request(
                "POST", "/mcp", headers={"x-orbit-actor": "harness:session:abc"},
                body={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )

        self.assertEqual(
            "harness:session:abc",
            forward.call_args.args[2]["x-orbit-actor"],
        )

    def test_blocking_forwards_have_a_dedicated_capacity_budget(self) -> None:
        app = create_hub_app(self.Manager(), forward_concurrency=24)

        self.assertEqual(24, app.state.forward_limiter.total_tokens)
        with self.assertRaisesRegex(ValueError, "must be positive"):
            create_hub_app(self.Manager(), forward_concurrency=0)

    def test_list_workspaces_reports_unavailable_registrations(self) -> None:
        manager = self.Manager()

        class Registry:
            calls = 0

            def list(self):
                self.calls += 1
                return [{
                    "workspace_id": "missing", "name": "Missing",
                    "path": "/projects/missing", "kind": "registered",
                    "available": False,
                }]

        manager.registry = Registry()
        with AsgiHarness(create_hub_app(manager)) as client:
            response = client.request("POST", "/mcp", body={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "list_workspaces", "arguments": {}},
            })

        payload = response.json()["result"]
        self.assertFalse(payload["structuredContent"]["workspaces"][0]["available"])
        self.assertEqual(1, manager.registry.calls)

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


class HubGlobalControlTests(unittest.TestCase):
    class Registry:
        def list(self):
            return [{
                "workspace_id": "project-a", "name": "Project A",
                "path": "/projects/a", "kind": "registered", "available": True,
            }, {
                "workspace_id": "project-b", "name": "Project B",
                "path": "/projects/b", "kind": "registered", "available": True,
            }]

    class Manager:
        def __init__(self):
            self.registry = HubGlobalControlTests.Registry()

        def ensure(self, identifier):
            self.ensured = identifier
            return "http://127.0.0.1:41001"

        def find_live(self, identifier):
            return "http://127.0.0.1:41001" if identifier == "project-a" else None

    def test_template_is_global_then_published_by_target_runtime(self) -> None:
        source = json.dumps({
            "dsl_version": "1.0",
            "metadata": {"id": "shared", "name": "Shared"},
            "nodes": [], "edges": [],
        })
        manager = self.Manager()
        with tempfile.TemporaryDirectory() as temporary:
            store = WorkflowTemplateStore(Path(temporary) / "templates.json")
            with mock.patch(
                "orbit.hub._runtime_json",
                return_value=(201, {"data": {"workflow_id": "workflow:shared", "version": 1}}),
            ) as runtime, AsgiHarness(create_hub_app(manager, template_store=store)) as client:
                created = client.request("POST", "/api/v1/workflow-templates", body={
                    "name": "Shared", "source": source, "expected_version": 0,
                }, headers={"idempotency-key": "template-1"})
                template_id = created.json()["template_id"]
                listed = client.get("/api/v1/workflow-templates")
                imported = client.request(
                    "POST", f"/api/v1/workflow-templates/{template_id}/instantiate",
                    headers={"idempotency-key": "import-1"},
                    body={"workspace_id": "project-a", "expected_latest_version": 0},
                )

        self.assertEqual(201, created.status_code)
        self.assertEqual(1, len(listed.json()["templates"]))
        self.assertEqual(201, imported.status_code)
        self.assertEqual("project-a", manager.ensured)
        call = runtime.call_args.args[0]
        self.assertIn("/api/v1/workflows/workflow:shared/versions", call)
        self.assertEqual("import-1", runtime.call_args.kwargs["headers"]["idempotency-key"])

    def test_agent_statistics_sum_only_live_workspace_runtimes(self) -> None:
        catalog = {"data": {"handlers": [{
            "name": "agent.codex", "version": "1.2.3",
            "attempt_count": 7, "failed_count": 2,
        }, {"name": "transform", "attempt_count": 99, "failed_count": 99}]}}
        with mock.patch(
            "orbit.hub._runtime_json", return_value=(200, catalog),
        ), AsgiHarness(create_hub_app(self.Manager())) as client:
            response = client.get("/api/v1/global/agent-stats")

        payload = response.json()
        self.assertEqual([{
            "name": "agent.codex", "attempt_count": 7, "failed_count": 2,
            "workspaces": 1, "versions": ["1.2.3"],
        }], payload["agents"])
        self.assertEqual(
            ["online", "offline"],
            [item["runtime"] for item in payload["workspaces"]],
        )
        self.assertEqual("sum_of_workspace_runtime_statistics", payload["semantics"])

    def test_a_malformed_catalog_costs_one_workspace_not_the_endpoint(self) -> None:
        """The per-Workspace guard has to actually cover what a Runtime can say.

        `{"data": null}` and a null tally raise AttributeError and TypeError,
        neither of which the guard caught, so one older or half-booted Runtime
        took the machine-wide statistics down with a 500. And the Workspace was
        appended "online" before the totals were summed, so a failure mid-way
        listed it twice, contradicting itself, over half-added numbers.
        """

        with mock.patch(
            "orbit.hub._runtime_json", return_value=(200, {"data": None}),
        ), AsgiHarness(create_hub_app(self.Manager())) as client:
            response = client.get("/api/v1/global/agent-stats")

        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertEqual([], payload["agents"])
        states = [item["runtime"] for item in payload["workspaces"]]
        self.assertEqual(len(states), len(payload["workspaces"]))
        self.assertNotIn("error", states, "a null `data` is an empty catalog")

    def test_a_workspace_that_fails_mid_sum_contributes_nothing(self) -> None:
        """All of a Workspace's numbers, or none: never half of them."""

        catalog = {"data": {"handlers": [
            {"name": "agent.codex", "attempt_count": 7, "failed_count": 0},
            {"name": "agent.claude", "attempt_count": [], "failed_count": 0},
        ]}}
        with mock.patch(
            "orbit.hub._runtime_json", return_value=(200, catalog),
        ), AsgiHarness(create_hub_app(self.Manager())) as client:
            response = client.get("/api/v1/global/agent-stats")

        payload = response.json()
        self.assertEqual(200, response.status_code, response.text)
        # An unreadable tally is zero, not a refusal and not a partial sum.
        self.assertEqual(
            {"agent.claude": 0, "agent.codex": 7},
            {item["name"]: item["attempt_count"] for item in payload["agents"]},
        )
        self.assertEqual(
            1, sum(1 for item in payload["workspaces"] if item["runtime"] == "online"),
        )

    def test_global_template_writes_require_concurrency_controls(self) -> None:
        source = json.dumps({
            "dsl_version": "1.0",
            "metadata": {"id": "shared", "name": "Shared"},
            "nodes": [], "edges": [],
        })
        with tempfile.TemporaryDirectory() as temporary, AsgiHarness(create_hub_app(
            self.Manager(), template_store=WorkflowTemplateStore(
                Path(temporary) / "templates.json"
            ),
        )) as client:
            missing_key = client.request(
                "POST", "/api/v1/workflow-templates",
                body={"name": "Shared", "source": source, "expected_version": 0},
            )
            missing_version = client.request(
                "POST", "/api/v1/workflow-templates",
                headers={"idempotency-key": "template-1"},
                body={"name": "Shared", "source": source},
            )

        self.assertEqual(400, missing_key.status_code)
        self.assertIn("idempotency-key", missing_key.text)
        self.assertEqual(400, missing_version.status_code)
        self.assertIn("expected_version", missing_version.text)

    def test_corrupt_global_template_catalog_is_unavailable_not_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "templates.json"
            path.write_text("{broken", encoding="utf-8")
            with AsgiHarness(create_hub_app(
                self.Manager(), template_store=WorkflowTemplateStore(path),
            )) as client:
                read = client.get("/api/v1/workflow-templates")
                write = client.request(
                    "POST", "/api/v1/workflow-templates",
                    headers={"idempotency-key": "new"},
                    body={
                        "name": "x", "expected_version": 0,
                        "source": json.dumps({
                            "dsl_version": "1.0",
                            "metadata": {"id": "x", "name": "x"},
                            "nodes": [], "edges": [],
                        }),
                    },
                )

            self.assertEqual(503, read.status_code)
            self.assertEqual(503, write.status_code)
            self.assertEqual("{broken", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
