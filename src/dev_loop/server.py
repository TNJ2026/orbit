"""FastMCP server exposing the dev_loop mailbox tools."""

from __future__ import annotations

import anyio

from mcp.server.fastmcp import FastMCP

from .store import DEFAULT_DB_PATH, DEFAULT_LEASE_SECONDS, Store, UnknownAgentError

MAX_WAIT_SECONDS = 60
MAX_LEASE_SECONDS = 3600
POLL_INTERVAL = 0.5

# Store uses synchronous sqlite3; run every call in a worker thread so it
# never blocks the event loop (many concurrent long-polling clients).
_to_thread = anyio.to_thread.run_sync


def create_server(
    host: str = "127.0.0.1",
    port: int = 8848,
    db_path: str | None = None,
) -> FastMCP:
    store = Store(db_path or DEFAULT_DB_PATH)

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
    async def send_message(sender: str, to: str, content: str, reply_to: int | None = None) -> dict:
        """Send a prompt/message to another agent's inbox.

        - sender: your own registered agent name.
        - to: the recipient's agent name, or "*" to broadcast to every registered
          agent except yourself.
        - content: the prompt or message text to deliver.
        - reply_to: optional id of the message you are replying to; set it so the
          recipient can reconstruct the conversation with get_thread.

        The message is stored durably and delivered when the recipient next calls
        check_inbox. Returns the created message id(s)."""
        await _to_thread(store.touch_agent, sender)
        try:
            ids = await _to_thread(store.send_message, sender, to, content, reply_to)
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

    return mcp
