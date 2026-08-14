"""`/mcp` — the Model Context Protocol surface for agent CLIs.

JSON-RPC 2.0 over a single POST, implemented directly on Starlette: the
protocol is small enough that a dependency would cost more than it saves, and
the runtime already owns the identity, authorisation and idempotency rules that
matter here.

Every tool call goes through the same application services as `/api/v1`.
Nothing is anonymous — a caller without the right scope gets the
same refusal it would get over HTTP — and a write tool without an idempotency
key is rejected rather than silently retried into a duplicate run.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..workflow.api.workflow_catalog import WorkflowCatalogReadModelService
from ..workflow.application.authoring_job_service import (
    AuthoringJobConflict, AuthoringJobService,
)
from ..workflow.catalogs import InMemorySchemaCatalog
from .api_v1 import (
    OPS_READ_SCOPE, OPS_WRITE_SCOPE, READ_SCOPE, WRITE_SCOPE, Authorizer,
)

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


def build_mcp_dispatcher(
    db_path: Path | str,
    *,
    clock=None,
    workflow_db_path: Path | str | None = None,
    authorizer: Authorizer | None = None,
    schema_catalog=None,
    artifact_backend=None,
    authoring_service=None,
    workflow_publisher=None,
    authoring_jobs=None,
    authoring_broker=None,
    langgraph_service=None,
) -> Callable[[Mapping[str, Any], str | None], dict[str, Any] | None]:
    """One JSON-RPC message in, at most one response out.

    Transport-free on purpose. HTTP and stdio are two ways of carrying the
    same bytes, and a second transport must not become a second implementation
    of the tools, the scopes or the idempotency rules — those live here, once.
    """

    path = Path(db_path)
    workflow_path = Path(workflow_db_path or db_path)
    workflow_reads = WorkflowCatalogReadModelService(
        workflow_path, schema_catalog or InMemorySchemaCatalog({}),
        usage_source=getattr(langgraph_service, "workflow_usage", None),
    )
    # Shared with `/api/v1` rather than built again. Two of these against one
    # database each recover every queued job on their own thread, so a single
    # authoring job would run the Agent CLI twice — and the one constructed
    # second would fail the jobs the first had just started. The composition
    # root passes one in; the fallback exists for callers that build this
    # surface on its own.
    authoring_jobs = authoring_jobs or (
        AuthoringJobService(
            path, authoring_service, workflow_publisher,
            workflow_db_path=workflow_path,
        )
        if authoring_service is not None and workflow_publisher is not None
        else None
    )
    guard = authorizer or Authorizer()
    now = clock or (lambda: datetime.now(timezone.utc))

    tools = (
        # -- discovery ----------------------------------------------------
        # `start_run` needs a workflow_id, and until now nothing over MCP could
        # produce one: an agent had to be told out of band what it was allowed
        # to run. `goal_readiness` comes along because a workflow that cannot
        # start from a Goal is not a candidate, and finding that out by failing
        # a start is a worse way to learn it.
        {
            "name": "list_workflows",
            "description": (
                "Published workflows a run can be started from, with the inputs "
                "each one takes and whether it can start from a plain goal."
            ),
            "scope": READ_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ready_only": {
                        "type": "boolean",
                        "description": "Keep only workflows a goal can start.",
                    },
                },
            },
        },
        # -- authoring ----------------------------------------------------
        # Generation is a job, not a call: it runs an Agent CLI and compiles
        # what comes back. The pair is deliberate — one tool starts it, the
        # other is how the caller learns what happened.
        {
            "name": "generate_workflow",
            "description": (
                "Draft a new workflow from a description. Returns a job; poll "
                "get_authoring_job for the outcome. Nothing is published until "
                "the compiler accepts it."
            ),
            "scope": WRITE_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "agent": {"type": "string"},
                    "display_language": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["prompt", "idempotency_key"],
            },
        },
        {
            "name": "get_authoring_job",
            "description": (
                "State of a workflow authoring job: queued, running, done or "
                "failed, with the compiler's findings when it failed."
            ),
            "scope": READ_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        },
        # A job whose chosen agent is a connected App writes no DSL of its own:
        # it parks the prompt here and waits for that App to answer. These two
        # tools are that exchange, and a job needs one round-trip per compile
        # attempt.
        {
            "name": "claim_authoring_request",
            "description": (
                "Take the oldest generation prompt waiting for this client, "
                "or nothing when none is waiting. Write the DSL the prompt "
                "asks for and return it with submit_authoring_response. Pass "
                "the same `client` name every time: it is how an author "
                "addresses this App by the name it registered, and polling is what "
                "keeps the App listed as connected. A claim is a lease — "
                "answer within lease_seconds or the request is offered to "
                "somebody else. Also lists every request still waiting, "
                "claimed ones included."
            ),
            "scope": WRITE_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "client": {
                        "type": "string",
                        "description": (
                            "A stable name for this App, e.g. its product "
                            "name. Omit to take only unaddressed work."
                        ),
                    },
                },
            },
        },
        {
            "name": "wait_authoring_request",
            "description": (
                "Wait for a workflow generation prompt addressed to this App. "
                "While waiting, the App is offered in Orbit under this name. "
                "Use this when a person will click Generate in the Orbit UI."
            ),
            "scope": WRITE_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "client": {
                        "type": "string",
                        "description": (
                            "The name this App is offered under in Orbit. Choose a "
                            "stable, readable one; a name another Agent already "
                            "answers to is refused rather than renamed."
                        ),
                    },
                    "timeout_seconds": {
                        "type": "integer", "minimum": 1, "maximum": 300,
                        "default": 120,
                    },
                },
                "required": ["client"],
            },
        },
        {
            "name": "submit_authoring_response",
            "description": (
                "Answer a claimed generation request with the DSL document. "
                "It is compiled and published like any other agent's answer; "
                "a rejected document comes back as a fresh request carrying "
                "the compiler's findings."
            ),
            "scope": WRITE_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "request_id": {"type": "string"},
                    "dsl": {
                        "description": (
                            "The DSL document, as a JSON object or as the "
                            "text of one."
                        ),
                    },
                },
                "required": ["request_id", "dsl"],
            },
        },
    )
    if langgraph_service is not None:
        tools += (
            {
                "name": "list_runs",
                "description": "List workflow runs executed by LangGraph.",
                "scope": READ_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                },
            },
            {
                "name": "inspect_run",
                "description": "Inspect one LangGraph run and its interrupts.",
                "scope": READ_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {"run_id": {"type": "string"}},
                    "required": ["run_id"],
                },
            },
            {
                "name": "replay_langgraph_run",
                "description": (
                    "Re-derive a LangGraph run's state from what was recorded, "
                    "step by step. Reads only the checkpoints: no Handler runs "
                    "and nothing is written."
                ),
                "scope": READ_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                    "required": ["run_id"],
                },
            },
            {
                "name": "start_run",
                "description": "Start a published workflow using LangGraph.",
                "scope": WRITE_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workflow_id": {"type": "string"},
                        "workflow_version": {"type": "integer"},
                        "input": {"type": "object"},
                        "idempotency_key": {"type": "string"},
                    },
                    "required": ["workflow_id", "idempotency_key"],
                },
            },
            {
                "name": "resume_run",
                "description": "Resume an interrupted LangGraph run at its observed revision.",
                "scope": WRITE_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "value": {},
                        "interrupt_id": {"type": "string"},
                        "expected_version": {"type": "integer"},
                        "idempotency_key": {"type": "string"},
                    },
                    "required": [
                        "run_id", "expected_version", "idempotency_key",
                    ],
                },
            },
            {
                "name": "recover_run",
                "description": "Recover a LangGraph run left running after process loss.",
                "scope": OPS_WRITE_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {"run_id": {"type": "string"}},
                    "required": ["run_id"],
                },
            },
            {
                "name": "cancel_run",
                "description": "Cancel a LangGraph run at its observed revision.",
                "scope": WRITE_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "expected_version": {"type": "integer"},
                        "idempotency_key": {"type": "string"},
                    },
                    "required": [
                        "run_id", "expected_version", "idempotency_key",
                    ],
                },
            },
            {
                "name": "list_artifacts",
                "description": "List committed Artifacts produced by LangGraph runs.",
                "scope": READ_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                },
            },
            {
                "name": "read_artifact",
                "description": "Read metadata for one committed LangGraph Artifact.",
                "scope": READ_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {"artifact_id": {"type": "string"}},
                    "required": ["artifact_id"],
                },
            },
            {
                "name": "get_artifact_lineage",
                "description": "Read upstream and downstream LangGraph Artifact lineage.",
                "scope": READ_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {"artifact_id": {"type": "string"}},
                    "required": ["artifact_id"],
                },
            },
            {
                "name": "collect_artifacts",
                "description": "Garbage-collect a bounded set of abandoned LangGraph Artifacts.",
                "scope": OPS_WRITE_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                },
            },
        )
    by_name = {tool["name"]: tool for tool in tools}

    def langgraph_run_dto(run) -> dict[str, Any]:
        return {
            "run_id": run.run_id,
            "workflow_id": run.workflow_id,
            "workflow_version": run.workflow_version,
            "status": run.status,
            "revision": run.revision,
            "result": run.result,
            "interrupts": list(run.interrupts),
            "error": run.error,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
        }

    def call(name: str, arguments: Mapping[str, Any], actor: str) -> Any:
        if name == "list_runs":
            return {"runs": [
                langgraph_run_dto(run)
                for run in langgraph_service.list_runs(
                    status=arguments.get("status") or None,
                    limit=min(200, max(1, int(arguments.get("limit", 20)))),
                    actor=actor,
                )
            ]}
        if name == "inspect_run":
            return langgraph_run_dto(
                langgraph_service.get(str(arguments["run_id"]), actor=actor)
            )
        if name == "start_run":
            return langgraph_run_dto(langgraph_service.start(
                str(arguments["workflow_id"]), arguments.get("input") or {},
                workflow_version=arguments.get("workflow_version"),
                idempotency_key=str(arguments["idempotency_key"]),
                actor=actor,
            ))
        if name == "resume_run":
            return langgraph_run_dto(langgraph_service.resume(
                str(arguments["run_id"]), arguments.get("value"),
                expected_revision=int(arguments["expected_version"]),
                idempotency_key=str(arguments["idempotency_key"]),
                interrupt_id=arguments.get("interrupt_id"),
                actor=actor,
            ))
        if name == "replay_langgraph_run":
            return {"steps": list(langgraph_service.replay(
                str(arguments["run_id"]),
                actor=actor,
                limit=min(200, max(1, int(arguments.get("limit", 50)))),
            ))}
        if name == "recover_run":
            return langgraph_run_dto(
                langgraph_service.recover(str(arguments["run_id"]))
            )
        if name == "cancel_run":
            return langgraph_run_dto(langgraph_service.cancel(
                str(arguments["run_id"]),
                expected_revision=int(arguments["expected_version"]),
                idempotency_key=str(arguments["idempotency_key"]),
                actor=actor,
            ))
        if name == "list_artifacts":
            if getattr(langgraph_service, "artifacts", None) is None:
                raise LookupError("LangGraph Artifact store is unavailable")
            return {"artifacts": list(langgraph_service.artifacts.list(
                run_id=arguments.get("run_id") or None,
                limit=min(200, max(1, int(arguments.get("limit", 20)))),
                actor=actor,
            ))}
        if name == "read_artifact":
            if getattr(langgraph_service, "artifacts", None) is None:
                raise LookupError("LangGraph Artifact store is unavailable")
            return langgraph_service.artifacts.get(
                str(arguments["artifact_id"]), actor=actor,
            )
        if name == "get_artifact_lineage":
            if getattr(langgraph_service, "artifacts", None) is None:
                raise LookupError("LangGraph Artifact store is unavailable")
            return langgraph_service.artifacts.lineage(
                str(arguments["artifact_id"]), actor=actor,
            )
        if name == "collect_artifacts":
            if getattr(langgraph_service, "artifacts", None) is None:
                raise LookupError("LangGraph Artifact store is unavailable")
            collected = langgraph_service.artifacts.collect_abandoned(
                limit=min(200, max(1, int(arguments.get("limit", 100))))
            )
            return {"collected_artifact_ids": list(collected)}
        if name == "list_workflows":
            items = workflow_reads.list()
            if arguments.get("ready_only"):
                items = [
                    item for item in items if item["goal_readiness"] == "ready"
                ]
            # Deliberately a projection rather than the catalog entry: the full
            # entry carries the graph and every handler binding, which is a
            # page of JSON an agent choosing what to run does not read.
            return {
                "workflows": [
                    {
                        "workflow_id": item["workflow_id"],
                        "name": item["name"],
                        "description": item["description"],
                        "latest_version": item["latest_version"],
                        "goal_readiness": item["goal_readiness"],
                        "readiness_reason": item["readiness_reason"],
                        "input_mode": item["input_mode"],
                        "inputs": item["inputs"],
                        "goal_binding": item["goal_binding"],
                    }
                    for item in items
                ]
            }
        if name == "generate_workflow":
            if authoring_jobs is None:
                raise ValueError("workflow authoring is not configured")
            return authoring_jobs.create(
                actor=actor, prompt=str(arguments["prompt"]),
                idempotency_key=str(arguments["idempotency_key"]),
                agent=arguments.get("agent"),
                display_language=arguments.get("display_language"),
            )
        if name == "get_authoring_job":
            if authoring_jobs is None:
                raise ValueError("workflow authoring is not configured")
            return authoring_jobs.get(str(arguments["job_id"]), actor=actor)
        if name == "claim_authoring_request":
            if authoring_broker is None:
                raise ValueError("client-side workflow generation is not configured")
            claimed = authoring_broker.claim(
                actor=actor, client=arguments.get("client"),
            )
            # "Nothing waiting" is an answer, not a failure: a client polling
            # this must not have to read an error to learn it is idle. The
            # waiting list includes requests already claimed, so a client that
            # lost a request_id can find its own work again instead of leaving
            # a job parked until its deadline.
            return {
                "request": claimed,
                "waiting": authoring_broker.pending(),
                # Who else is connected, so a client can see the names an
                # author is being offered alongside its own.
                "clients": authoring_broker.clients(),
            }
        if name == "wait_authoring_request":
            if authoring_broker is None:
                raise ValueError("client-side workflow generation is not configured")
            claimed = authoring_broker.wait_claim(
                actor=actor,
                client=str(arguments["client"]),
                timeout_seconds=int(arguments.get("timeout_seconds", 120)),
            )
            return {
                "request": claimed,
                "waiting": authoring_broker.pending(),
                "clients": authoring_broker.clients(),
            }
        if name == "submit_authoring_response":
            if authoring_broker is None:
                raise ValueError("client-side workflow generation is not configured")
            return authoring_broker.respond(
                str(arguments["request_id"]), arguments["dsl"], actor=actor,
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
        except AuthoringJobConflict as exc:
            # The job the caller collided with is the useful part of the
            # refusal: it is usually their own earlier attempt.
            return _result(request_id, _content(
                {"error": exc.code, "job": exc.job}, is_error=True,
            ))
        except ValueError as exc:
            # A tool that fails on its own terms is a result, not a protocol
            # error: the caller is an agent that needs to read the reason.
            return _result(request_id, _content({"error": str(exc)}, is_error=True))
        except LookupError as exc:
            # Nonexistent and not-visible-to-you deliberately read the same,
            # here as over HTTP: an id an actor may not see must not be
            # confirmed to exist by the shape of the refusal.
            return _result(request_id, _content(
                {"error": str(exc) or "not found"}, is_error=True,
            ))
        except PermissionError as exc:
            return _failure(request_id, NOT_AUTHORIZED, str(exc))
        return _result(request_id, _content(payload))

    return dispatch


def build_mcp(
    db_path: Path | str,
    *,
    authenticator: Callable[[Request], str | None] | None = None,
    **kwargs: Any,
) -> list[Route]:
    """`/mcp` over HTTP, dispatcher included — for callers that need no handle
    on the dispatcher itself. The composition root builds the two separately so
    the stdio transport can share the one dispatcher rather than construct a
    second set of services against the same database."""

    return mcp_routes(
        build_mcp_dispatcher(db_path, **kwargs),
        authenticator=authenticator,
    )


def mcp_routes(
    dispatch: Callable[[Mapping[str, Any], str | None], dict[str, Any] | None],
    *,
    authenticator: Callable[[Request], str | None] | None = None,
) -> list[Route]:
    """`/mcp` over HTTP: one POST carrying one JSON-RPC message, or a batch."""

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
                if isinstance(item, Mapping)
                and (response := await asyncio.to_thread(dispatch, item, actor)) is not None
            ]
            return JSONResponse(responses) if responses else JSONResponse(None, status_code=202)
        if not isinstance(message, Mapping) or message.get("jsonrpc") != "2.0":
            return JSONResponse(_failure(None, INVALID_REQUEST, "expected a JSON-RPC 2.0 message"))

        response = await asyncio.to_thread(dispatch, message, actor)
        if response is None:
            return JSONResponse(None, status_code=202)
        return JSONResponse(response)

    return [Route("/mcp", endpoint, methods=["POST"])]


def serve_stdio(
    dispatch: Callable[[Mapping[str, Any], str | None], dict[str, Any] | None],
    actor: str,
    *,
    stdin=None,
    stdout=None,
) -> None:
    """Carry the same JSON-RPC over stdin/stdout until the client hangs up.

    Newline-delimited JSON, one message per line, which is what MCP stdio
    clients speak. Nothing but responses may ever reach stdout — a stray print
    would be read as a protocol message — so diagnostics belong on stderr.

    The actor is supplied rather than derived. Over HTTP the identity comes
    from the connection, and there is no connection here: whoever started this
    process is the caller, and the composition root is the only place that can
    say who that is.
    """

    import sys

    source = sys.stdin if stdin is None else stdin
    sink = sys.stdout if stdout is None else stdout

    try:
        _pump(dispatch, actor, source, sink)
    except BrokenPipeError:
        # The client closed the pipe. That is how an MCP session ends, not a
        # fault to report — and reporting it would mean writing to the pipe
        # that just went away.
        return


def _pump(dispatch, actor, source, sink) -> None:
    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _emit(sink, _failure(None, PARSE_ERROR, "each line must be JSON"))
            continue
        if isinstance(message, list):
            responses = [
                response for item in message
                if isinstance(item, Mapping)
                and (response := dispatch(item, actor)) is not None
            ]
            if responses:
                _emit(sink, responses)
            continue
        if not isinstance(message, Mapping) or message.get("jsonrpc") != "2.0":
            _emit(sink, _failure(None, INVALID_REQUEST, "expected a JSON-RPC 2.0 message"))
            continue
        response = dispatch(message, actor)
        if response is not None:
            # A notification gets no line at all: on this transport silence is
            # the whole of "no response", there being no 202 to send.
            _emit(sink, response)


def _emit(sink, payload: Any) -> None:
    sink.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sink.flush()
