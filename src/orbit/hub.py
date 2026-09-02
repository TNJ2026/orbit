"""Stable loopback entry point for workspace-scoped Orbit Runtimes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request as UrlRequest, urlopen

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect as websocket_connect

from .agent_apps.host import default_workspace
from .global_control import WorkflowTemplateError, WorkflowTemplateStore
from .platform.projects import project_id, resolve_project_root
from .platform.runtime_ownership import DiscoveredRuntime, discover_runtimes
from .web.mcp import (
    INVALID_PARAMS, INVALID_REQUEST, METHOD_NOT_FOUND, PARSE_ERROR,
    PROTOCOL_VERSION, SERVER_INFO, McpSessionRegistry,
)
from .web.mcp_app import ORBIT_DASHBOARD_MIME_TYPE, ORBIT_MCP_APP_RESOURCES


DEFAULT_HUB_ROOT = Path.home() / ".orbit" / "hub"


class HubError(RuntimeError):
    pass


class MultipleRuntimesError(HubError):
    pass


class WorkspaceRegistry:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or DEFAULT_HUB_ROOT / "workspaces.json").expanduser()
        self._lock = threading.Lock()

    def register(
        self, workspace: Path | str, *, create: bool = False,
    ) -> tuple[str, Path]:
        requested = Path(workspace).expanduser().resolve()
        if create:
            requested.mkdir(parents=True, exist_ok=True)
        elif not requested.is_dir():
            raise HubError(f"Orbit workspace is not an existing directory: {requested}")
        root = resolve_project_root(requested)
        identifier = project_id(root)
        with self._lock:
            entries = self._read()
            entries[identifier] = str(root)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps({"workspaces": entries}, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.path)
        return identifier, root

    def resolve(self, identifier: str | None) -> Path:
        if identifier is None or identifier == "default":
            _, root = self.register(default_workspace(), create=True)
            return root
        value = self._read().get(identifier)
        if value is None:
            raise HubError(f"unknown Orbit workspace: {identifier}")
        root = Path(value).expanduser().resolve()
        if not root.is_dir():
            raise HubError(f"Orbit workspace is unavailable: {identifier}")
        return root

    def _read(self) -> dict[str, str]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        values = payload.get("workspaces", {}) if isinstance(payload, dict) else {}
        return {
            str(key): str(value) for key, value in values.items()
            if isinstance(key, str) and isinstance(value, str)
        }

    def list(self) -> list[dict[str, Any]]:
        default_id, default_root = self.register(default_workspace(), create=True)
        entries = self._read()
        return [
            {
                "workspace_id": identifier,
                "name": Path(value).name,
                "path": value,
                "kind": "default" if identifier == default_id else "registered",
                # Registration is durable configuration, not proof that a
                # removable volume, checkout or directory still exists. Keep
                # the entry so it can become available again, but never present
                # it as a usable selection without saying which state it is in.
                "available": Path(value).expanduser().is_dir(),
            }
            for identifier, value in sorted(entries.items(), key=lambda item: item[1])
        ]

    def select(self, *, workspace_id: str | None = None, path: str | None = None, name: str | None = None) -> str:
        if path:
            if not Path(path).expanduser().is_absolute():
                raise HubError("workspace path must be absolute")
            return self.register(path)[0]
        entries = self.list()
        if workspace_id:
            self.resolve(workspace_id)
            return workspace_id
        if name:
            matches = [item for item in entries if item["name"] == name]
            if len(matches) != 1:
                raise HubError(
                    f"workspace name must match exactly one registered workspace: {name}"
                )
            identifier = str(matches[0]["workspace_id"])
            self.resolve(identifier)
            return identifier
        raise HubError("workspace_id, path, or name is required")


def workspace_urls(identifier: str, hub_url: str = "http://127.0.0.1:8848") -> dict[str, str]:
    base = hub_url.rstrip("/") + f"/workspaces/{identifier}"
    events = base.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    return {
        "workspace_id": identifier,
        "mcp_url": f"{base}/mcp",
        "ui_url": f"{base}/ui/",
        "events_url": f"{events}/events",
    }


class WorkspaceRuntimeManager:
    def __init__(
        self,
        *,
        registry: WorkspaceRegistry | None = None,
        runtime_discovery: Callable[[], Iterable[DiscoveredRuntime]] = discover_runtimes,
        launcher: Callable[[Path], subprocess.Popen] | None = None,
        health_check: Callable[[str], bool] | None = None,
        timeout_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        log_root: Path | str | None = None,
    ) -> None:
        self.registry = registry or WorkspaceRegistry()
        self.runtime_discovery = runtime_discovery
        self.launcher = launcher or self._launch
        self.health_check = health_check or self._healthy
        self.timeout_seconds = timeout_seconds
        self.clock = clock
        self.sleep = sleep
        self.log_root = Path(log_root or DEFAULT_HUB_ROOT / "runtimes").expanduser()
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def ensure(self, identifier: str | None = None) -> str:
        workspace = self.registry.resolve(identifier)
        key = str(workspace)
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            deadline = self.clock() + self.timeout_seconds
            multiple = False
            try:
                found = self._find(workspace)
            except MultipleRuntimesError:
                # A predecessor can remain healthy while its successor becomes
                # ready during graceful shutdown. Do not launch a third Runtime;
                # let the overlap converge inside the ordinary readiness window.
                found, multiple = None, True
            if found is not None:
                return found
            if not multiple:
                self.launcher(workspace)
            while self.clock() < deadline:
                try:
                    found = self._find(workspace)
                    multiple = False
                except MultipleRuntimesError:
                    found, multiple = None, True
                if found is not None:
                    return found
                self.sleep(0.1)
        if multiple:
            raise MultipleRuntimesError(
                f"multiple Orbit Runtimes remained live for {workspace} "
                f"after {self.timeout_seconds:g}s"
            )
        raise HubError(f"Orbit Runtime did not become ready for {workspace}")

    def _find(self, workspace: Path) -> str | None:
        expected = str(workspace.resolve())
        matches = []
        for runtime in self.runtime_discovery():
            try:
                actual = str(Path(str(runtime.facts.get("project_root"))).expanduser().resolve())
            except (OSError, TypeError, ValueError):
                continue
            if actual == expected and runtime.base_url and self.health_check(runtime.base_url):
                matches.append(runtime.base_url.rstrip("/"))
        if len(matches) > 1:
            raise MultipleRuntimesError(
                f"multiple Orbit Runtimes are live for {workspace}"
            )
        return matches[0] if matches else None

    def find_live(self, identifier: str) -> str | None:
        """Return an already-live Runtime without starting an offline Workspace."""
        return self._find(self.registry.resolve(identifier))

    def _launch(self, workspace: Path) -> subprocess.Popen:
        identifier = project_id(workspace)
        directory = self.log_root / identifier
        directory.mkdir(parents=True, exist_ok=True)
        stdout = (directory / "stdout.log").open("ab")
        stderr = (directory / "stderr.log").open("ab")
        try:
            return subprocess.Popen(
                [sys.executable, "-m", "orbit", "serve", "--host", "127.0.0.1",
                 "--port", "0", "--project-root", str(workspace)],
                cwd=workspace,
                env={**os.environ, "ORBIT_HUB_CHILD": "1"},
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                close_fds=True,
                start_new_session=os.name != "nt",
            )
        finally:
            stdout.close()
            stderr.close()

    @staticmethod
    def _healthy(base_url: str) -> bool:
        try:
            with urlopen(f"{base_url.rstrip('/')}/health/ready", timeout=0.5) as response:
                return 200 <= response.status < 300
        except (OSError, URLError, ValueError):
            return False


def _forward(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, bytes, str]:
    request = UrlRequest(url, data=body, method="POST", headers=headers)
    try:
        with urlopen(request, timeout=330) as response:
            return response.status, response.read(), response.headers.get("content-type", "application/json")
    except HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("content-type", "application/json")
    except (OSError, URLError) as exc:
        raise HubError(f"Orbit Runtime is unavailable: {exc}") from exc


def _runtime_json(
    url: str, *, method: str = "GET", body: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None, timeout: float = 10,
) -> tuple[int, Mapping[str, Any]]:
    encoded = None if body is None else json.dumps(body).encode("utf-8")
    request = UrlRequest(url, data=encoded, method=method, headers={
        "accept": "application/json", **({} if encoded is None else {
            "content-type": "application/json",
        }), **dict(headers or {}),
    })
    try:
        with urlopen(request, timeout=timeout) as response:
            status, payload = response.status, response.read()
    except HTTPError as exc:
        status, payload = exc.code, exc.read()
    except (OSError, URLError) as exc:
        raise HubError(f"Orbit Runtime is unavailable: {exc}") from exc
    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HubError("Orbit Runtime returned invalid JSON") from exc
    return status, decoded if isinstance(decoded, Mapping) else {}


def create_hub_app(
    manager: WorkspaceRuntimeManager | None = None, *, forward_concurrency: int = 24,
    template_store: WorkflowTemplateStore | None = None,
) -> Starlette:
    if forward_concurrency < 1:
        raise ValueError("forward concurrency must be positive")
    runtimes = manager or WorkspaceRuntimeManager()
    templates = template_store or WorkflowTemplateStore()
    sessions = McpSessionRegistry()
    selections: dict[str, str] = {}
    selection_lock = threading.Lock()
    # A stuck Runtime must not consume AnyIO's entire default worker pool (40
    # threads at present) and prevent health checks or other workspaces from
    # making progress. Waiting callers remain async tasks, not worker threads.
    forward_limiter = anyio.CapacityLimiter(forward_concurrency)

    def result(request_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": payload}

    def failure(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message},
        }

    async def workspace_tools(
        identifier: str | None, envelope: Mapping[str, Any], actor: str | None,
    ) -> Mapping[str, Any]:
        base = await anyio.to_thread.run_sync(runtimes.ensure, identifier)
        headers = {"content-type": "application/json"}
        if actor is not None:
            headers["x-orbit-actor"] = actor
        status, payload, _ = await anyio.to_thread.run_sync(
            _forward,
            f"{base}/internal/v1/agent-tools",
            json.dumps(envelope).encode("utf-8"),
            headers,
            limiter=forward_limiter,
        )
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise HubError("Orbit Runtime returned an invalid tool response") from exc
        if status >= 400 or not isinstance(decoded, Mapping):
            message = decoded.get("error") if isinstance(decoded, Mapping) else None
            raise HubError(str(message or f"Orbit Runtime tool backend failed ({status})"))
        return decoded

    async def dispatch_gateway(
        message: Mapping[str, Any], identifier: str | None, actor: str,
        session_id: str | None, forwarded_actor: str | None,
    ) -> dict[str, Any] | None:
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if not isinstance(params, Mapping):
            return failure(request_id, INVALID_PARAMS, "params must be an object")
        sessions.observe(actor, str(method or ""), params)

        if method == "initialize":
            return result(request_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"listChanged": False},
                },
                "serverInfo": SERVER_INFO,
            })
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if method == "ping":
            return result(request_id, {})
        if method == "resources/list":
            return result(request_id, {"resources": [
                {
                    "uri": resource["uri"],
                    "name": resource["name"],
                    "description": resource["description"],
                    "mimeType": ORBIT_DASHBOARD_MIME_TYPE,
                    "_meta": {
                        "ui": {"prefersBorder": resource["prefers_border"]},
                        "openai/widgetPrefersBorder": resource["prefers_border"],
                    },
                }
                for resource in ORBIT_MCP_APP_RESOURCES
            ]})
        if method == "resources/templates/list":
            return result(request_id, {"resourceTemplates": []})
        if method == "resources/read":
            resource = next(
                (item for item in ORBIT_MCP_APP_RESOURCES if item["uri"] == params.get("uri")),
                None,
            )
            if resource is None:
                return failure(request_id, INVALID_PARAMS, "unknown resource")
            return result(request_id, {"contents": [{
                "uri": resource["uri"],
                "mimeType": ORBIT_DASHBOARD_MIME_TYPE,
                "text": resource["html"],
                "_meta": {
                    "ui": {"prefersBorder": resource["prefers_border"]},
                    "openai/widgetPrefersBorder": resource["prefers_border"],
                },
            }]})
        if method == "tools/list":
            backend = await workspace_tools(
                identifier, {"operation": "list"}, forwarded_actor,
            )
            tools = list(backend.get("result", {}).get("tools", ()))
            tools.extend((
                {
                    "name": "list_workspaces",
                    "description": "List Orbit workspaces registered with the local Gateway.",
                    "inputSchema": {"type": "object", "properties": {}},
                    "outputSchema": {"type": "object"},
                },
                {
                    "name": "select_workspace",
                    "description": "Select this MCP session's workspace by readable name or absolute path.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "path": {"type": "string"},
                        },
                    },
                    "outputSchema": {"type": "object"},
                },
            ))
            return result(request_id, {"tools": tools})
        elif method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments") or {}
            if name == "list_workspaces":
                workspaces = runtimes.registry.list()
                return result(request_id, {
                    "content": [{"type": "text", "text": json.dumps({"workspaces": workspaces})}],
                    "structuredContent": {"workspaces": workspaces},
                    "isError": False,
                })
            if name == "select_workspace":
                if not session_id:
                    return failure(request_id, INVALID_PARAMS, "MCP session id is required")
                try:
                    selected = runtimes.registry.select(
                        path=arguments.get("path"), name=arguments.get("name"),
                    )
                except HubError as exc:
                    return result(request_id, {
                        "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
                        "structuredContent": {"error": str(exc)}, "isError": True,
                    })
                with selection_lock:
                    selections[session_id] = selected
                payload = next(
                    item for item in runtimes.registry.list()
                    if item["workspace_id"] == selected
                )
                return result(request_id, {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "structuredContent": payload, "isError": False,
                })
            backend = await workspace_tools(identifier, {
                "operation": "call",
                "name": name,
                "arguments": arguments,
            }, forwarded_actor)
        else:
            return failure(request_id, METHOD_NOT_FOUND, f"unknown method {method}")
        if "protocol_error" in backend:
            return {"jsonrpc": "2.0", "id": request_id, "error": backend["protocol_error"]}
        return result(request_id, backend.get("result", {}))

    async def ready(_request: Request) -> Response:
        return JSONResponse({"status": "ready", "service": "orbit-hub"})

    async def template_catalog(request: Request) -> Response:
        if request.method == "GET":
            return JSONResponse({"templates": templates.list()})
        try:
            body = await request.json()
            idempotency_key = request.headers.get("idempotency-key", "").strip()
            if not idempotency_key:
                raise WorkflowTemplateError("idempotency-key header is required")
            item = templates.put(
                name=str(body.get("name", "")), source=str(body.get("source", "")),
                source_format=str(body.get("source_format", "json")),
                expected_version=body.get("expected_version"),
                idempotency_key=idempotency_key,
            )
        except (ValueError, WorkflowTemplateError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(item, status_code=201)

    async def template_item(request: Request) -> Response:
        template_id = request.path_params["template_id"]
        if request.method == "DELETE":
            try:
                body = await request.json()
                idempotency_key = request.headers.get("idempotency-key", "").strip()
                if not idempotency_key:
                    raise WorkflowTemplateError("idempotency-key header is required")
                expected_version = body.get("expected_version")
                if not isinstance(expected_version, int) or isinstance(expected_version, bool):
                    raise WorkflowTemplateError("expected_version must be an integer")
                deleted = templates.delete(
                    template_id, expected_version=expected_version,
                    idempotency_key=idempotency_key,
                )
            except (ValueError, WorkflowTemplateError) as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            return JSONResponse({"template_id": template_id, "deleted": deleted})
        try:
            return JSONResponse(templates.get(template_id))
        except WorkflowTemplateError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    async def instantiate_template(request: Request) -> Response:
        template_id = request.path_params["template_id"]
        try:
            body = await request.json()
            idempotency_key = request.headers.get("idempotency-key", "").strip()
            if not idempotency_key:
                raise WorkflowTemplateError("idempotency-key header is required")
            identifier = str(body.get("workspace_id", ""))
            expected = body.get("expected_latest_version")
            if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
                raise WorkflowTemplateError(
                    "expected_latest_version must be a non-negative integer"
                )
            item = templates.get(template_id)
            base = await anyio.to_thread.run_sync(runtimes.ensure, identifier)
            status, result_payload = await anyio.to_thread.run_sync(
                lambda: _runtime_json(
                    f"{base}/api/v1/workflows/{quote(item['workflow_id'], safe=':')}/versions",
                    method="POST",
                    body={"source": item["source"], "expected_version": expected},
                    headers={
                        "x-orbit-actor": "local",
                        "idempotency-key": idempotency_key,
                    }, timeout=60,
                )
            )
        except (ValueError, WorkflowTemplateError, HubError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({
            "template_id": template_id, "workspace_id": identifier,
            "runtime_response": result_payload,
        }, status_code=status)

    async def global_agent_stats(_request: Request) -> Response:
        totals: dict[str, dict[str, Any]] = {}
        workspaces: list[dict[str, Any]] = []
        for workspace in runtimes.registry.list():
            if not workspace.get("available", True):
                workspaces.append({**workspace, "runtime": "unavailable"})
                continue
            identifier = str(workspace["workspace_id"])
            try:
                base = await anyio.to_thread.run_sync(runtimes.find_live, identifier)
                if base is None:
                    workspaces.append({**workspace, "runtime": "offline"})
                    continue
                status, payload = await anyio.to_thread.run_sync(
                    lambda: _runtime_json(
                        f"{base}/api/v1/handler-catalog",
                        headers={"x-orbit-actor": "local"},
                    )
                )
                if status >= 400:
                    raise HubError(f"handler catalog failed ({status})")
                handlers = payload.get("data", {}).get("handlers", ())
                agents = [
                    item for item in handlers
                    if isinstance(item, Mapping) and str(item.get("name", "")).startswith("agent.")
                ]
                workspaces.append({**workspace, "runtime": "online", "agent_count": len(agents)})
                for item in agents:
                    name = str(item["name"])
                    total = totals.setdefault(name, {
                        "name": name, "attempt_count": 0, "failed_count": 0,
                        "workspaces": 0, "versions": [],
                    })
                    total["attempt_count"] += int(item.get("attempt_count", 0))
                    total["failed_count"] += int(item.get("failed_count", 0))
                    total["workspaces"] += 1
                    version = item.get("version")
                    if isinstance(version, str) and version not in total["versions"]:
                        total["versions"].append(version)
            except (HubError, OSError, ValueError) as exc:
                workspaces.append({**workspace, "runtime": "error", "error": str(exc)})
        return JSONResponse({
            "agents": sorted(totals.values(), key=lambda item: item["name"]),
            "workspaces": workspaces,
            "semantics": "sum_of_workspace_runtime_statistics",
        })

    async def mcp(request: Request) -> Response:
        explicit_identifier = request.path_params.get("workspace_id")
        session_id = request.headers.get("mcp-session-id")
        with selection_lock:
            identifier = explicit_identifier or selections.get(session_id or "")
        try:
            message = json.loads(await request.body() or b"")
        except json.JSONDecodeError:
            return JSONResponse(failure(None, PARSE_ERROR, "request body must be JSON"))
        forwarded_actor = request.headers.get("x-orbit-actor")
        actor = forwarded_actor or "local"
        try:
            if isinstance(message, list):
                responses = []
                for item in message:
                    if isinstance(item, Mapping):
                        response = await dispatch_gateway(
                            item, identifier, actor, session_id, forwarded_actor,
                        )
                        if response is not None:
                            responses.append(response)
                return JSONResponse(responses) if responses else JSONResponse(None, status_code=202)
            if not isinstance(message, Mapping) or message.get("jsonrpc") != "2.0":
                return JSONResponse(failure(None, INVALID_REQUEST, "expected a JSON-RPC 2.0 message"))
            response = await dispatch_gateway(
                message, identifier, actor, session_id, forwarded_actor,
            )
            if message.get("method") == "initialize" and not session_id:
                session_id = uuid.uuid4().hex
            headers = {} if not session_id else {"mcp-session-id": session_id}
            return (
                JSONResponse(None, status_code=202, headers=headers)
                if response is None else JSONResponse(response, headers=headers)
            )
        except HubError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)

    async def ui(request: Request) -> Response:
        identifier = request.path_params.get("workspace_id")
        tail = request.path_params.get("path", "")
        try:
            base = await anyio.to_thread.run_sync(runtimes.ensure, identifier)
        except HubError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        target = f"{base}/ui/{tail}"
        if request.url.query:
            target += f"?{request.url.query}"
        return RedirectResponse(target, status_code=307)

    async def events(websocket: WebSocket) -> None:
        identifier = websocket.path_params.get("workspace_id")
        try:
            base = await anyio.to_thread.run_sync(runtimes.ensure, identifier)
            upstream_url = base.replace("http://", "ws://", 1).replace(
                "https://", "wss://", 1,
            ) + "/events"
            if websocket.url.query:
                upstream_url += f"?{websocket.url.query}"
            async with websocket_connect(upstream_url) as upstream:
                await websocket.accept()

                async with anyio.create_task_group() as tasks:
                    async def client_to_runtime() -> None:
                        try:
                            while True:
                                message = await websocket.receive()
                                if message["type"] == "websocket.disconnect":
                                    return
                                if message.get("text") is not None:
                                    await upstream.send(message["text"])
                                elif message.get("bytes") is not None:
                                    await upstream.send(message["bytes"])
                        finally:
                            tasks.cancel_scope.cancel()

                    async def runtime_to_client() -> None:
                        try:
                            async for message in upstream:
                                if isinstance(message, bytes):
                                    await websocket.send_bytes(message)
                                else:
                                    await websocket.send_text(message)
                        finally:
                            tasks.cancel_scope.cancel()

                    tasks.start_soon(client_to_runtime)
                    tasks.start_soon(runtime_to_client)
        except (HubError, OSError, WebSocketDisconnect):
            try:
                await websocket.close(code=1011)
            except RuntimeError:
                pass

    app = Starlette(routes=[
        Route("/health/ready", ready, methods=["GET"]),
        Route("/api/v1/global/agent-stats", global_agent_stats, methods=["GET"]),
        Route("/api/v1/workflow-templates", template_catalog, methods=["GET", "POST"]),
        Route("/api/v1/workflow-templates/{template_id}", template_item, methods=["GET", "DELETE"]),
        Route("/api/v1/workflow-templates/{template_id}/instantiate", instantiate_template, methods=["POST"]),
        Route("/mcp", mcp, methods=["POST"]),
        Route("/workspaces/{workspace_id}/mcp", mcp, methods=["POST"]),
        Route("/ui", ui, methods=["GET"]),
        Route("/ui/{path:path}", ui, methods=["GET"]),
        Route("/workspaces/{workspace_id}/ui", ui, methods=["GET"]),
        Route("/workspaces/{workspace_id}/ui/{path:path}", ui, methods=["GET"]),
        WebSocketRoute("/events", events),
        WebSocketRoute("/workspaces/{workspace_id}/events", events),
    ])
    app.state.forward_limiter = forward_limiter
    return app
