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
from typing import Any, Callable, Container, Iterable, Mapping
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
from .global_control import (
    WorkflowTemplateError, WorkflowTemplateStorageError, WorkflowTemplateStore,
)
from .platform.projects import project_id, resolve_project_root
from .platform.runtime_ownership import DiscoveredRuntime, discover_runtimes
from .web.mcp import (
    INVALID_PARAMS, INVALID_REQUEST, METHOD_NOT_FOUND, PARSE_ERROR,
    PROTOCOL_VERSION, SERVER_INFO, SESSION_RECOVERY_INSTRUCTIONS,
    McpSessionRegistry,
)
from .web.mcp_app import ORBIT_DASHBOARD_MIME_TYPE, ORBIT_MCP_APP_RESOURCES


def default_hub_root() -> Path:
    """Where the Hub keeps the workspace registry and its Runtime logs.

    Overridable, and read on each call rather than at import, because the
    thing that most needs to move it is a test: registering a workspace goes
    through the `orbit` CLI, so a suite that cannot redirect this writes its
    throwaway directories into the developer's real registry and leaves them
    there. Every e2e run added one, and none was ever removed.
    """

    configured = os.environ.get("ORBIT_HUB_ROOT")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".orbit" / "hub"


class HubError(RuntimeError):
    pass


class MultipleRuntimesError(HubError):
    pass


class WorkspaceRegistry:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or default_hub_root() / "workspaces.json").expanduser()
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
            self._save(entries)
        return identifier, root

    def _save(self, entries: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps({"workspaces": entries}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

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

    def forget(self, identifier: str) -> bool:
        """Drop one registration. The Workspace and its Runtime are untouched.

        Registering is how a Workspace becomes routable; nothing was ever the
        reverse of it, so a directory that was opened once stayed in the list
        for good — and a test suite that opens a throwaway directory per run
        added one every time.
        """

        with self._lock:
            entries = self._read()
            if identifier not in entries:
                return False
            del entries[identifier]
            self._save(entries)
        return True

    def prune(self, *, live: Container[str] = ()) -> list[str]:
        """Forget the registrations whose directory is no longer there.

        Only those. An entry that is merely offline is a Workspace nobody has
        opened lately, which is not the same as one that is gone, and dropping
        it would lose a routing id somebody may still hold. `live` names the
        Workspaces a Runtime is currently serving; those are spared whatever
        the filesystem says, because a Runtime answering for a directory is
        better evidence than a `stat` of it.
        """

        with self._lock:
            entries = self._read()
            doomed = [
                identifier for identifier, value in entries.items()
                if identifier not in live
                and not Path(value).expanduser().is_dir()
            ]
            if not doomed:
                return []
            for identifier in doomed:
                del entries[identifier]
            self._save(entries)
        return sorted(doomed)

    def select(self, *, workspace_id: str | None = None, path: str | None = None, name: str | None = None) -> str:
        if path:
            if not Path(path).expanduser().is_absolute():
                raise HubError("workspace path must be absolute")
            return self.register(path)[0]
        if workspace_id:
            self.resolve(workspace_id)
            return workspace_id
        if name:
            # Only this branch reads the listing, and the listing stats every
            # registered path. Computing it first meant selecting by id paid
            # for a sweep it never looked at.
            matches = [item for item in self.list() if item["name"] == name]
            if len(matches) != 1:
                raise HubError(
                    f"workspace name must match exactly one registered workspace: {name}"
                )
            identifier = str(matches[0]["workspace_id"])
            self.resolve(identifier)
            return identifier
        raise HubError("workspace_id, path, or name is required")


class ProjectAccessGrants:
    """Which Workspaces an operator has agreed to hand real project files.

    `orbit serve --agent-project-access` is the switch that decides this, and
    a Runtime the Hub starts is never typed by anybody: `_serve_arguments`
    writes its whole argv. So a workflow node declaring a `workspace_access`
    policy was refused on every Runtime the Hub had ever launched — the
    capability existed and the ordinary way of starting Orbit could not reach
    it. Consent is recorded here instead, and turned into that switch at
    launch.

    Per Workspace, because it is a decision about one project rather than
    about Orbit, and durable, because the Hub relaunches Runtimes without
    asking again. Kept apart from `workspaces.json`: that file is routing and
    is rewritten by every `hub register`, and permission that a routine
    re-registration could silently drop is not permission.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(
            path or default_hub_root() / "project-access.json"
        ).expanduser()
        self._lock = threading.Lock()

    def mode(self, identifier: str) -> str | None:
        return self._read().get(identifier)

    def granted(self, identifier: str) -> bool:
        return self.mode(identifier) is not None

    def set(self, identifier: str, *, allowed: bool) -> None:
        with self._lock:
            entries = self._read()
            if allowed:
                entries[identifier] = "read_write"
            else:
                entries.pop(identifier, None)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps({"project_access": entries}, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.path)

    def _read(self) -> dict[str, str]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        values = payload.get("project_access", {}) if isinstance(payload, dict) else {}
        if not isinstance(values, dict):
            return {}
        result: dict[str, str] = {}
        for key, value in values.items():
            if not isinstance(key, str):
                continue
            # Old boolean/legacy-read records are not consent to the current
            # non-git direct-write contract. The operator must re-authorize.
            if value == "read_write":
                result[key] = value
        return result


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
        grants: ProjectAccessGrants | None = None,
    ) -> None:
        self.registry = registry or WorkspaceRegistry()
        self.grants = grants or ProjectAccessGrants()
        self.runtime_discovery = runtime_discovery
        self.launcher = launcher or self._launch
        self.health_check = health_check or self._healthy
        self.timeout_seconds = timeout_seconds
        self.clock = clock
        self.sleep = sleep
        self.log_root = Path(log_root or default_hub_root() / "runtimes").expanduser()
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

    def serving(self, workspace: Path | str) -> str | None:
        """A live Runtime for this directory, asked without resolving a registration.

        `find_live` goes through `resolve`, which refuses a Workspace whose
        directory is gone — and that is exactly the entry a prune has to ask
        about, since a Runtime may still be serving a directory somebody
        deleted underneath it.
        """

        return self._find(Path(workspace).expanduser())

    def _serve_arguments(self, workspace: Path) -> list[str]:
        """The whole argv a workspace Runtime is started with.

        Its own method because it is the only place an operator's consent can
        reach a Runtime the Hub launches. Nobody is at a prompt to type
        `--agent-project-access`, so a Workspace it was granted for carries
        it here or the grant may as well not exist.
        """

        arguments = [
            sys.executable, "-m", "orbit", "serve", "--host", "127.0.0.1",
            "--port", "0", "--project-root", str(workspace),
        ]
        grant_mode = self.grants.mode(project_id(workspace))
        if grant_mode == "read_write":
            arguments.append("--agent-project-access")
        return arguments

    def _launch(self, workspace: Path) -> subprocess.Popen:
        identifier = project_id(workspace)
        directory = self.log_root / identifier
        directory.mkdir(parents=True, exist_ok=True)
        stdout = (directory / "stdout.log").open("ab")
        stderr = (directory / "stderr.log").open("ab")
        try:
            return subprocess.Popen(
                self._serve_arguments(workspace),
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


def _agent_rows(payload: Any) -> list[Mapping[str, Any]]:
    """The Agent handlers in a Runtime's catalog answer, or a typed refusal.

    Written out rather than chained through `.get(...)` defaults because the
    defaults do not hold: `{"data": null}` from an older or half-booted Runtime
    makes `.get("handlers")` an AttributeError, which is not in any caller's
    except clause and took the whole endpoint down with it.
    """

    data = payload.get("data") if isinstance(payload, Mapping) else None
    handlers = data.get("handlers") if isinstance(data, Mapping) else None
    if handlers is None:
        return []
    if not isinstance(handlers, (list, tuple)):
        raise HubError("handler catalog did not return a list of handlers")
    return [
        item for item in handlers
        if isinstance(item, Mapping) and str(item.get("name", "")).startswith("agent.")
    ]


def _count(value: Any) -> int:
    """A tally from another process, or zero. Never a TypeError."""

    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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
                "instructions": SESSION_RECOVERY_INSTRUCTIONS,
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
                # `list` stats every registered path to say whether it is still
                # there. One entry on an unresponsive mount would otherwise
                # stall every request this process is routing, not just this
                # one — the same reason `ensure` and `_runtime_json` are here.
                workspaces = await anyio.to_thread.run_sync(runtimes.registry.list)
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
                listed = await anyio.to_thread.run_sync(runtimes.registry.list)
                payload = next(
                    item for item in listed if item["workspace_id"] == selected
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

    # The template store takes a machine-wide file lock, and a lock with no
    # timeout is not something to hold the event loop on: a second Hub, an
    # `orbit hub register`, or a home directory on a network mount would park
    # every other request this process is routing — `/mcp`, `/health/ready`,
    # every workspace proxy — behind one JSON file. Same treatment as
    # `runtimes.ensure` and `_runtime_json` below.
    async def template_catalog(request: Request) -> Response:
        if request.method == "GET":
            try:
                listed = await anyio.to_thread.run_sync(templates.list)
                return JSONResponse({"templates": listed})
            except WorkflowTemplateStorageError as exc:
                return JSONResponse({"error": str(exc)}, status_code=503)
        try:
            body = await request.json()
            idempotency_key = request.headers.get("idempotency-key", "").strip()
            if not idempotency_key:
                raise WorkflowTemplateError("idempotency-key header is required")
            item = await anyio.to_thread.run_sync(lambda: templates.put(
                name=str(body.get("name", "")), source=str(body.get("source", "")),
                source_format=str(body.get("source_format", "json")),
                expected_version=body.get("expected_version"),
                idempotency_key=idempotency_key,
            ))
        except WorkflowTemplateStorageError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
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
                deleted = await anyio.to_thread.run_sync(lambda: templates.delete(
                    template_id, expected_version=expected_version,
                    idempotency_key=idempotency_key,
                ))
            except WorkflowTemplateStorageError as exc:
                return JSONResponse({"error": str(exc)}, status_code=503)
            except (ValueError, WorkflowTemplateError) as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            return JSONResponse({"template_id": template_id, "deleted": deleted})
        try:
            return JSONResponse(
                await anyio.to_thread.run_sync(templates.get, template_id)
            )
        except WorkflowTemplateStorageError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
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
            item = await anyio.to_thread.run_sync(templates.get, template_id)
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
        except WorkflowTemplateStorageError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        except (ValueError, WorkflowTemplateError, HubError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({
            "template_id": template_id, "workspace_id": identifier,
            "runtime_response": result_payload,
        }, status_code=status)

    async def global_agent_stats(_request: Request) -> Response:
        totals: dict[str, dict[str, Any]] = {}
        workspaces: list[dict[str, Any]] = []
        for workspace in await anyio.to_thread.run_sync(runtimes.registry.list):
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
                agents = _agent_rows(payload)
                # Summed into a local first. A failure part-way through used to
                # leave its share already added to `totals` and the Workspace
                # listed twice — once "online" from before the loop, once
                # "error" from the handler — so the machine-wide numbers were
                # wrong rather than merely short of one Workspace.
                contribution: dict[str, dict[str, Any]] = {}
                for item in agents:
                    name = str(item.get("name", ""))
                    entry = contribution.setdefault(name, {
                        "attempt_count": 0, "failed_count": 0, "versions": [],
                    })
                    entry["attempt_count"] += _count(item.get("attempt_count"))
                    entry["failed_count"] += _count(item.get("failed_count"))
                    version = item.get("version")
                    if isinstance(version, str) and version not in entry["versions"]:
                        entry["versions"].append(version)
            except (HubError, OSError, TypeError, ValueError) as exc:
                # `TypeError` too: a Runtime that answers `{"data": null}` or a
                # null count is a Runtime with a problem, not a reason to lose
                # the statistics of every other Workspace to a 500.
                workspaces.append({**workspace, "runtime": "error", "error": str(exc)})
                continue
            workspaces.append({**workspace, "runtime": "online", "agent_count": len(agents)})
            for name, entry in contribution.items():
                total = totals.setdefault(name, {
                    "name": name, "attempt_count": 0, "failed_count": 0,
                    "workspaces": 0, "versions": [],
                })
                total["attempt_count"] += entry["attempt_count"]
                total["failed_count"] += entry["failed_count"]
                total["workspaces"] += 1
                for version in entry["versions"]:
                    if version not in total["versions"]:
                        total["versions"].append(version)
        return JSONResponse({
            "agents": sorted(totals.values(), key=lambda item: item["name"]),
            "workspaces": workspaces,
            "semantics": "sum_of_workspace_runtime_statistics",
        })

    async def background_delegations(request: Request) -> Response:
        """Route the one machine worker without exposing Runtime ports to it."""
        if request.client is None or request.client.host not in {
            "127.0.0.1", "::1", "localhost", "testclient",
        }:
            return JSONResponse({"error": "loopback access required"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "request body must be JSON"}, status_code=400)
        if not isinstance(body, Mapping):
            return JSONResponse({"error": "expected an object"}, status_code=400)
        operation = request.path_params["operation"]
        identifier = str(body.get("workspace_id", ""))
        candidates: list[tuple[str, str]]
        if operation == "claim":
            candidates = [
                (str(item["workspace_id"]), str(item["path"]))
                for item in await anyio.to_thread.run_sync(runtimes.registry.list)
                if item.get("available", True)
            ]
        elif identifier:
            try:
                resolved = await anyio.to_thread.run_sync(runtimes.registry.resolve, identifier)
            except HubError as exc:
                return JSONResponse({"error": str(exc)}, status_code=404)
            candidates = [(identifier, str(resolved))]
        else:
            return JSONResponse({"error": "workspace_id is required"}, status_code=400)
        for candidate, workspace_path in candidates:
            try:
                base = await anyio.to_thread.run_sync(runtimes.find_live, candidate)
                if base is None:
                    continue
                status, payload = await anyio.to_thread.run_sync(lambda: _runtime_json(
                    f"{base}/internal/v1/background-delegations/{operation}",
                    method="POST", body=body, timeout=10,
                ))
            except HubError:
                continue
            if status >= 400:
                if operation != "claim":
                    return JSONResponse(payload, status_code=status)
                continue
            delegation = payload.get("delegation")
            if delegation is not None:
                return JSONResponse({
                    "workspace_id": candidate, "workspace_path": workspace_path,
                    "delegation": delegation,
                })
            if operation != "claim":
                return JSONResponse({"workspace_id": candidate, "delegation": None})
        return JSONResponse({"workspace_id": None, "delegation": None})

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
        Route(
            "/internal/v1/background-delegations/{operation}",
            background_delegations, methods=["POST"],
        ),
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
