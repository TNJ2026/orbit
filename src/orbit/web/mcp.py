"""`/mcp` — the Model Context Protocol surface for agent CLIs.

JSON-RPC 2.0 over a single POST, implemented directly on Starlette: the
protocol is small enough that a dependency would cost more than it saves, and
the runtime already owns the identity, authorisation and idempotency rules that
matter here.

Every tool call goes through the same RunApplicationService as `/api/v1` and
`orbit run`. Nothing is anonymous — a caller without the right scope gets the
same refusal it would get over HTTP — and a write tool without an idempotency
key is rejected rather than silently retried into a duplicate run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..workflow.api.read_models import ReadModelService
from ..workflow.application.run_service import RunApplicationService, RunStartError
from .api_v1 import READ_SCOPE, WRITE_SCOPE, Authorizer

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "orbit", "version": "1.0"}

# JSON-RPC reserved codes; -32001 is our application-level refusal.
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
PARSE_ERROR = -32700
NOT_AUTHORIZED = -32001


def _result(request_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _failure(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _content(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    """MCP tool results are text content; JSON keeps them machine-readable."""

    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False, sort_keys=True)}
        ],
        "isError": is_error,
    }


def build_mcp(
    db_path: Path | str,
    durable_service,
    *,
    authenticator: Callable[[Request], str | None] | None = None,
    authorizer: Authorizer | None = None,
) -> list[Route]:
    path = Path(db_path)
    reads = ReadModelService(path)
    runs = RunApplicationService(path, durable_service)
    guard = authorizer or Authorizer()

    tools = (
        {
            "name": "list_runs",
            "description": "List workflow runs, newest first.",
            "scope": READ_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    "active_only": {"type": "boolean"},
                },
            },
        },
        {
            "name": "inspect_run",
            "description": "Why a run is where it is: status, open responsibilities, recent errors.",
            "scope": READ_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
        },
        {
            "name": "start_run",
            "description": "Start a run of a published workflow.",
            "scope": WRITE_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "workflow_version": {"type": "integer"},
                    "input": {"type": "object"},
                    "goal": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["workflow_id", "idempotency_key"],
            },
        },
        {
            "name": "cancel_run",
            "description": "Cancel a run at the version the caller last observed.",
            "scope": WRITE_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "expected_version": {"type": "integer"},
                    "reason": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["run_id", "expected_version", "idempotency_key"],
            },
        },
    )
    by_name = {tool["name"]: tool for tool in tools}

    def call(name: str, arguments: Mapping[str, Any], actor: str) -> Any:
        if name == "list_runs":
            items, cursor = reads.list_runs(
                limit=min(200, max(1, int(arguments.get("limit", 20)))),
                active_only=bool(arguments.get("active_only", False)),
            )
            return {"runs": items, "next_cursor": cursor}
        if name == "inspect_run":
            return runs.inspect(str(arguments["run_id"]))
        if name == "start_run":
            started = runs.start_run(
                workflow_id=str(arguments["workflow_id"]),
                version=arguments.get("workflow_version"),
                inputs=arguments.get("input") or {},
                goal=str(arguments.get("goal", "")),
                actor=actor,
                idempotency_key=str(arguments["idempotency_key"]),
            )
            return started.to_dict()
        if name == "cancel_run":
            return runs.cancel_run(
                str(arguments["run_id"]), int(arguments["expected_version"]),
                actor=actor, idempotency_key=str(arguments["idempotency_key"]),
                reason=str(arguments.get("reason", "cancelled via mcp")),
            )
        raise KeyError(name)

    def dispatch(message: Mapping[str, Any], actor: str | None) -> dict[str, Any] | None:
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}

        if method == "initialize":
            return _result(request_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            })
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None  # notifications carry no id and get no response
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            return _result(request_id, {
                "tools": [
                    {k: v for k, v in tool.items() if k != "scope"} for tool in tools
                ]
            })
        if method != "tools/call":
            return _failure(request_id, METHOD_NOT_FOUND, f"unknown method {method}")

        name = str(params.get("name", ""))
        tool = by_name.get(name)
        if tool is None:
            return _failure(request_id, INVALID_PARAMS, f"unknown tool {name}")
        if actor is None:
            return _failure(request_id, NOT_AUTHORIZED, "valid actor credentials are required")
        if not guard.allows(actor, tool["scope"]):
            return _failure(request_id, NOT_AUTHORIZED, f"actor lacks scope {tool['scope']}")

        try:
            payload = call(name, params.get("arguments") or {}, actor)
        except KeyError as exc:
            return _result(request_id, _content({"error": f"missing argument {exc}"}, is_error=True))
        except (RunStartError, ValueError) as exc:
            # A tool that fails on its own terms is a result, not a protocol
            # error: the caller is an agent that needs to read the reason.
            return _result(request_id, _content({"error": str(exc)}, is_error=True))
        except PermissionError as exc:
            return _failure(request_id, NOT_AUTHORIZED, str(exc))
        return _result(request_id, _content(payload))

    async def endpoint(request: Request) -> JSONResponse:
        actor = None if authenticator is None else authenticator(request)
        if actor is not None and not actor.strip():
            actor = None
        try:
            message = json.loads(await request.body() or b"")
        except json.JSONDecodeError:
            return JSONResponse(_failure(None, PARSE_ERROR, "request body must be JSON"))

        if isinstance(message, list):
            responses = [
                response for item in message
                if isinstance(item, Mapping) and (response := dispatch(item, actor)) is not None
            ]
            return JSONResponse(responses) if responses else JSONResponse(None, status_code=202)
        if not isinstance(message, Mapping) or message.get("jsonrpc") != "2.0":
            return JSONResponse(_failure(None, INVALID_REQUEST, "expected a JSON-RPC 2.0 message"))

        response = dispatch(message, actor)
        if response is None:
            return JSONResponse(None, status_code=202)
        return JSONResponse(response)

    return [Route("/mcp", endpoint, methods=["POST"])]
