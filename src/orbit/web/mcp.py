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

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..workflow.api.artifact_read_models import ArtifactReadModelService
from ..workflow.api.read_models import ReadModelService
from ..workflow.api.workflow_catalog import WorkflowCatalogReadModelService
from ..workflow.application.authoring_job_service import (
    AuthoringJobConflict, AuthoringJobService,
)
from ..workflow.application.human_service import HumanTaskService
from ..workflow.application.run_service import RunApplicationService, RunStartError
from ..workflow.catalogs import InMemorySchemaCatalog
from ..workflow.domain.ids import EntityId
from ..workflow.persistence.database import connect_workflow_database
from .api_v1 import (
    OPS_READ_SCOPE, PREVIEW_LIMIT_BYTES, READ_SCOPE, SENSITIVE_SCOPE,
    WRITE_SCOPE, Authorizer,
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
    durable_service,
    *,
    workflow_db_path: Path | str | None = None,
    authorizer: Authorizer | None = None,
    single_goal_mode: bool = True,
    schema_catalog=None,
    artifact_backend=None,
    authoring_service=None,
    workflow_publisher=None,
    authoring_jobs=None,
    authoring_broker=None,
) -> Callable[[Mapping[str, Any], str | None], dict[str, Any] | None]:
    """One JSON-RPC message in, at most one response out.

    Transport-free on purpose. HTTP and stdio are two ways of carrying the
    same bytes, and a second transport must not become a second implementation
    of the tools, the scopes or the idempotency rules — those live here, once.
    """

    path = Path(db_path)
    workflow_path = Path(workflow_db_path or db_path)
    reads = ReadModelService(path)
    runs = RunApplicationService(
        path, durable_service, enforce_single_goal=single_goal_mode,
        workflow_db_path=workflow_db_path,
    )
    workflow_reads = WorkflowCatalogReadModelService(
        workflow_path, schema_catalog or InMemorySchemaCatalog({}), usage_path=path,
    )
    artifact_backend = artifact_backend or getattr(
        durable_service, "artifact_backend", None
    )

    def read_preview_blob(blob_key: str) -> bytes:
        if artifact_backend is None:
            raise LookupError("Artifact store is unavailable")
        return artifact_backend.read(blob_key, max_size_bytes=PREVIEW_LIMIT_BYTES)

    artifact_reads = ArtifactReadModelService(path, blob_reader=read_preview_blob)
    humans = HumanTaskService(path)
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
    now = getattr(durable_service, "clock", None) or (
        lambda: datetime.now(timezone.utc)
    )

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
        # -- results ------------------------------------------------------
        # `inspect_run` says where a run is; these say what it produced. An
        # agent that can start work but cannot read the answer is a write-only
        # integration.
        {
            "name": "get_run_result",
            "description": "The declared result of a run, once it has one.",
            "scope": READ_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
        },
        {
            "name": "list_artifacts",
            "description": "Artifacts produced by runs, newest first.",
            "scope": READ_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "content_type": {"type": "string"},
                    "q": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
            },
        },
        {
            "name": "read_artifact",
            "description": "Metadata for one artifact: size, content type, origin.",
            "scope": READ_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {"artifact_id": {"type": "string"}},
                "required": ["artifact_id"],
            },
        },
        # -- human tasks --------------------------------------------------
        # A run that stops for a person looks identical to a stuck one from the
        # outside. These make the difference legible, and let an agent that is
        # itself a named participant answer.
        {
            "name": "list_inbox",
            "description": (
                "Open responsibilities: human tasks awaiting a decision, "
                "exhausted budgets, and unknown results awaiting a call."
            ),
            "scope": READ_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
            },
        },
        {
            "name": "request_human_task_token",
            "description": (
                "Mint this actor's submission token for a human task it "
                "participates in. Refused for anyone the task does not name."
            ),
            "scope": WRITE_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "expected_version": {"type": "integer"},
                },
                "required": ["task_id", "expected_version"],
            },
        },
        {
            "name": "submit_human_task",
            "description": (
                "Answer a human task: approve, reject, or supply its value. "
                "Needs the submission token from request_human_task_token."
            ),
            "scope": WRITE_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "submission_token": {"type": "string"},
                    "decision": {"type": "string"},
                    "value": {},
                    "expected_version": {"type": "integer"},
                    "idempotency_key": {"type": "string"},
                },
                "required": [
                    "task_id", "submission_token", "decision",
                    "expected_version", "idempotency_key",
                ],
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
        # A job whose chosen agent is `app:<client>` writes no DSL of its own:
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
                "addresses this App as `app:<client>`, and polling is what "
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
                "While waiting, the App is offered in Orbit as app:<client>. "
                "Use this when a person will click Generate in the Orbit UI."
            ),
            "scope": WRITE_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "client": {
                        "type": "string",
                        "description": "Stable App name used as app:<client> in Orbit.",
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
        # -- operations ---------------------------------------------------
        {
            "name": "runtime_status",
            "description": (
                "Whether the runtime itself is healthy: job, timer and lease "
                "counts by state."
            ),
            "scope": OPS_READ_SCOPE,
            "inputSchema": {"type": "object", "properties": {}},
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
        if name == "get_run_result":
            return reads.outcome(
                EntityId.parse(str(arguments["run_id"])), actor=actor,
                content_visible=guard.allows(actor, SENSITIVE_SCOPE),
            )
        if name == "list_artifacts":
            items, cursor = artifact_reads.list(
                actor,
                limit=min(200, max(1, int(arguments.get("limit", 50)))),
                q=str(arguments.get("q", "")),
                run_id=str(arguments.get("run_id", "")),
                content_type=str(arguments.get("content_type", "")),
                # A title is content. An actor without the sensitive scope gets
                # the same catalog without it, never a refusal for the page.
                with_titles=guard.allows(actor, SENSITIVE_SCOPE),
            )
            return {"artifacts": items, "next_cursor": cursor}
        if name == "read_artifact":
            return artifact_reads.detail(
                actor, EntityId.parse(str(arguments["artifact_id"])),
                with_title=guard.allows(actor, SENSITIVE_SCOPE),
            )
        if name == "list_inbox":
            items, _cursor = reads.inbox(
                limit=min(200, max(1, int(arguments.get("limit", 50)))),
                actor=actor,
            )
            return {"items": items, "action_count": sum(
                item["requires_actor_action"] for item in items
            )}
        if name == "request_human_task_token":
            # The service decides whether this actor may hold one; being able
            # to name the task is not the same as being named by it.
            return humans.reissue_token(
                EntityId.parse(str(arguments["task_id"])), actor=actor,
                expected_version=int(arguments["expected_version"]), now=now(),
            )
        if name == "submit_human_task":
            task_id = EntityId.parse(str(arguments["task_id"]))
            version = int(arguments["expected_version"])
            linked = humans.linked_scope(task_id)
            if linked is not None:
                # A task the graph is waiting on is answered through the
                # kernel, so the run advances in the same transaction.
                _node_run_id, run_id = linked
                return durable_service.submit_human_task(
                    task_id, run_id, version,
                    token=str(arguments["submission_token"]),
                    decision=str(arguments["decision"]),
                    value=arguments.get("value"), actor=actor,
                    idempotency_key=str(arguments["idempotency_key"]), now=now(),
                )
            status = humans.submit(
                task_id, str(arguments["submission_token"]),
                str(arguments["decision"]), arguments.get("value"),
                actor=actor, expected_version=version, now=now(),
            )
            return {
                "task_id": str(task_id),
                "decision": str(arguments["decision"]),
                "status": status.value,
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
        if name == "runtime_status":
            with connect_workflow_database(path, read_only=True) as connection:
                def counts(table: str) -> dict[str, int]:
                    return {
                        row["status"]: int(row["count"])
                        for row in connection.execute(
                            f"SELECT status, COUNT(*) AS count FROM {table}"
                            " GROUP BY status"
                        )
                    }

                return {
                    "jobs": counts("jobs"),
                    "timers": counts("durable_timers"),
                    "active_leases": int(connection.execute(
                        "SELECT COUNT(*) FROM job_leases WHERE status='active'"
                    ).fetchone()[0]),
                    "runs": counts("workflow_runs"),
                }
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
        except (RunStartError, ValueError) as exc:
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
    durable_service,
    *,
    authenticator: Callable[[Request], str | None] | None = None,
    **kwargs: Any,
) -> list[Route]:
    """`/mcp` over HTTP, dispatcher included — for callers that need no handle
    on the dispatcher itself. The composition root builds the two separately so
    the stdio transport can share the one dispatcher rather than construct a
    second set of services against the same database."""

    return mcp_routes(
        build_mcp_dispatcher(db_path, durable_service, **kwargs),
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
