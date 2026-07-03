"""FastMCP server exposing the dev_loop mailbox tools."""

from __future__ import annotations

import anyio

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from .store import DEFAULT_LEASE_SECONDS, Store, UnknownAgentError, project_db_path

MAX_WAIT_SECONDS = 60
MAX_LEASE_SECONDS = 3600
POLL_INTERVAL = 0.5

# Store uses synchronous sqlite3; run every call in a worker thread so it
# never blocks the event loop (many concurrent long-polling clients).
_to_thread = anyio.to_thread.run_sync


async def _read_json(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _json_error(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def create_server(
    host: str = "127.0.0.1",
    port: int = 8848,
    db_path: str | None = None,
) -> FastMCP:
    store = Store(db_path or project_db_path())

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

    @mcp.tool()
    async def register_agent(name: str, description: str = "") -> dict:
        """Register yourself (or refresh your registration) in the dev-loop agent
        registry. Call this once at the start of a session with a short stable name
        such as 'claude-code', 'codex', or 'gemini', and a one-line description of
        what you are working on. Returns the full list of registered agents so you
        can see who else is available to message."""
        agents = await _to_thread(store.register_agent, name, description)
        return {"registered": name, "agents": agents}

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

        The message is stored durably and delivered when the recipient next calls
        check_inbox. Returns the created message id(s)."""
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
        except UnknownAgentError as exc:
            return {"delivered": 0, "message_ids": [], "error": str(exc)}
        if not ids:
            return {"delivered": 0, "message_ids": [], "note": "no recipients (broadcast with no other registered agents?)"}
        return {"delivered": len(ids), "message_ids": ids}

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
    async def ui(_: Request) -> HTMLResponse:
        return HTMLResponse(_UI_HTML)

    @mcp.custom_route("/api/agents", methods=["GET"])
    async def api_list_agents(_: Request) -> JSONResponse:
        agents = await _to_thread(store.list_agents)
        return JSONResponse({"agents": agents})

    @mcp.custom_route("/api/agents", methods=["POST"])
    async def api_register_agent(request: Request) -> JSONResponse:
        data = await _read_json(request)
        name = str(data.get("name", "")).strip()
        description = str(data.get("description", "")).strip()
        if not name:
            return _json_error("agent name is required")
        agents = await _to_thread(store.register_agent, name, description)
        return JSONResponse({"registered": name, "agents": agents})

    @mcp.custom_route("/api/messages", methods=["GET"])
    async def api_list_messages(request: Request) -> JSONResponse:
        params = request.query_params
        agent = params.get("agent") or None
        status = params.get("status", "all")
        kind = params.get("kind", "all")
        task_status = params.get("task_status", "all")
        limit = int(params.get("limit", "100"))
        messages = await _to_thread(
            store.list_messages, agent, status, kind, task_status, limit
        )
        return JSONResponse({"messages": messages})

    @mcp.custom_route("/api/messages", methods=["POST"])
    async def api_send_message(request: Request) -> JSONResponse:
        data = await _read_json(request)
        sender = str(data.get("sender", "")).strip()
        to = str(data.get("to", "")).strip()
        content = str(data.get("content", "")).strip()
        kind = str(data.get("kind", "message")).strip()
        title = str(data.get("title", "")).strip()
        task_status = str(data.get("task_status", "")).strip()
        reply_to = data.get("reply_to")
        if reply_to in ("", None):
            reply_to = None
        else:
            reply_to = int(reply_to)
        if not sender or not to or not content:
            return _json_error("sender, to, and content are required")
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
        except UnknownAgentError as exc:
            return _json_error(str(exc))
        return JSONResponse({"delivered": len(ids), "message_ids": ids})

    @mcp.custom_route("/api/messages/{message_id:int}/task-status", methods=["POST"])
    async def api_update_task_status(request: Request) -> JSONResponse:
        data = await _read_json(request)
        message_id = int(request.path_params["message_id"])
        task_status = str(data.get("task_status", "")).strip()
        if not task_status:
            return _json_error("task_status is required")
        updated = await _to_thread(store.update_task_status, message_id, task_status)
        return JSONResponse(
            {"updated": updated, "message_id": message_id, "task_status": task_status}
        )

    @mcp.custom_route("/api/inbox/check", methods=["POST"])
    async def api_check_inbox(request: Request) -> JSONResponse:
        data = await _read_json(request)
        agent = str(data.get("agent", "")).strip()
        lease_seconds = int(data.get("lease_seconds", DEFAULT_LEASE_SECONDS))
        lease_seconds = max(1, min(lease_seconds, MAX_LEASE_SECONDS))
        if not agent:
            return _json_error("agent is required")
        await _to_thread(store.touch_agent, agent)
        try:
            messages = await _to_thread(store.fetch_unread, agent, lease_seconds)
        except UnknownAgentError as exc:
            return _json_error(str(exc))
        return JSONResponse({"agent": agent, "count": len(messages), "messages": messages})

    @mcp.custom_route("/api/messages/{message_id:int}/ack", methods=["POST"])
    async def api_ack_message(request: Request) -> JSONResponse:
        data = await _read_json(request)
        agent = str(data.get("agent", "")).strip()
        lease_token = str(data.get("lease_token", "")).strip()
        message_id = int(request.path_params["message_id"])
        if not agent or not lease_token:
            return _json_error("agent and lease_token are required")
        await _to_thread(store.touch_agent, agent)
        try:
            acked = await _to_thread(store.ack_message, agent, message_id, lease_token)
        except UnknownAgentError as exc:
            return _json_error(str(exc))
        return JSONResponse({"acked": acked, "message_id": message_id})

    @mcp.custom_route("/api/thread/{message_id:int}", methods=["GET"])
    async def api_get_thread(request: Request) -> JSONResponse:
        message_id = int(request.path_params["message_id"])
        thread = await _to_thread(store.get_thread, message_id)
        return JSONResponse({"messages": thread})

    return mcp


_UI_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>dev-loop</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f7f4;
      --panel: #ffffff;
      --ink: #1d2326;
      --muted: #687176;
      --line: #d9dedb;
      --accent: #256c5a;
      --accent-ink: #ffffff;
      --warn: #9a5a12;
      --danger: #a33b3b;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      height: 52px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 18px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    h1 { font-size: 17px; margin: 0; font-weight: 650; }
    main {
      display: grid;
      grid-template-columns: 260px minmax(360px, 1fr) minmax(340px, 0.95fr);
      height: calc(100vh - 52px);
      min-height: 560px;
    }
    section {
      min-width: 0;
      border-right: 1px solid var(--line);
      overflow: auto;
      background: var(--panel);
    }
    section:last-child { border-right: 0; }
    .pane-head {
      position: sticky;
      top: 0;
      z-index: 2;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      min-height: 49px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.94);
      backdrop-filter: blur(8px);
    }
    h2 { font-size: 13px; margin: 0; text-transform: uppercase; color: var(--muted); }
    button, input, select, textarea {
      font: inherit;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
    }
    button {
      min-height: 32px;
      padding: 5px 10px;
      cursor: pointer;
    }
    button.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
    button.danger { color: var(--danger); }
    button:disabled { opacity: .55; cursor: default; }
    input, select, textarea { width: 100%; padding: 7px 8px; }
    textarea { min-height: 96px; resize: vertical; }
    .stack { display: grid; gap: 8px; padding: 12px; }
    .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    .row > * { min-width: 0; }
    .row .grow { flex: 1; }
    .agent, .message, .thread-item {
      border-bottom: 1px solid var(--line);
      padding: 11px 12px;
      cursor: pointer;
    }
    .agent.active, .message.active { background: #eef5f1; }
    .agent-name, .message-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      font-weight: 650;
    }
    .meta, .preview {
      margin-top: 3px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .preview { color: #3f494e; }
    .badge {
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      padding: 2px 7px;
      border: 1px solid var(--line);
      border-radius: 999px;
      font-size: 12px;
      color: var(--muted);
      background: #fff;
      white-space: nowrap;
    }
    .badge.available { color: var(--accent); border-color: #9fc7b9; }
    .badge.leased { color: var(--warn); border-color: #deb16f; }
    .badge.read { color: var(--muted); }
    .empty, .error {
      padding: 18px 12px;
      color: var(--muted);
    }
    .error { color: var(--danger); }
    .composer {
      border-top: 1px solid var(--line);
      background: #fbfbf9;
    }
    .thread {
      min-height: 220px;
    }
    .thread-item { cursor: default; }
    .thread-body {
      margin-top: 7px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    @media (max-width: 980px) {
      main {
        grid-template-columns: 1fr;
        height: auto;
      }
      section {
        min-height: 360px;
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }
    }
  </style>
</head>
<body>
  <header>
    <h1>dev-loop</h1>
    <div id="status" class="meta"></div>
  </header>
  <main>
    <section>
      <div class="pane-head">
        <h2>Agents</h2>
        <button id="refreshAgents">Refresh</button>
      </div>
      <div class="stack">
        <input id="agentName" placeholder="agent name">
        <input id="agentDescription" placeholder="description">
        <button id="registerAgent" class="primary">Register</button>
      </div>
      <div id="agents"></div>
    </section>

    <section>
      <div class="pane-head">
        <h2>Messages</h2>
        <div class="row">
          <select id="messageStatus">
            <option value="all">All</option>
            <option value="available">Available</option>
            <option value="leased">Leased</option>
            <option value="read">Read</option>
          </select>
          <select id="messageKind">
            <option value="all">Any kind</option>
            <option value="task">Tasks</option>
            <option value="message">Messages</option>
          </select>
          <select id="taskStatusFilter">
            <option value="all">Any task</option>
            <option value="created">Created</option>
            <option value="assigned">Assigned</option>
            <option value="in_progress">In progress</option>
            <option value="replied">Replied</option>
            <option value="accepted">Accepted</option>
            <option value="needs_changes">Needs changes</option>
            <option value="blocked">Blocked</option>
            <option value="closed">Closed</option>
          </select>
          <button id="claimInbox">Claim inbox</button>
          <button id="refreshMessages">Refresh</button>
        </div>
      </div>
      <div id="messages"></div>
    </section>

    <section>
      <div class="pane-head">
        <h2>Thread</h2>
        <div class="row">
          <select id="taskStatusAction">
            <option value="created">Created</option>
            <option value="assigned">Assigned</option>
            <option value="in_progress">In progress</option>
            <option value="replied">Replied</option>
            <option value="accepted">Accepted</option>
            <option value="needs_changes">Needs changes</option>
            <option value="blocked">Blocked</option>
            <option value="closed">Closed</option>
          </select>
          <button id="updateTaskStatus" disabled>Set status</button>
          <button id="ackMessage" class="danger" disabled>Ack</button>
        </div>
      </div>
      <div id="thread" class="thread empty">No message selected.</div>
      <div class="composer stack">
        <div class="row">
          <select id="composeKind">
            <option value="task">Task</option>
            <option value="message">Message</option>
          </select>
          <select id="taskTemplate">
            <option value="analyze">Analyze</option>
            <option value="implement">Implement</option>
            <option value="review">Review</option>
            <option value="test">Test</option>
            <option value="custom">Custom</option>
          </select>
        </div>
        <input id="taskTitle" placeholder="task title">
        <div class="row">
          <input id="sendTo" class="grow" placeholder="to">
          <input id="replyTo" style="max-width: 110px" placeholder="reply to">
        </div>
        <textarea id="content" placeholder="message"></textarea>
        <button id="sendMessage" class="primary">Send</button>
      </div>
    </section>
  </main>
  <script>
    const state = {
      agents: [],
      selectedAgent: "",
      selectedMessage: null,
      leases: new Map()
    };

    const $ = (id) => document.getElementById(id);

    async function api(path, options = {}) {
      const response = await fetch(path, {
        headers: { "content-type": "application/json" },
        ...options
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || response.statusText);
      return data;
    }

    function setStatus(text, isError = false) {
      $("status").textContent = text;
      $("status").style.color = isError ? "var(--danger)" : "var(--muted)";
    }

    function fmtTime(value) {
      if (!value) return "";
      return new Date(value).toLocaleString();
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }

    async function loadAgents() {
      const data = await api("/api/agents");
      state.agents = data.agents;
      if (!state.selectedAgent && state.agents.length) {
        state.selectedAgent = state.agents[0].name;
      }
      renderAgents();
    }

    function renderAgents() {
      $("agents").innerHTML = state.agents.length ? state.agents.map(agent => `
        <div class="agent ${agent.name === state.selectedAgent ? "active" : ""}" data-agent="${escapeHtml(agent.name)}">
          <div class="agent-name">
            <span>${escapeHtml(agent.name)}</span>
            <span class="badge">${fmtTime(agent.last_seen)}</span>
          </div>
          <div class="meta">${escapeHtml(agent.description || "")}</div>
        </div>
      `).join("") : `<div class="empty">No agents.</div>`;
      document.querySelectorAll(".agent").forEach(el => {
        el.addEventListener("click", async () => {
          state.selectedAgent = el.dataset.agent;
          renderAgents();
          await loadMessages();
        });
      });
    }

    async function loadMessages() {
      const params = new URLSearchParams({
        status: $("messageStatus").value,
        kind: $("messageKind").value,
        task_status: $("taskStatusFilter").value,
        limit: "100"
      });
      if (state.selectedAgent) params.set("agent", state.selectedAgent);
      const data = await api(`/api/messages?${params}`);
      renderMessages(data.messages);
    }

    function renderMessages(messages) {
      $("messages").innerHTML = messages.length ? messages.map(message => `
        <div class="message ${state.selectedMessage && state.selectedMessage.id === message.id ? "active" : ""}" data-id="${message.id}">
          <div class="message-title">
            <span>#${message.id} ${escapeHtml(message.sender)} to ${escapeHtml(message.recipient)}</span>
            <span class="badge ${escapeHtml(message.status || "available")}">${escapeHtml(message.status || "available")}</span>
          </div>
          <div class="meta">
            ${escapeHtml(message.kind || "message")}
            ${message.title ? ` - ${escapeHtml(message.title)}` : ""}
            ${message.task_status ? ` - ${escapeHtml(message.task_status)}` : ""}
          </div>
          <div class="preview">${escapeHtml(message.content).slice(0, 180)}</div>
          <div class="meta">${fmtTime(message.created_at)} - deliveries ${message.delivery_count || 0}</div>
        </div>
      `).join("") : `<div class="empty">No messages.</div>`;
      document.querySelectorAll(".message").forEach(el => {
        el.addEventListener("click", () => selectMessage(Number(el.dataset.id)));
      });
    }

    async function selectMessage(id) {
      const data = await api(`/api/thread/${id}`);
      state.selectedMessage = data.messages.find(message => message.id === id) || data.messages[0] || null;
      const selectedLease = state.leases.get(id);
      $("ackMessage").disabled = !selectedLease;
      $("updateTaskStatus").disabled = !state.selectedMessage || state.selectedMessage.kind !== "task";
      if (state.selectedMessage && state.selectedMessage.task_status) {
        $("taskStatusAction").value = state.selectedMessage.task_status;
      }
      $("replyTo").value = id || "";
      if (state.selectedMessage) $("sendTo").value = state.selectedMessage.sender;
      $("thread").className = "thread";
      $("thread").innerHTML = data.messages.length ? data.messages.map(message => `
        <div class="thread-item">
          <div class="message-title">
            <span>#${message.id} ${escapeHtml(message.sender)} to ${escapeHtml(message.recipient)}</span>
            <span class="badge">${fmtTime(message.created_at)}</span>
          </div>
          <div class="meta">
            ${escapeHtml(message.kind || "message")}
            ${message.title ? ` - ${escapeHtml(message.title)}` : ""}
            ${message.task_status ? ` - ${escapeHtml(message.task_status)}` : ""}
          </div>
          <div class="thread-body">${escapeHtml(message.content)}</div>
        </div>
      `).join("") : `<div class="empty">Thread not found.</div>`;
      await loadMessages();
    }

    async function claimInbox() {
      if (!state.selectedAgent) throw new Error("select an agent first");
      const data = await api("/api/inbox/check", {
        method: "POST",
        body: JSON.stringify({ agent: state.selectedAgent, lease_seconds: 300 })
      });
      for (const message of data.messages) {
        state.leases.set(message.id, message.lease_token);
      }
      setStatus(`Claimed ${data.count} message(s) for ${state.selectedAgent}`);
      await loadMessages();
      if (data.messages[0]) await selectMessage(data.messages[0].id);
    }

    async function ackSelected() {
      if (!state.selectedMessage) return;
      const token = state.leases.get(state.selectedMessage.id);
      if (!token) return;
      const data = await api(`/api/messages/${state.selectedMessage.id}/ack`, {
        method: "POST",
        body: JSON.stringify({ agent: state.selectedAgent, lease_token: token })
      });
      if (data.acked) {
        state.leases.delete(state.selectedMessage.id);
        $("ackMessage").disabled = true;
      }
      setStatus(data.acked ? `Acked #${data.message_id}` : `Ack failed for #${data.message_id}`, !data.acked);
      await loadMessages();
    }

    async function updateSelectedTaskStatus() {
      if (!state.selectedMessage || state.selectedMessage.kind !== "task") return;
      const data = await api(`/api/messages/${state.selectedMessage.id}/task-status`, {
        method: "POST",
        body: JSON.stringify({ task_status: $("taskStatusAction").value })
      });
      setStatus(data.updated ? `Updated #${data.message_id} to ${data.task_status}` : `Task status unchanged`, !data.updated);
      await selectMessage(state.selectedMessage.id);
    }

    async function registerAgent() {
      await api("/api/agents", {
        method: "POST",
        body: JSON.stringify({
          name: $("agentName").value,
          description: $("agentDescription").value
        })
      });
      $("agentName").value = "";
      $("agentDescription").value = "";
      await loadAgents();
      await loadMessages();
    }

    async function sendMessage() {
      if (!state.selectedAgent) throw new Error("select an agent first");
      const replyTo = $("replyTo").value.trim();
      const data = await api("/api/messages", {
        method: "POST",
        body: JSON.stringify({
          sender: state.selectedAgent,
          to: $("sendTo").value,
          kind: $("composeKind").value,
          title: $("taskTitle").value,
          task_status: $("composeKind").value === "task" ? "assigned" : "",
          content: $("content").value,
          reply_to: replyTo || null
        })
      });
      $("content").value = "";
      $("taskTitle").value = "";
      setStatus(`Delivered ${data.delivered} message(s)`);
      await loadMessages();
      if (data.message_ids[0]) await selectMessage(data.message_ids[0]);
    }

    async function run(action) {
      try {
        await action();
      } catch (error) {
        setStatus(error.message, true);
      }
    }

    $("refreshAgents").addEventListener("click", () => run(loadAgents));
    $("refreshMessages").addEventListener("click", () => run(loadMessages));
    $("messageStatus").addEventListener("change", () => run(loadMessages));
    $("messageKind").addEventListener("change", () => run(loadMessages));
    $("taskStatusFilter").addEventListener("change", () => run(loadMessages));
    $("registerAgent").addEventListener("click", () => run(registerAgent));
    $("claimInbox").addEventListener("click", () => run(claimInbox));
    $("ackMessage").addEventListener("click", () => run(ackSelected));
    $("updateTaskStatus").addEventListener("click", () => run(updateSelectedTaskStatus));
    $("sendMessage").addEventListener("click", () => run(sendMessage));
    $("taskTemplate").addEventListener("change", applyTemplate);
    $("composeKind").addEventListener("change", () => {
      if ($("composeKind").value === "task") applyTemplate();
    });

    function applyTemplate() {
      if ($("composeKind").value !== "task" || $("taskTemplate").value === "custom") return;
      const templates = {
        analyze: "Task Type: analyze\\n\\nContext:\\n- Repo path:\\n- User goal:\\n- Relevant files:\\n- Constraints:\\n\\nDeliverable:\\n- Root cause or key findings\\n- Suggested change\\n- Tests to add\\n- Risks",
        implement: "Task Type: implement\\n\\nContext:\\n- Repo path:\\n- User goal:\\n- Files to edit:\\n- Constraints:\\n\\nDeliverable:\\n- Summary of changes\\n- Patch or exact file edits\\n- Verification command\\n- Risks",
        review: "Task Type: review\\n\\nContext:\\n- Repo path:\\n- Change under review:\\n- Files to inspect:\\n\\nDeliverable:\\n- Findings ordered by severity\\n- File and line references\\n- Missing tests\\n- Residual risk",
        test: "Task Type: test\\n\\nContext:\\n- Repo path:\\n- Feature or bugfix:\\n- Test target:\\n\\nDeliverable:\\n- Test plan\\n- Commands run\\n- Failures found\\n- Recommended fixes"
      };
      if (!$("content").value.trim()) $("content").value = templates[$("taskTemplate").value];
      if (!$("taskTitle").value.trim()) $("taskTitle").value = `${$("taskTemplate").value} task`;
    }

    run(async () => {
      await loadAgents();
      await loadMessages();
      applyTemplate();
      setStatus("Ready");
    });
  </script>
</body>
</html>
"""
