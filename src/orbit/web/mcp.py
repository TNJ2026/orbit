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
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping

from orbit import __version__
from .run_visibility import reading_actor, writing_actor
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..workflow.api.workflow_catalog import WorkflowCatalogReadModelService
from ..workflow.application.authoring_job_service import (
    AuthoringJobConflict, AuthoringJobService,
)
from ..workflow.catalogs import InMemorySchemaCatalog
from ..workflow.api.routes import ApiCommandExecutor
from .api_v1 import (
    OPS_READ_SCOPE, OPS_WRITE_SCOPE, READ_SCOPE, SENSITIVE_SCOPE, WRITE_SCOPE, Authorizer,
)
from ..workflow.application.authoring_job_service import authoring_is_active
from .run_projection import langgraph_run_dto
from .mcp_app import (
    ORBIT_AUTHORING_URI, ORBIT_DASHBOARD_MIME_TYPE, ORBIT_DASHBOARD_URI,
    ORBIT_GOALS_URI, ORBIT_MCP_APP_RESOURCES, ORBIT_RUN_URI, ORBIT_WORKFLOWS_URI,
)

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "orbit", "version": "1.0"}

# JSON-RPC reserved codes; -32001 is our application-level refusal.
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
PARSE_ERROR = -32700
NOT_AUTHORIZED = -32001

MCP_SESSION_PRESENCE_SECONDS = 60.0
MCP_TOOL_PROFILES = frozenset({"full", "harness"})
HARNESS_TOOL_NAMES = frozenset({
    "get_capabilities", "list_workflows", "inspect_workflows",
    "get_workflow_definition", "inspect_workflow_definition", "list_agents",
    "delete_workflow",
    "list_runs", "inspect_run",
    "replay_langgraph_run",
    "generate_workflow", "modify_workflow", "get_authoring_job",
    "list_authoring_jobs", "read_authoring_output",
    # The Host stands on the authoring queue with these, which is what makes it
    # a writer Orbit will pick over forking an Agent CLI. Without them the
    # preference has nothing to prefer: being connected does not put a client
    # on the queue, waiting does, and a profile that hides the wait leaves the
    # Host permanently absent from it.
    "register_authoring_client", "wait_authoring_request",
    "submit_authoring_response",
    "list_runtime_events", "get_run_steps", "get_run_graph", "get_run_edges",
    "read_run_output",
    "start_run", "resume_run", "cancel_run", "list_artifacts",
    "read_artifact", "read_artifact_content", "get_artifact_lineage",
    "configure_execution_lease", "claim_delegation", "renew_delegation",
    "complete_delegation", "reconcile_delegation", "get_delegation_stats",
})
OBJECT_OUTPUT_SCHEMA = {"type": "object"}
MCP_ARTIFACT_CONTENT_MAX_BYTES = 2 * 1024 * 1024


class McpSessionRegistry:
    """Which MCP clients the Runtime has heard from, and when.

    The HTTP transport has no connection to watch, so presence is observed,
    never declared: a client counts as connected for `presence_seconds` after
    its last message, the same rule the authoring broker applies to its
    pollers. Sessions are keyed by actor — the one identity every message
    already carries — with ``anonymous`` standing in for calls that arrive
    without credentials, since those can still handshake and discover tools.
    """

    def __init__(
        self,
        *,
        presence_seconds: float = MCP_SESSION_PRESENCE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self.presence_seconds = float(presence_seconds)
        self.clock = clock
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))

    def observe(
        self, actor: str | None, method: str, params: Mapping[str, Any],
    ) -> None:
        """One JSON-RPC message from a client: create or refresh its session.

        `initialize` is the only message that says who the client is; every
        message says it is still there. Both facts are worth keeping.
        """

        key = actor if actor else "anonymous"
        client_info = None
        protocol_version = None
        if method == "initialize" and isinstance(params, Mapping):
            info = params.get("clientInfo")
            if isinstance(info, Mapping):
                client_info = {
                    "name": str(info.get("name", "")),
                    "version": str(info.get("version", "")),
                }
            declared = params.get("protocolVersion")
            protocol_version = None if declared is None else str(declared)
        seen = self.clock()
        stamp = self.wall_clock().isoformat()
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                session = {
                    "session_id": key, "actor": actor,
                    "client": None, "protocol_version": None,
                    "connected_at": stamp, "requests": 0,
                }
                self._sessions[key] = session
            session["seen"] = seen
            session["last_seen"] = stamp
            session["last_method"] = method
            session["requests"] += 1
            if client_info is not None:
                session["client"] = client_info
            if protocol_version is not None:
                session["protocol_version"] = protocol_version

    def sessions(self) -> list[dict[str, Any]]:
        """Every session seen, most recent first, with a `connected` verdict."""

        cutoff = self.clock() - self.presence_seconds
        with self._lock:
            sessions = list(self._sessions.values())
        return [
            {
                key: value for key, value in session.items() if key != "seen"
            }
            | {"connected": session["seen"] > cutoff}
            for session in sorted(
                sessions, key=lambda item: item["last_seen"], reverse=True,
            )
        ]


def _result(request_id: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _failure(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


# One tool, deliberately, and not the six that draw a card. Shortening the
# text block for all of them at once is what went wrong the first time: the
# model reads that block too, and with the names gone from a listing it went
# and fetched every definition separately, mounting a card for each. What has
# changed since is `inspect_workflows` — a documented way to get the same
# catalogue with nothing drawn — so the summary can name it and the model has
# somewhere to go. Whether it actually goes there is the thing being watched;
# until that is known, this stays at one tool.
SUMMARISED_TOOLS = {"list_workflows"}


def _summary(payload: Any) -> str:
    """What the card is showing, in a sentence, and where the values are."""

    count = len(payload.get("workflows", [])) if isinstance(payload, Mapping) else 0
    return json.dumps({
        "shown_in_card": count,
        "note": (
            "The workflows are drawn in the card beside this. Call "
            "`inspect_workflows` for the same catalogue as values."
        ),
    }, ensure_ascii=False)


def _content(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    """Return modern structured content and the legacy JSON text together.

    Both carry the whole payload, and a card-bound tool is no exception.
    Summarising the text block there was tried and reverted: a host that
    mounts the card prints the answer twice, which is what it was meant to
    fix, but the model reads the same text block — and with the names gone
    from a workflow listing it went and fetched each definition one at a
    time, mounting a card for every one. Six cards is worse than one table.
    """

    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False, sort_keys=True)}
        ],
        "structuredContent": payload,
        "isError": is_error,
    }


def workflow_id_argument(arguments: Mapping[str, Any]) -> str:
    """The workflow id as Orbit stores it, however the caller wrote it.

    Every published id is namespaced — `workflow:wf_…` — and every surface
    that hands one back says so. An Agent given one reliably drops the
    prefix: `workflow:` reads as a label on the value rather than part of
    it, so the id it passes back is the id it was shown minus its kind. That
    cost a real run three failed starts and a detour through the source,
    because the miss was reported as a missing *version* of a workflow whose
    every version was in fact there.

    Restoring the prefix is unambiguous here — a workflow id is the only kind
    of id these tools take — and a caller that spells it in full is untouched.
    """

    raw = str(arguments["workflow_id"]).strip()
    if not raw:
        raise ValueError("workflow_id is required")
    return raw if raw.startswith("workflow:") else f"workflow:{raw}"


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
    session_registry: McpSessionRegistry | None = None,
    execution_registry=None,
    tool_profile: str = "full",
    delegation_queue=None,
) -> Callable[[Mapping[str, Any], str | None], dict[str, Any] | None]:
    """One JSON-RPC message in, at most one response out.

    Transport-free on purpose. HTTP and stdio are two ways of carrying the
    same bytes, and a second transport must not become a second implementation
    of the tools, the scopes or the idempotency rules — those live here, once.
    """

    if tool_profile not in MCP_TOOL_PROFILES:
        raise ValueError(
            f"unknown MCP tool profile {tool_profile!r}; expected full or harness"
        )
    path = Path(db_path)
    workflow_path = Path(workflow_db_path or db_path)
    workflow_reads = WorkflowCatalogReadModelService(
        workflow_path, schema_catalog or InMemorySchemaCatalog({}),
        usage_source=getattr(langgraph_service, "workflow_usage", None),
    )
    command_executor = ApiCommandExecutor(path)
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
    sessions = session_registry or McpSessionRegistry()

    tools = (
        {
            "name": "get_capabilities",
            "description": "Report Orbit and integration protocol capabilities.",
            "scope": READ_SCOPE,
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "open_orbit_dashboard",
            "description": (
                "Open Orbit's compact current-task card beside the conversation. "
                "It shows live progress and attention state; use the full UI for "
                "catalogs, history, logs, and workflow management."
            ),
            "scope": READ_SCOPE,
            "inputSchema": {"type": "object", "properties": {}},
            "_meta": {"ui": {"resourceUri": ORBIT_DASHBOARD_URI}},
        },
        {
            "name": "open_orbit_goals",
            "description": (
                "Open Orbit's recent goals card beside the conversation. "
                "It shows goal runs and their current status without opening the full UI."
            ),
            "scope": READ_SCOPE,
            "inputSchema": {"type": "object", "properties": {}},
            "_meta": {"ui": {"resourceUri": ORBIT_GOALS_URI}},
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
                "Show the published workflows to the person, as a card. Use "
                "this when they asked to see what is available. To choose or "
                "filter one yourself, call `inspect_workflows` instead: it "
                "returns the same catalogue without opening a card, and a card "
                "per question is what fills a conversation with them."
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
            "_meta": {"ui": {"resourceUri": ORBIT_WORKFLOWS_URI}},
        },
        {
            "name": "get_workflow_definition",
            "description": (
                "The published steps of one workflow, in the order a reader "
                "meets them: what each does, which Agent runs it, and the "
                "prompt it was authored with."
            ),
            "scope": READ_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {"workflow_id": {"type": "string"}},
                "required": ["workflow_id"],
            },
            # The definition is a view inside the workflow-list App. Binding
            # the read to that same resource lets the mounted card call it;
            # there is deliberately no separate workflow-detail resource.
            "_meta": {"ui": {"resourceUri": ORBIT_WORKFLOWS_URI}},
        },
        {
            "name": "inspect_workflows",
            "description": (
                "The published workflows for goal resolution and filtering, "
                "without opening the workflow App card. Same catalogue as "
                "`list_workflows`; this is the one to call when the answer is "
                "yours to work out rather than the person's to look at."
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
        {
            "name": "inspect_workflow_definition",
            "description": (
                "Read one published workflow definition for goal resolution and "
                "validation without opening the workflow-detail App card."
            ),
            "scope": READ_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {"workflow_id": {"type": "string"}},
                "required": ["workflow_id"],
            },
        },
        {
            "name": "delete_workflow",
            "description": (
                "Delete one published workflow after explicit user confirmation. "
                "Requires the currently observed latest version and an idempotency key."
            ),
            "scope": WRITE_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "expected_version": {"type": "integer", "minimum": 1},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["workflow_id", "expected_version", "idempotency_key"],
            },
        },
        {
            "name": "list_agents",
            "description": (
                "Agent handlers this Runtime registered at startup, with the "
                "node kinds each may run. The set is sealed when the Runtime "
                "starts, so it does not change while one is up."
            ),
            "scope": READ_SCOPE,
            "inputSchema": {"type": "object", "properties": {}},
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
            "_meta": {"ui": {"resourceUri": ORBIT_AUTHORING_URI}},
        },
        {
            "name": "modify_workflow",
            "description": (
                "Modify or regenerate one published workflow through an asynchronous "
                "authoring job. Poll get_authoring_job for the outcome."
            ),
            "scope": WRITE_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "prompt": {"type": "string"},
                    "mode": {"type": "string", "enum": ["modify", "regenerate"]},
                    "agent": {"type": "string"},
                    "display_language": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["workflow_id", "prompt", "idempotency_key"],
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
        {
            "name": "list_authoring_jobs",
            "description": (
                "Recent workflow authoring jobs owned by this actor, including "
                "generation, validation and publication status."
            ),
            "scope": READ_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "active": {"type": "boolean"},
                    "type": {"type": "string", "enum": ["generate", "modify"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            },
        },
        {
            "name": "read_authoring_output",
            "description": (
                "Follow sensitive Agent console and progress output for one "
                "actor-owned workflow authoring job."
            ),
            "scope": SENSITIVE_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "after": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                },
                "required": ["job_id"],
            },
        },
        # A job whose chosen agent is a connected App writes no DSL of its own:
        # it parks the prompt here and waits for that App to answer. These two
        # tools are that exchange: registration makes an exact delivery route
        # addressable without taking somebody else's work, then each compile
        # attempt needs one claim/submit round-trip.
        {
            "name": "register_authoring_client",
            "description": (
                "Register an authoring delivery address without claiming any "
                "queued request. Use this immediately before creating a job "
                "that explicitly targets the address."
            ),
            "scope": WRITE_SCOPE,
            "inputSchema": {
                "type": "object",
                "properties": {"client": {"type": "string"}},
                "required": ["client"],
            },
        },
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
                "description": (
                    "List workflow runs executed by LangGraph. Yours by "
                    "default; pass owner=workspace for every run this Runtime "
                    "holds, which is what its own UI shows."
                ),
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
                    "properties": {"run_id": {"type": "string"},},
                    "required": ["run_id"],
                },
            },
            {
                "name": "get_run_steps",
                "description": "Read the derived step summaries for one Run.",
                "scope": READ_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {"run_id": {"type": "string"},},
                    "required": ["run_id"],
                },
            },
            {
                "name": "get_run_graph",
                "description": "Read the immutable graph snapshot executed by one Run.",
                "scope": READ_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {"run_id": {"type": "string"},},
                    "required": ["run_id"],
                },
            },
            {
                "name": "get_run_edges",
                "description": "Read the derived edge decisions for one Run.",
                "scope": READ_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {"run_id": {"type": "string"},},
                    "required": ["run_id"],
                },
            },
            {
                "name": "read_run_output",
                "description": "Follow sensitive Handler console output after a chunk cursor.",
                "scope": SENSITIVE_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "after": {"type": "integer", "minimum": 0},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                        "node_id": {"type": "string"},
                    },
                    "required": ["run_id"],
                },
            },
            {
                "name": "list_runtime_events",
                "description": "Read actor-scoped Runtime event hints after a position.",
                "scope": READ_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "after_position": {"type": "integer", "minimum": 0},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
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
                        "goal": {"type": "string", "maxLength": 4000},
                        "wait": {"type": "boolean", "default": True},
                        "idempotency_key": {"type": "string"},
                    },
                    "required": ["workflow_id", "idempotency_key"],
                },
                "_meta": {"ui": {"resourceUri": ORBIT_RUN_URI}},
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
                "name": "read_artifact_content",
                "description": "Read one small committed Artifact as bounded base64 content.",
                "scope": READ_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "artifact_id": {"type": "string"},
                        "max_bytes": {
                            "type": "integer", "minimum": 1,
                            "maximum": MCP_ARTIFACT_CONTENT_MAX_BYTES,
                        },
                    },
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
    if delegation_queue is not None:
        tools += (
            {
                "name": "configure_execution_lease",
                "description": (
                    "Pin the actor's Harness Provider allowlist and delegation "
                    "budgets before leasing work."
                ),
                "scope": WRITE_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "lease_id": {"type": "string"},
                        "workspace_id": {"type": "string"},
                        "allowed_providers": {
                            "type": "array", "items": {"type": "string"},
                        },
                        "max_delegations": {
                            "type": "integer", "minimum": 1, "maximum": 10000,
                        },
                        "max_wall_seconds": {
                            "type": "integer", "minimum": 1, "maximum": 7200,
                        },
                        "expires_at": {"type": "string"},
                    },
                    "required": [
                        "lease_id", "workspace_id", "allowed_providers",
                        "max_delegations", "max_wall_seconds", "expires_at",
                    ],
                },
            },
            {
                "name": "claim_delegation",
                "description": "Lease the oldest queued Harness subagent delegation.",
                "scope": WRITE_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "worker_id": {"type": "string"},
                        "lease_seconds": {"type": "integer", "minimum": 5, "maximum": 300},
                    },
                    "required": ["worker_id"],
                },
            },
            {
                "name": "renew_delegation",
                "description": "Renew a Harness delegation lease and observe cancellation.",
                "scope": WRITE_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "delegation_id": {"type": "string"}, "worker_id": {"type": "string"},
                        "lease_seconds": {"type": "integer", "minimum": 5, "maximum": 300},
                    },
                    "required": ["delegation_id", "worker_id"],
                },
            },
            {
                "name": "complete_delegation",
                "description": "Settle a leased Harness delegation exactly once.",
                "scope": WRITE_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "delegation_id": {"type": "string"}, "worker_id": {"type": "string"},
                        "result": {}, "error": {"type": "string"},
                    },
                    "required": ["delegation_id", "worker_id"],
                },
            },
            {
                "name": "reconcile_delegation",
                "description": (
                    "Record a human verdict for an unknown Harness delegation. "
                    "This never retries or rewrites the original attempt."
                ),
                "scope": WRITE_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "delegation_id": {"type": "string"},
                        "outcome": {"type": "string", "enum": [
                            "confirmed_succeeded", "confirmed_failed",
                        ]},
                        "note": {"type": "string", "maxLength": 4000},
                        "idempotency_key": {"type": "string"},
                    },
                    "required": ["delegation_id", "outcome", "idempotency_key"],
                },
            },
            {
                "name": "get_delegation_stats",
                "description": "Read actor-scoped Harness delegation counts.",
                "scope": READ_SCOPE,
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "prune_delegations",
                "description": "Delete bounded old terminal Harness delegations.",
                "scope": OPS_WRITE_SCOPE,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "before": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                    },
                    "required": ["before"],
                },
            },
        )
    tools = tuple(
        {**tool, "outputSchema": OBJECT_OUTPUT_SCHEMA}
        for tool in tools
        if tool_profile == "full" or tool["name"] in HARNESS_TOOL_NAMES
    )
    by_name = {tool["name"]: tool for tool in tools}

    def call(name: str, arguments: Mapping[str, Any], actor: str) -> Any:
        if name == "get_capabilities":
            return {
                "orbit_version": __version__,
                "integration_protocol": "orbit-harness/1",
                "mcp_protocol": PROTOCOL_VERSION,
                "event_schemas": ["langgraph_run/1", "langgraph_node/1"],
                "tool_profile": tool_profile,
            }
        name = {
            "open_orbit_dashboard": "list_runs",
            "open_orbit_goals": "list_runs",
        }.get(name, name)
        if name == "list_runs":
            owner = reading_actor(actor)
            # Not a widening of scope: every actor reaching this transport is
            # the same local operator, and the Runtime's own UI already shows
            # all of it. What the default protects is an Agent's account of its
            # own work, which a Workspace-wide list would bury.
            return {"runs": [
                langgraph_run_dto(
                    run, can_write=guard.allows(actor, WRITE_SCOPE),
                )
                for run in langgraph_service.list_runs(
                    status=arguments.get("status") or None,
                    limit=min(200, max(1, int(arguments.get("limit", 20)))),
                    actor=owner,
                )
            ]}
        if name == "configure_execution_lease":
            return {"execution_lease": delegation_queue.configure_execution_lease(
                actor=actor, lease_id=str(arguments["lease_id"]),
                workspace_id=str(arguments["workspace_id"]),
                allowed_providers=arguments["allowed_providers"],
                max_delegations=int(arguments["max_delegations"]),
                max_wall_seconds=int(arguments["max_wall_seconds"]),
                expires_at=str(arguments["expires_at"]),
            )}
        if name == "claim_delegation":
            return {"delegation": delegation_queue.claim(
                actor=actor, worker_id=str(arguments["worker_id"]),
                lease_seconds=int(arguments.get("lease_seconds", 30)),
            )}
        if name == "renew_delegation":
            return {"delegation": delegation_queue.renew(
                str(arguments["delegation_id"]), actor=actor,
                worker_id=str(arguments["worker_id"]),
                lease_seconds=int(arguments.get("lease_seconds", 30)),
            )}
        if name == "complete_delegation":
            return {"delegation": delegation_queue.complete(
                str(arguments["delegation_id"]), actor=actor,
                worker_id=str(arguments["worker_id"]),
                result=arguments.get("result"), error=arguments.get("error"),
            )}
        if name == "reconcile_delegation":
            return {"reconciliation": delegation_queue.reconcile(
                str(arguments["delegation_id"]), actor=actor,
                outcome=str(arguments["outcome"]),
                note=str(arguments.get("note", "")),
                idempotency_key=str(arguments["idempotency_key"]),
            )}
        if name == "get_delegation_stats":
            return {"delegations": delegation_queue.stats(actor=actor)}
        if name == "prune_delegations":
            return delegation_queue.prune(
                before=str(arguments["before"]),
                limit=int(arguments.get("limit", 100)),
            )
        if name == "inspect_run":
            return langgraph_run_dto(
                langgraph_service.get(str(arguments["run_id"]), actor=reading_actor(actor)),
                can_write=guard.allows(actor, WRITE_SCOPE),
            )
        if name == "get_run_steps":
            run_id = str(arguments["run_id"])
            langgraph_service.get(run_id, actor=reading_actor(actor))
            steps = list(langgraph_service.steps(run_id, actor=reading_actor(actor)))
            if delegation_queue is not None:
                steps = [dict(step) for step in steps]
                for step in steps:
                    resolution = step.get("resolution")
                    delegation_id = (
                        resolution.get("delegation_id")
                        if isinstance(resolution, Mapping) else None
                    )
                    if delegation_id:
                        decision = delegation_queue.reconciliation(
                            str(delegation_id), actor=actor,
                        )
                        if decision is not None:
                            step["reconciliation"] = decision
            return {"steps": steps}
        if name == "get_run_graph":
            run_id = str(arguments["run_id"])
            return {"graph": langgraph_service.graph(run_id, actor=reading_actor(actor))}
        if name == "get_run_edges":
            run_id = str(arguments["run_id"])
            return {"edges": list(langgraph_service.edges(run_id, actor=reading_actor(actor)))}
        if name == "read_run_output":
            run_id = str(arguments["run_id"])
            langgraph_service.get(run_id, actor=reading_actor(actor))
            after = int(arguments.get("after", 0))
            console = getattr(langgraph_service, "console", None)
            if console is None:
                chunks, position, has_more = [], after, False
            else:
                chunks, position, has_more = console.read(
                    run_id, after_chunk_id=after,
                    limit=min(500, max(1, int(arguments.get("limit", 200)))),
                    node_id=arguments.get("node_id") or None,
                )
            return {"chunks": chunks, "after": position, "has_more": has_more}
        if name == "list_runtime_events":
            supplied = arguments.get("after_position")
            position = (
                langgraph_service.events_head() if supplied is None
                else int(supplied)
            )
            events = langgraph_service.events_after(
                position,
                limit=min(200, max(1, int(arguments.get("limit", 200)))),
                actor=actor,
            )
            return {
                "events": list(events),
                "next_position": events[-1]["position"] if events else position,
            }
        if name == "start_run":
            wait = arguments.get("wait")
            if wait is not None and not isinstance(wait, bool):
                raise ValueError("wait must be true or false")
            return langgraph_run_dto(
                langgraph_service.start(
                    workflow_id_argument(arguments), arguments.get("input") or {},
                    workflow_version=arguments.get("workflow_version"),
                    idempotency_key=str(arguments["idempotency_key"]),
                    actor=actor, goal=str(arguments.get("goal") or ""),
                    wait=True if wait is None else wait,
                ),
                can_write=True,
            )
        if name == "resume_run":
            return langgraph_run_dto(
                langgraph_service.resume(
                    str(arguments["run_id"]), arguments.get("value"),
                    expected_revision=int(arguments["expected_version"]),
                    idempotency_key=str(arguments["idempotency_key"]),
                    interrupt_id=arguments.get("interrupt_id"),
                    actor=writing_actor(actor),
                ),
                can_write=True,
            )
        if name == "replay_langgraph_run":
            return {"steps": list(langgraph_service.replay(
                str(arguments["run_id"]),
                # A replay reads what a Run did, and `/api/v1` has always read
                # it that way. Two transports disagreeing about one database is
                # the thing `run_visibility` exists to stop.
                actor=reading_actor(actor),
                limit=min(200, max(1, int(arguments.get("limit", 50)))),
            ))}
        if name == "recover_run":
            return langgraph_run_dto(
                langgraph_service.recover(str(arguments["run_id"])),
                can_write=True,
            )
        if name == "cancel_run":
            return langgraph_run_dto(
                langgraph_service.cancel(
                    str(arguments["run_id"]),
                    expected_revision=int(arguments["expected_version"]),
                    idempotency_key=str(arguments["idempotency_key"]),
                    actor=writing_actor(actor),
                ),
                can_write=True,
            )
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
        if name == "read_artifact_content":
            if getattr(langgraph_service, "artifacts", None) is None:
                raise LookupError("LangGraph Artifact store is unavailable")
            artifact_id = str(arguments["artifact_id"])
            metadata = langgraph_service.artifacts.get(artifact_id, actor=actor)
            limit = min(
                MCP_ARTIFACT_CONTENT_MAX_BYTES,
                max(1, int(arguments.get(
                    "max_bytes", MCP_ARTIFACT_CONTENT_MAX_BYTES,
                ))),
            )
            if int(metadata["size_bytes"]) > limit:
                raise ValueError(
                    f"Artifact is too large for MCP content proxy ({metadata['size_bytes']} > {limit})"
                )
            content = langgraph_service.artifacts.read(artifact_id, actor=actor)
            return {
                "artifact": metadata,
                "encoding": "base64",
                "content": base64.b64encode(content).decode("ascii"),
            }
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
        if name in {"get_workflow_definition", "inspect_workflow_definition"}:
            detail = workflow_reads.detail(workflow_id_argument(arguments))
            graph = detail.get("graph") or {}
            layout = {
                spot["node_id"]: spot
                for spot in (graph.get("layout") or {}).get("positions") or ()
            }
            configs = {
                node["id"]: node.get("config") or {}
                for node in (detail.get("definition") or {}).get("nodes") or ()
            }
            # Read in the order the diagram lays them out, which is the order a
            # person meets the steps — not the order the IR happens to store.
            def place(node):
                spot = layout.get(node["node_id"]) or {}
                return (spot.get("depth", 0), spot.get("lane", 0), node["node_id"])
            return {
                "workflow_id": detail["workflow_id"],
                "name": detail["name"],
                "description": detail["description"],
                "latest_version": detail["latest_version"],
                "goal_readiness": detail["goal_readiness"],
                "readiness_reason": detail["readiness_reason"],
                # A deleted workflow still reads: its Runs did not go anywhere.
                # Said out loud so a caller holding the id knows why starting
                # it will be refused, rather than finding out from `start_run`.
                "archived": detail["archived"],
                "input_mode": detail["input_mode"],
                "inputs": detail["inputs"],
                "goal_binding": detail["goal_binding"],
                "graph": graph,
                "nodes": [
                    {
                        "node_id": node["node_id"],
                        "label": node.get("label") or node["node_id"],
                        "kind": node["kind"],
                        "handler": node.get("handler_name"),
                        "prompt": str(
                            configs.get(node["node_id"], {}).get("prompt") or ""
                        ),
                    }
                    for node in sorted(graph.get("nodes") or (), key=place)
                ],
            }
        if name == "list_agents":
            # Identity only, like the HTTP catalog it mirrors: no config
            # schema, no secrets, nothing a caller could assemble into a
            # command. A Runtime with no registry answers with none rather
            # than refusing — an unsealed registry is a startup state, not an
            # error the caller can act on.
            registered = (
                execution_registry.entries()
                if execution_registry is not None and execution_registry.sealed
                else ()
            )
            attempt_stats = (
                langgraph_service.handler_attempts()
                if getattr(langgraph_service, "handler_attempts", None) is not None
                else {}
            )
            return {
                "agents": [
                    {
                        "name": entry.manifest.name,
                        "version": entry.manifest.version,
                        "node_kinds": list(entry.manifest.node_kinds),
                        "attempt_count": int(
                            attempt_stats.get(entry.manifest.name, {}).get("total", 0)
                        ),
                        "failed_count": int(
                            attempt_stats.get(entry.manifest.name, {}).get("failed", 0)
                        ),
                    }
                    for entry in registered
                    if entry.manifest.name.startswith("agent.")
                ],
            }
        if name in {"list_workflows", "inspect_workflows"}:
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
                        # What shape this workflow is, which is what a reader
                        # sizes one up by: a count and a tally of kinds, not
                        # the graph that would say the same at a hundred times
                        # the size.
                        "node_count": item["summary"]["node_count"],
                        "node_kinds": item["summary"]["node_kinds"],
                    }
                    for item in items
                ]
            }
        if name == "delete_workflow":
            if workflow_publisher is None:
                raise ValueError("workflow deletion is not configured")
            workflow_id = workflow_id_argument(arguments)
            expected_version = int(arguments["expected_version"])
            # The same refusal `/api/v1` has always made. Without it an Agent
            # could delete a Workflow its own `modify` job was mid-way through,
            # and the job then died on a retired id — the rare restart-gap the
            # job service re-checks for, made the ordinary path over this door.
            if authoring_is_active(db_path, workflow_id):
                raise ValueError("workflow authoring is still active")

            def delete(_body, _actor, _key):
                workflow_publisher.delete_workflow(
                    workflow_id, expected_latest_version=expected_version,
                )
                return {"workflow_id": workflow_id, "deleted": True}

            _status, result = command_executor.execute(
                actor=actor,
                idempotency_key=str(arguments["idempotency_key"]),
                method="DELETE",
                request_path=f"/mcp/workflows/{workflow_id}",
                body={"expected_version": expected_version},
                handler=delete,
            )
            return result
        if name == "generate_workflow":
            if authoring_jobs is None:
                raise ValueError("workflow authoring is not configured")
            return authoring_jobs.create(
                actor=actor, prompt=str(arguments["prompt"]),
                idempotency_key=str(arguments["idempotency_key"]),
                agent=arguments.get("agent"),
                display_language=arguments.get("display_language"),
            )
        if name == "modify_workflow":
            if authoring_jobs is None:
                raise ValueError("workflow authoring is not configured")
            return authoring_jobs.create(
                actor=actor, workflow_id=workflow_id_argument(arguments),
                prompt=str(arguments["prompt"]),
                mode=str(arguments.get("mode", "modify")),
                idempotency_key=str(arguments["idempotency_key"]),
                agent=arguments.get("agent"),
                display_language=arguments.get("display_language"),
            )
        if name == "get_authoring_job":
            if authoring_jobs is None:
                raise ValueError("workflow authoring is not configured")
            return authoring_jobs.get(str(arguments["job_id"]), actor=actor)
        if name == "list_authoring_jobs":
            if authoring_jobs is None:
                return {"jobs": []}
            jobs = authoring_jobs.list(
                actor=actor,
                active_only=bool(arguments.get("active", False)),
                job_type=arguments.get("type") or None,
            )
            limit = min(100, max(1, int(arguments.get("limit", 20))))
            return {"jobs": jobs[:limit]}
        if name == "read_authoring_output":
            if authoring_jobs is None:
                raise ValueError("workflow authoring is not configured")
            job_id = str(arguments["job_id"])
            # The ownership check deliberately comes before the console read:
            # a job another actor owns looks exactly like one that is absent.
            authoring_jobs.get(job_id, actor=actor)
            after = int(arguments.get("after", 0))
            chunks, next_after = authoring_jobs.output(
                job_id,
                after_chunk_id=after,
                limit=min(500, max(1, int(arguments.get("limit", 200)))),
            )
            return {
                "chunks": chunks,
                "next_after": next_after,
                "has_more": next_after is not None,
            }
        if name == "register_authoring_client":
            if authoring_broker is None:
                raise ValueError("client-side workflow generation is not configured")
            client = authoring_broker.touch(str(arguments["client"]))
            return {"client": client, "registered": True}
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

        # Every well-formed message is a sign of life, including the
        # notifications that get no response.
        sessions.observe(actor, str(method or ""), params)

        if method == "initialize":
            return _result(request_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"listChanged": False},
                },
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
        if method == "resources/list":
            return _result(request_id, {"resources": [
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
        # Declared `resources`, so a client is entitled to ask how they are
        # addressed. Orbit's are five fixed `ui://` documents with nothing
        # templated about them, and the answer to that is an empty list —
        # not METHOD_NOT_FOUND, which reads to a host as a resource surface
        # that does not work and takes the panels down with it. Observed:
        # WorkBuddy 5.4.2 asks for this immediately after `resources/list`.
        if method == "resources/templates/list":
            return _result(request_id, {"resourceTemplates": []})
        if method == "resources/read":
            resource = next(
                (
                    item for item in ORBIT_MCP_APP_RESOURCES
                    if item["uri"] == params.get("uri")
                ),
                None,
            )
            if resource is None:
                return _failure(request_id, INVALID_PARAMS, "unknown resource")
            return _result(request_id, {"contents": [{
                "uri": resource["uri"],
                "mimeType": ORBIT_DASHBOARD_MIME_TYPE,
                "text": resource["html"],
                "_meta": {
                    "ui": {"prefersBorder": resource["prefers_border"]},
                    "openai/widgetPrefersBorder": resource["prefers_border"],
                },
            }]})
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
        if name in SUMMARISED_TOOLS:
            # The card carries the answer; the text block would carry it a
            # second time and the host prints both.
            return _result(request_id, {
                "content": [{"type": "text", "text": _summary(payload)}],
                "structuredContent": payload,
                "isError": False,
            })
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


def agent_tool_routes(
    dispatch: Callable[[Mapping[str, Any], str | None], dict[str, Any] | None],
    *,
    authenticator: Callable[[Request], str | None] | None = None,
    path: str = "/internal/v1/agent-tools",
) -> list[Route]:
    """Private HTTP adapter for the MCP Gateway's workspace tool backend.

    This deliberately isn't an MCP endpoint: the fixed Hub owns JSON-RPC,
    handshakes, resources, notifications, and client sessions.  A workspace
    Runtime receives only a small domain envelope asking it to describe or
    invoke its registered Agent tools.
    """

    async def endpoint(request: Request) -> JSONResponse:
        actor = None if authenticator is None else authenticator(request)
        if actor is not None and not actor.strip():
            actor = None
        try:
            envelope = json.loads(await request.body() or b"")
        except json.JSONDecodeError:
            return JSONResponse({"error": "request body must be JSON"}, status_code=400)
        if not isinstance(envelope, Mapping):
            return JSONResponse({"error": "expected an object"}, status_code=400)

        operation = envelope.get("operation")
        if operation == "list":
            message = {
                "jsonrpc": "2.0", "id": "internal", "method": "tools/list",
                "params": {},
            }
        elif operation == "call":
            message = {
                "jsonrpc": "2.0", "id": "internal", "method": "tools/call",
                "params": {
                    "name": envelope.get("name", ""),
                    "arguments": envelope.get("arguments") or {},
                },
            }
        else:
            return JSONResponse({"error": "unknown operation"}, status_code=400)

        response = await asyncio.to_thread(dispatch, message, actor)
        if response is None:
            return JSONResponse({"error": "tool backend returned no response"}, status_code=500)
        if "error" in response:
            return JSONResponse({"protocol_error": response["error"]})
        return JSONResponse({"result": response.get("result", {})})

    return [Route(path, endpoint, methods=["POST"])]


def serve_stdio(
    dispatch: Callable[[Mapping[str, Any], str | None], dict[str, Any] | None],
    actor: str,
    *,
    stdin=None,
    stdout=None,
    actor_prefix: str | None = None,
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
        _pump(dispatch, actor, source, sink, actor_prefix=actor_prefix)
    except BrokenPipeError:
        # The client closed the pipe. That is how an MCP session ends, not a
        # fault to report — and reporting it would mean writing to the pipe
        # that just went away.
        return


def _stdio_actor(message, default: str, prefix: str | None) -> str:
    if prefix is None:
        return default
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, Mapping) else None
    candidate = meta.get("orbit/actor") if isinstance(meta, Mapping) else None
    if candidate is None:
        return default
    if (
        not isinstance(candidate, str) or not candidate.startswith(prefix)
        or len(candidate) > 256 or not candidate.strip()
    ):
        raise ValueError("stdio actor is outside the configured prefix")
    return candidate


def _pump(dispatch, actor, source, sink, *, actor_prefix=None) -> None:
    def routed(message):
        try:
            message_actor = _stdio_actor(message, actor, actor_prefix)
        except ValueError as exc:
            return _failure(message.get("id"), NOT_AUTHORIZED, str(exc))
        return dispatch(message, message_actor)

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
                and (response := routed(item)) is not None
            ]
            if responses:
                _emit(sink, responses)
            continue
        if not isinstance(message, Mapping) or message.get("jsonrpc") != "2.0":
            _emit(sink, _failure(None, INVALID_REQUEST, "expected a JSON-RPC 2.0 message"))
            continue
        response = routed(message)
        if response is not None:
            # A notification gets no line at all: on this transport silence is
            # the whole of "no response", there being no 202 to send.
            _emit(sink, response)


def _emit(sink, payload: Any) -> None:
    sink.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sink.flush()
