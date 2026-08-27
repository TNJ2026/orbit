"""stdio-to-HTTP JSON-RPC transport adapter for local Agent Apps."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .error_text import reading
from .manifest import AgentAppManifest


INTERNAL_ERROR = -32603
PARSE_ERROR = -32700
_NOT_LOCAL = object()


EVENT_TOOLS = (
    {
        "name": "wait_app_event",
        "description": (
            "Wait for the next Orbit Runtime event already captured by this App. "
            "Re-read the relevant resource before acting."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_seconds": {
                    "type": "number", "minimum": 0, "maximum": 300, "default": 120,
                },
                "event_types": {
                    "type": "array", "items": {"type": "string"},
                },
            },
        },
    },
    {
        "name": "list_app_events",
        "description": "List unacknowledged Runtime events captured by this App.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "event_types": {
                    "type": "array", "items": {"type": "string"},
                },
            },
        },
    },
    {
        "name": "ack_app_event",
        "description": "Acknowledge one captured Runtime event after handling it.",
        "inputSchema": {
            "type": "object",
            "properties": {"event_id": {"type": "string"}},
            "required": ["event_id"],
        },
    },
)


def _error(request_id: Any, message: str, code: int = INTERNAL_ERROR) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _transport_error(request_id: Any, detail: str) -> dict[str, Any]:
    """A transport failure, said twice: once to act on, once to quote.

    The sentence is what a reader does something about; the raw text is what
    they put in a bug report.  Both travel, because the classification is a
    guess and a guess nobody can check is worse than no guess at all.
    """

    payload = _error(request_id, reading(detail))
    payload["error"]["data"] = {"detail": detail}
    return payload


def forward_http(url: str, message: Any, *, timeout: float = 330) -> Any:
    """Forward exactly one JSON-RPC payload without interpreting business data."""

    # Intentionally longer than the authoring wait maximum (300s): one local
    # loopback MCP request may be a long poll. The proxy is single-session and
    # synchronous by design, so this bounds a wedged upstream without cutting
    # off a legitimate wait_authoring_request call.

    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=body, method="POST", headers={"content-type": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except HTTPError as exc:
        payload = exc.read()
        if not payload:
            raise RuntimeError(f"MCP endpoint returned HTTP {exc.code}") from exc
    except (OSError, URLError) as exc:
        raise RuntimeError(f"MCP endpoint is unavailable: {exc.reason if isinstance(exc, URLError) else exc}") from exc
    if not payload:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("MCP endpoint returned invalid JSON") from exc


def serve_proxy(
    manifest: AgentAppManifest,
    *,
    stdin=None,
    stdout=None,
    forward: Callable[[str, Any], Any] = forward_http,
    state_dir: Path | str | None = None,
    bridge_factory=None,
) -> None:
    """Copy newline-delimited JSON-RPC between Codex and one ready App service."""

    if manifest.mcp is None:
        raise RuntimeError(f"{manifest.app_id} does not declare an MCP endpoint")
    source = sys.stdin if stdin is None else stdin
    sink = sys.stdout if stdout is None else stdout
    bridge = None
    inbox = None
    if manifest.events is not None:
        from .event_bridge import AgentAppEventBridge, EventInbox

        root = Path(state_dir or Path.home() / ".local/state/agent-apps" / manifest.app_id)
        inbox = EventInbox(root / "event-inbox.db")
        factory = bridge_factory or AgentAppEventBridge
        bridge = factory(manifest.events.url, inbox)
        bridge.start()
    try:
        for line in source:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                _emit(sink, _error(None, "each line must be JSON", PARSE_ERROR))
                continue
            request_id = message.get("id") if isinstance(message, Mapping) else None
            try:
                response = _local_response(message, inbox)
                if response is _NOT_LOCAL:
                    response = forward(manifest.mcp.url, message)
                    if inbox is not None:
                        response = _with_event_tools(message, response)
            except (RuntimeError, ValueError) as exc:
                _emit(sink, _transport_error(request_id, str(exc)))
                continue
            if response is not None:
                _emit(sink, response)
    finally:
        if bridge is not None:
            bridge.stop()


def _with_event_tools(message: Any, response: Any) -> Any:
    if not isinstance(message, Mapping) or message.get("method") != "tools/list":
        return response
    if not isinstance(response, Mapping) or not isinstance(response.get("result"), Mapping):
        return response
    tools = response["result"].get("tools")
    if not isinstance(tools, list):
        return response
    existing = {item.get("name") for item in tools if isinstance(item, Mapping)}
    result = dict(response["result"])
    result["tools"] = [*tools, *(tool for tool in EVENT_TOOLS if tool["name"] not in existing)]
    return {**response, "result": result}


def _tool_content(payload: Any) -> dict[str, Any]:
    return {
        "content": [{
            "type": "text",
            "text": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        }],
        "isError": False,
    }


def _local_response(message: Any, inbox) -> Any:
    if inbox is None or not isinstance(message, Mapping):
        return _NOT_LOCAL
    if message.get("method") != "tools/call":
        return _NOT_LOCAL
    parameters = message.get("params")
    if not isinstance(parameters, Mapping):
        return _NOT_LOCAL
    name = parameters.get("name")
    if name not in {tool["name"] for tool in EVENT_TOOLS}:
        return _NOT_LOCAL
    arguments = parameters.get("arguments") or {}
    if not isinstance(arguments, Mapping):
        raise ValueError("tool arguments must be an object")
    event_types = arguments.get("event_types") or []
    if not isinstance(event_types, list) or not all(
        isinstance(item, str) for item in event_types
    ):
        raise ValueError("event_types must be an array of strings")
    if name == "wait_app_event":
        event = inbox.wait(
            timeout_seconds=float(arguments.get("timeout_seconds", 120)),
            event_types=event_types,
        )
        payload = {"event": event, "cursor": inbox.cursor()}
    elif name == "list_app_events":
        payload = {
            "events": inbox.pending(
                limit=int(arguments.get("limit", 50)), event_types=event_types,
            ),
            "cursor": inbox.cursor(),
        }
    else:
        event_id = arguments.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event_id is required")
        payload = {"event_id": event_id, "acknowledged": inbox.acknowledge(event_id)}
    return {
        "jsonrpc": "2.0",
        "id": message.get("id"),
        "result": _tool_content(payload),
    }


def _emit(sink, payload: Any) -> None:
    sink.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    sink.flush()
