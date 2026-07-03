"""FastMCP server exposing the dev_loop mailbox tools."""

from __future__ import annotations

import ipaddress
import shutil
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import anyio

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .project_index import list_projects
from .store import DEFAULT_LEASE_SECONDS, InvalidInputError, Store, UnknownAgentError

MAX_WAIT_SECONDS = 60
MAX_LEASE_SECONDS = 3600
POLL_INTERVAL = 0.5

# The HTTP API is a local-only control surface: agents act on what lands in
# their inboxes, so a forged request is a prompt-injection channel. Defense is
# layered: the peer socket IP must be loopback (the load-bearing check — it is
# not client-controllable, so it holds even when bound beyond loopback with
# --host 0.0.0.0), the Host header must be a loopback hostname (blocks DNS
# rebinding), and any browser Origin must be a loopback origin (blocks CSRF).
_LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}
_AGENT_TOOL_CANDIDATES = [
    {
        "id": "claude",
        "name": "Claude Code",
        "command": "claude",
        "agent_name": "claude-code",
        "description": "Claude Code CLI",
    },
    {
        "id": "codex",
        "name": "Codex CLI",
        "command": "codex",
        "agent_name": "codex",
        "description": "OpenAI Codex CLI",
    },
    {
        "id": "gemini",
        "name": "Gemini CLI",
        "command": "gemini",
        "agent_name": "gemini",
        "description": "Google Gemini CLI",
    },
    {
        "id": "agy",
        "name": "Antigravity CLI",
        "command": "agy",
        "agent_name": "antigravity",
        "description": "Google Antigravity CLI",
    },
    {
        "id": "hermes",
        "name": "Hermes",
        "command": "hermes",
        "agent_name": "hermes",
        "description": "Hermes agent CLI",
    },
    {
        "id": "openclaw",
        "name": "OpenClaw",
        "command": "openclaw",
        "agent_name": "openclaw",
        "description": "OpenClaw agent CLI",
    },
]

# Store uses synchronous sqlite3; run every call in a worker thread so it
# never blocks the event loop (many concurrent long-polling clients).
_to_thread = anyio.to_thread.run_sync

_UI_HTML = (
    resources.files("dev_loop").joinpath("static/ui.html").read_text(encoding="utf-8")
)


async def _read_json(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _cors_headers(request: Request) -> dict[str, str]:
    origin = request.headers.get("origin")
    if origin and urlparse(origin).hostname in _LOCAL_HOSTNAMES:
        return {
            "access-control-allow-origin": origin,
            "access-control-allow-methods": "GET, POST, OPTIONS",
            "access-control-allow-headers": "content-type",
            "vary": "Origin",
        }
    return {}


def _json(request: Request, data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status_code, headers=_cors_headers(request))


def _json_error(
    message: str, status_code: int = 400, request: Request | None = None
) -> JSONResponse:
    headers = _cors_headers(request) if request is not None else None
    return JSONResponse({"error": message}, status_code=status_code, headers=headers)


def _is_loopback_peer(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return False


def _forbid_non_local(request: Request) -> JSONResponse | None:
    # Peer IP is not client-controllable — this is the check that holds even
    # when the server is bound beyond loopback (--host 0.0.0.0).
    if not _is_loopback_peer(request):
        return _json_error("API is only served to local clients", 403, request)
    if request.url.hostname not in _LOCAL_HOSTNAMES:
        return _json_error("API is only served to local hostnames", 403, request)
    origin = request.headers.get("origin")
    if origin and urlparse(origin).hostname not in _LOCAL_HOSTNAMES:
        return _json_error("cross-origin requests are not allowed", 403, request)
    return None


def _parse_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise InvalidInputError(f"{name} must be an integer, got {value!r}") from None


def detect_agent_tools() -> list[dict[str, Any]]:
    tools = []
    for candidate in _AGENT_TOOL_CANDIDATES:
        path = shutil.which(candidate["command"])
        tool = {
            **candidate,
            "installed": path is not None,
            "path": path,
        }
        if candidate["id"] == "hermes":
            profiles = detect_hermes_profiles()
            tool["profiles"] = profiles
            tool["profile_count"] = len(profiles)
        tools.append(tool)
    return tools


def detect_hermes_profiles(profile_root: Path | None = None) -> list[dict[str, str]]:
    root = profile_root or (Path.home() / ".hermes" / "profiles")
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name.lower())
    except OSError:
        return []
    return [
        {"name": path.name, "path": str(path)}
        for path in children
        if path.is_dir() and not path.name.startswith(".")
    ]


def list_agent_roles(agents_dir: Path | None = None) -> list[dict[str, str]]:
    root = agents_dir or (Path.cwd() / "agents")
    try:
        files = sorted(root.glob("*.md"), key=lambda path: path.stem)
    except OSError:
        return []
    roles = []
    for path in files:
        if path.name.startswith("_"):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        title = next(
            (line.lstrip("#").strip() for line in content.splitlines() if line.startswith("#")),
            path.stem,
        )
        roles.append(
            {
                "id": path.stem,
                "name": title,
                "path": str(path),
                "content": content,
            }
        )
    return roles


def create_server(
    host: str = "127.0.0.1",
    port: int = 8848,
    db_path: str | None = None,
    project: dict[str, Any] | None = None,
) -> FastMCP:
    store = Store(db_path)
    current_project = project or {
        "id": "",
        "name": "",
        "project_root": "",
        "db_path": str(store.db_path),
        "server_url": f"http://{host}:{port}",
        "host": host,
        "port": port,
        "last_seen": "",
    }

    mcp = FastMCP(
        "dev-loop",
        instructions=(
            "dev-loop is a local mailbox that lets LLM CLIs and agents pass prompts "
            "to each other. Protocol: 1) call register_agent once with a stable name "
            "(e.g. 'claude-code', 'codex', 'gemini'); 2) use send_message to hand a "
            "prompt to another agent; 3) call check_inbox periodically (use "
            "wait_seconds=30 for near-real-time delivery) to receive prompts sent to "
            "you; 4) after handling a received message, call ack_message so it is "
            "not redelivered; 5) reply with send_message using reply_to so "
            "conversations stay threaded. Hub/orchestrator agents receiving from many agents: run ONE "
            "polling loop only, and process each returned message in id order before "
            "polling again — do not run multiple concurrent check_inbox loops for "
            "the same agent name."
        ),
        host=host,
        port=port,
        stateless_http=True,
    )

    async def _deliver(
        sender: str,
        to: str,
        content: str,
        reply_to: int | None,
        kind: str,
        title: str,
        task_status: str,
    ) -> dict:
        """Shared delivery path for the MCP tool and the HTTP API."""
        await _to_thread(store.touch_agent, sender)
        try:
            ids = await _to_thread(
                store.send_message,
                sender,
                to,
                content,
                reply_to,
                kind,
                title,
                task_status,
            )
        except (UnknownAgentError, InvalidInputError) as exc:
            return {"delivered": 0, "message_ids": [], "error": str(exc)}
        if not ids:
            return {
                "delivered": 0,
                "message_ids": [],
                "note": "no recipients (broadcast with no other registered agents?)",
            }
        return {"delivered": len(ids), "message_ids": ids}

    @mcp.tool()
    async def register_agent(name: str, description: str = "") -> dict:
        """Register yourself (or refresh your registration) in the dev-loop agent
        registry. Call this once at the start of a session with a short stable name
        such as 'claude-code', 'codex', or 'gemini', and a one-line description of
        what you are working on. Returns the full list of registered agents so you
        can see who else is available to message."""
        try:
            agents = await _to_thread(store.register_agent, name, description)
        except InvalidInputError as exc:
            return {"registered": None, "agents": [], "error": str(exc)}
        return {"registered": name.strip(), "agents": agents}

    @mcp.tool()
    async def list_agents() -> list[dict]:
        """List all registered agents with their descriptions and last-seen
        timestamps. Use this to discover which agents you can send prompts to."""
        return await _to_thread(store.list_agents)

    @mcp.tool()
    async def send_message(
        sender: str,
        to: str,
        content: str,
        reply_to: int | None = None,
        kind: str = "message",
        title: str = "",
        task_status: str = "",
    ) -> dict:
        """Send a prompt/message to another agent's inbox.

        - sender: your own registered agent name.
        - to: the recipient's agent name, or "*" to broadcast to every registered
          agent except yourself.
        - content: the prompt or message text to deliver.
        - reply_to: optional id of the message you are replying to; set it so the
          recipient can reconstruct the conversation with get_thread.
        - kind: "message" or "task"; programming delegation should use "task".
        - title: optional short task title.
        - task_status: optional task status; defaults to "created" for tasks.
          Invalid values are rejected with an error.

        The message is stored durably and delivered when the recipient next calls
        check_inbox. Returns the created message id(s)."""
        return await _deliver(sender, to, content, reply_to, kind, title, task_status)

    @mcp.tool()
    async def check_inbox(
        agent: str,
        wait_seconds: int = 0,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> dict:
        """Fetch unread messages addressed to you and lease them for processing.

        - agent: your own registered agent name.
        - wait_seconds: 0 returns immediately; a positive value (max 60,
          recommended 30) long-polls, returning as soon as a message arrives.
        - lease_seconds: how long fetched messages are hidden from other polls
          before being redelivered if not acked; default 300, max 3600.

        Call this periodically while working so prompts from other agents reach
        you. Each message includes its id — pass it as reply_to in send_message
        when responding. After processing each message, call ack_message with
        that id so it will not be delivered again.

        If you are a hub/orchestrator receiving replies from many agents: use a
        single polling loop (never multiple concurrent pollers on the same agent
        name), and when a batch arrives, handle every message one at a time in
        ascending id order before calling check_inbox again."""
        await _to_thread(store.touch_agent, agent)
        wait = max(0, min(int(wait_seconds), MAX_WAIT_SECONDS))
        lease = max(1, min(int(lease_seconds), MAX_LEASE_SECONDS))
        deadline = anyio.current_time() + wait
        while True:
            try:
                messages = await _to_thread(store.fetch_unread, agent, lease)
            except UnknownAgentError as exc:
                return {"agent": agent, "count": 0, "messages": [], "error": str(exc)}
            if messages or anyio.current_time() >= deadline:
                return {"agent": agent, "count": len(messages), "messages": messages}
            await anyio.sleep(POLL_INTERVAL)

    @mcp.tool()
    async def ack_message(agent: str, message_id: int, lease_token: str) -> dict:
        """Acknowledge that you finished handling a received message.

        Once acked, the message will not be returned by check_inbox again.
        If a leased message is not acked before its lease expires, it becomes
        available for redelivery.

        lease_token must be copied from the message returned by check_inbox; it
        prevents an expired or unrelated lease from acking a newly redelivered
        message."""
        await _to_thread(store.touch_agent, agent)
        try:
            acked = await _to_thread(store.ack_message, agent, message_id, lease_token)
        except UnknownAgentError as exc:
            return {"acked": False, "message_id": message_id, "error": str(exc)}
        return {"acked": acked, "message_id": message_id}

    @mcp.tool()
    async def get_thread(message_id: int) -> list[dict]:
        """Return the full conversation thread containing the given message id:
        the reply_to chain back to the root plus all replies, ordered oldest
        first. Use this to recover context before answering a message that is
        part of an ongoing exchange."""
        return await _to_thread(store.get_thread, message_id)

    @mcp.custom_route("/", methods=["GET"])
    async def index(_: Request) -> RedirectResponse:
        return RedirectResponse("/ui")

    @mcp.custom_route("/ui", methods=["GET"])
    async def ui(request: Request) -> HTMLResponse | JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        return HTMLResponse(_UI_HTML)

    @mcp.custom_route("/api/{path:path}", methods=["OPTIONS"])
    async def api_options(request: Request) -> Response:
        if forbidden := _forbid_non_local(request):
            return forbidden
        return Response(status_code=204, headers=_cors_headers(request))

    @mcp.custom_route("/api/agents", methods=["GET"])
    async def api_list_agents(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        agents = await _to_thread(store.list_agents)
        return _json(request, {"agents": agents})

    @mcp.custom_route("/api/status", methods=["GET"])
    async def api_status(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        return _json(
            request,
            {
                "db_path": str(store.db_path),
                "project": {**current_project, "db_path": str(store.db_path)},
            },
        )

    @mcp.custom_route("/api/projects", methods=["GET"])
    async def api_projects(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        projects = await _to_thread(list_projects, current_project.get("id"))
        if current_project.get("id") and not any(
            project.get("id") == current_project.get("id") for project in projects
        ):
            projects.insert(0, {**current_project, "current": True, "online": True})
        return _json(
            request,
            {
                "current_project_id": current_project.get("id"),
                "projects": projects,
            },
        )

    @mcp.custom_route("/api/agent-tools", methods=["GET"])
    async def api_agent_tools(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        agents = await _to_thread(store.list_agents)
        registered = {agent["name"]: agent for agent in agents}
        tools = await _to_thread(detect_agent_tools)
        for tool in tools:
            agent = registered.get(tool["agent_name"])
            tool["registered"] = agent is not None
            tool["last_seen"] = agent["last_seen"] if agent else None
        return _json(request, {"tools": tools})

    @mcp.custom_route("/api/agent-roles", methods=["GET"])
    async def api_agent_roles(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        project_root = current_project.get("project_root")
        agents_dir = Path(project_root) / "agents" if project_root else Path.cwd() / "agents"
        roles = await _to_thread(list_agent_roles, agents_dir)
        return _json(request, {"roles": roles})

    @mcp.custom_route("/api/agent-roles/{role_id}", methods=["POST"])
    async def api_save_agent_role(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        role_id = request.path_params.get("role_id")
        if not role_id or not role_id.isidentifier() or role_id.startswith("_"):
            return _json_error("Invalid role ID", request=request)
        data = await _read_json(request)
        content = data.get("content")
        if content is None:
            return _json_error("Missing content", request=request)
        project_root = current_project.get("project_root")
        agents_dir = Path(project_root) / "agents" if project_root else Path.cwd() / "agents"
        if not agents_dir.is_dir():
            return _json_error("Agents directory not found", request=request)
        file_path = (agents_dir / f"{role_id}.md").resolve()
        if not str(file_path).startswith(str(agents_dir.resolve())):
            return _json_error("Access denied", request=request)
        def _write_role():
            file_path.write_text(content, encoding="utf-8")
        await _to_thread(_write_role)
        roles = await _to_thread(list_agent_roles, agents_dir)
        return _json(request, {"success": True, "roles": roles})

    @mcp.custom_route("/api/agents", methods=["POST"])
    async def api_register_agent(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        name = str(data.get("name", "")).strip()
        description = str(data.get("description", "")).strip()
        try:
            agents = await _to_thread(store.register_agent, name, description)
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, {"registered": name, "agents": agents})

    @mcp.custom_route("/api/messages", methods=["GET"])
    async def api_list_messages(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        params = request.query_params
        agent = params.get("agent") or None
        status = params.get("status", "all")
        kind = params.get("kind", "all")
        task_status = params.get("task_status", "all")
        try:
            limit = _parse_int(params.get("limit", "100"), "limit")
            messages = await _to_thread(
                store.list_messages, agent, status, kind, task_status, limit
            )
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, {"messages": messages})

    @mcp.custom_route("/api/messages", methods=["POST"])
    async def api_send_message(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        sender = str(data.get("sender", "")).strip()
        to = str(data.get("to", "")).strip()
        content = str(data.get("content", "")).strip()
        kind = str(data.get("kind", "message")).strip()
        title = str(data.get("title", "")).strip()
        task_status = str(data.get("task_status", "")).strip()
        reply_to = data.get("reply_to")
        if not sender or not to or not content:
            return _json_error("sender, to, and content are required", request=request)
        try:
            reply_to = None if reply_to in ("", None) else _parse_int(reply_to, "reply_to")
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        result = await _deliver(sender, to, content, reply_to, kind, title, task_status)
        if result.get("error"):
            return _json_error(result["error"], request=request)
        return _json(request, result)

    @mcp.custom_route("/api/messages/{message_id:int}/task-status", methods=["POST"])
    async def api_update_task_status(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        message_id = int(request.path_params["message_id"])
        task_status = str(data.get("task_status", "")).strip()
        if not task_status:
            return _json_error("task_status is required", request=request)
        try:
            updated = await _to_thread(store.update_task_status, message_id, task_status)
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        return _json(
            request,
            {"updated": updated, "message_id": message_id, "task_status": task_status}
        )

    @mcp.custom_route("/api/inbox/check", methods=["POST"])
    async def api_check_inbox(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        agent = str(data.get("agent", "")).strip()
        if not agent:
            return _json_error("agent is required", request=request)
        try:
            lease_seconds = _parse_int(
                data.get("lease_seconds", DEFAULT_LEASE_SECONDS), "lease_seconds"
            )
        except InvalidInputError as exc:
            return _json_error(str(exc), request=request)
        lease_seconds = max(1, min(lease_seconds, MAX_LEASE_SECONDS))
        await _to_thread(store.touch_agent, agent)
        try:
            messages = await _to_thread(store.fetch_unread, agent, lease_seconds)
        except UnknownAgentError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, {"agent": agent, "count": len(messages), "messages": messages})

    @mcp.custom_route("/api/messages/{message_id:int}/ack", methods=["POST"])
    async def api_ack_message(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        data = await _read_json(request)
        agent = str(data.get("agent", "")).strip()
        lease_token = str(data.get("lease_token", "")).strip()
        message_id = int(request.path_params["message_id"])
        if not agent or not lease_token:
            return _json_error("agent and lease_token are required", request=request)
        await _to_thread(store.touch_agent, agent)
        try:
            acked = await _to_thread(store.ack_message, agent, message_id, lease_token)
        except UnknownAgentError as exc:
            return _json_error(str(exc), request=request)
        return _json(request, {"acked": acked, "message_id": message_id})

    @mcp.custom_route("/api/thread/{message_id:int}", methods=["GET"])
    async def api_get_thread(request: Request) -> JSONResponse:
        if forbidden := _forbid_non_local(request):
            return forbidden
        message_id = int(request.path_params["message_id"])
        thread = await _to_thread(store.get_thread, message_id)
        return _json(request, {"messages": thread})

    return mcp
