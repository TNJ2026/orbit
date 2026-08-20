"""Operator and meta endpoints: catalog, liveness, status, capabilities."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ...workflow.api.dto import CursorError, decode_cursor, encode_cursor, envelope
from ...workflow.domain.serialization import to_primitive
from ...workflow.persistence.database import connect_workflow_database

from .common import (
    OPS_READ_SCOPE, OPS_WRITE_SCOPE, READ_SCOPE, WRITE_SCOPE,
    _required_version, error,
)


def build_routes(ctx) -> list[Route]:
    async def handler_catalog(request: Request) -> JSONResponse:
        """Installed handlers for the authoring UI.

        Identity and capabilities only: no secrets, and nothing a caller could
        paste together into a shell command.
        """

        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        registry = ctx.execution_registry
        if registry is None or not registry.sealed:
            return JSONResponse(
                envelope({
                    "handlers": [],
                    "agents": [
                        {**dict(item), "registration_status": "discovered"}
                        for item in ctx.agent_catalog
                    ],
                    "status_semantics": "registration_only",
                })
            )
        recent, attempt_counts, failed_counts = ctx.recent_handler_attempts()
        handlers = [
            {
                "name": entry.manifest.name,
                "version": entry.manifest.version,
                "manifest_fingerprint": entry.manifest.fingerprint,
                "node_kinds": list(entry.manifest.node_kinds),
                "inputs": dict(entry.manifest.inputs),
                "outputs": dict(entry.manifest.outputs),
                # Handler manifests recursively freeze JSON objects as
                # mappingproxy values.  Convert the full schema at the HTTP
                # boundary; dict() only thaws its outermost object.
                "config_schema": to_primitive(entry.manifest.config_schema),
                "execution_safety": entry.manifest.execution_safety.value,
                "capabilities": list(entry.manifest.capabilities),
                "required_secrets": list(entry.manifest.required_secrets),
                "supports_cancel": entry.manifest.supports_cancel,
                "supports_recover": entry.manifest.supports_recover,
                "registration_status": "registered",
                "recent_attempt": recent.get(entry.manifest.name),
                "attempt_count": attempt_counts.get(entry.manifest.name, 0),
                "failed_count": failed_counts.get(entry.manifest.name, 0),
            }
            for entry in registry.entries()
        ]
        return JSONResponse(
            envelope({
                "handlers": handlers,
                "agents": [
                    {**dict(item), "registration_status": "discovered"}
                    for item in ctx.agent_catalog
                ],
                "status_semantics": "registration_only",
            })
        )

    async def live_cursor(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            previous = decode_cursor(request.query_params.get("cursor"))
        except CursorError as exc:
            return error("invalid_cursor", str(exc))
        marker = ctx.change_marker()
        cursor = encode_cursor(marker)
        return JSONResponse(envelope({
            "cursor": cursor,
            "changed": bool(previous) and previous != marker,
            "observed_at": ctx.now().isoformat(),
        }))

    # quick_check walks the whole database file; on a grown runtime.db that is
    # seconds, not milliseconds, and Ops/Settings render on every visit. The
    # verdict is cached briefly — counts below stay live on every call.
    integrity_cache: dict[str, Any] = {"verdict": None, "checked_at": None}
    INTEGRITY_TTL_SECONDS = 300.0

    def integrity_verdict() -> tuple[str, str]:
        current = ctx.now()
        checked_at = integrity_cache["checked_at"]
        if (
            integrity_cache["verdict"] is None
            or (current - checked_at).total_seconds() >= INTEGRITY_TTL_SECONDS
        ):
            with connect_workflow_database(ctx.path, read_only=True) as connection:
                integrity_cache["verdict"] = connection.execute(
                    "PRAGMA quick_check(1)"
                ).fetchone()[0]
            integrity_cache["checked_at"] = current
        return integrity_cache["verdict"], integrity_cache["checked_at"].isoformat()

    async def ops_status(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, OPS_READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        quick, integrity_checked_at = integrity_verdict()
        with connect_workflow_database(ctx.path) as connection:
            migration_version = int(connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM workflow_schema_migrations"
            ).fetchone()[0])
        # Straight from the engine that runs the work. This used to count the
        # job, lease and timer rows of a second engine; those tables are gone,
        # and while they existed unwritten this page reported zeros as though
        # the Runtime were idle.
        counts = getattr(ctx.langgraph_service, "counts", None)
        engine = counts() if counts is not None else {
            "runs_by_status": {}, "timers_by_status": {},
            "handler_attempts_by_status": {},
        }
        runs = engine["runs_by_status"]
        return JSONResponse(envelope({
            "observed_at": ctx.now().isoformat(),
            "integrity": {
                "status": "ok" if quick == "ok" else "failed",
                "check": "sqlite_quick_check", "checked_at": integrity_checked_at,
                "migration_version": migration_version,
            },
            "capacity": {
                "poll_seconds": ctx.operational_config.get("poll_seconds"),
                "running_runs": runs.get("running", 0),
                "waiting_runs": runs.get("waiting", 0) + runs.get("interrupted", 0),
            },
            "engine": engine,
            # What the engine is keeping. It grows with every run and nothing
            # else would say so, which is how an operator ends up deciding
            # about retention only once a disk is full.
            "storage_bytes": (
                ctx.langgraph_service.store_sizes()
                if getattr(ctx.langgraph_service, "store_sizes", None) else {}
            ),
            "server_config": {
                "poll_seconds": ctx.operational_config.get("poll_seconds"),
                "artifact_store_configured": ctx.artifact_backend is not None,
            },
        }))

    async def capability_read(request: Request) -> JSONResponse:
        """What this deployment can actually do, and why not when it cannot.

        The delivery plan's empty states need three distinguishable answers —
        no data, no permission, not provided — and the client must never learn
        "not provided" by probing for 404s (plan §8, API-7). Capabilities are
        composition facts injected at build time, not guesses.
        """
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        may_shutdown = (
            ctx.shutdown_request is not None
            and actor in ctx.operators
            and ctx.guard.allows(actor, OPS_WRITE_SCOPE)
        )
        # The actor rides along so the shell can display who is signed in
        # without a separate whoami endpoint.
        return JSONResponse(envelope({
            "actor": actor,
            "capabilities": dict(ctx.capabilities or {}),
            "product_mode": {
                "single_goal_mode": ctx.single_goal_mode,
                # Where a step whose Agent is not installed here would be
                # carried. Null when there is nowhere to carry it — no Agent
                # registered, or several with nothing saying which one this
                # Runtime uses. The client is told; it never infers this.
                "agent_fallback": (
                    None if (target := ctx.agent_fallback()) is None
                    else {"handler_name": target.name, "version": target.version}
                ),
            },
            "permissions": {
                "start_run": ctx.guard.allows(actor, WRITE_SCOPE),
                "ops_read": ctx.guard.allows(actor, OPS_READ_SCOPE),
                "ops_write": ctx.guard.allows(actor, OPS_WRITE_SCOPE),
                # Whether this actor must carry the approval token back. The
                # client is told; it never infers this from being on loopback.
                "human_token_required": actor not in ctx.token_exempt_actors,
            },
            "runtime": {
                "status": "running",
                "allowed_commands": ([{
                    "command": "runtime.shutdown",
                    "label": "Stop Orbit",
                    "method": "POST",
                    "href": "/api/v1/runtime/shutdown",
                    "target_aggregate_id": "runtime",
                    "expected_version": 0,
                    "payload_schema": "runtime-shutdown/1.0",
                }] if may_shutdown else []),
            },
        }))

    async def runtime_shutdown(request: Request) -> JSONResponse:
        """Accept the operator command before asking the ASGI host to exit."""

        if ctx.shutdown_request is None:
            return error("not_supported", "runtime shutdown is not configured", 404)

        def command(
            body: Mapping[str, Any], actor: str, _key: str,
        ) -> Mapping[str, Any]:
            if actor not in ctx.operators:
                raise PermissionError("only a Runtime operator may stop Orbit")
            unknown = set(body) - {"expected_version"}
            if unknown:
                raise ValueError(
                    f"unknown runtime shutdown field: {sorted(unknown)[0]}"
                )
            if _required_version(body) != 0:
                raise ValueError("expected_version must be 0")
            return {"status": "stopping"}

        response = await ctx.mutate(
            request, OPS_WRITE_SCOPE, "runtime.shutdown", command,
        )
        if 200 <= response.status_code < 300:
            # Persist the idempotency receipt and flush the HTTP response before
            # uvicorn begins lifespan shutdown. A successful replay is safe too.
            asyncio.get_running_loop().call_later(0.05, ctx.shutdown_request)
        return response

    async def mcp_session_list(request: Request) -> JSONResponse:
        """Which MCP clients the Runtime has heard from, and for how long ago.

        The HTTP `/mcp` transport has no connection to report, so this answers
        "is an Agent connected" the only honest way: who called, what they
        called themselves at `initialize`, and whether their last message is
        inside the presence window.
        """

        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        registry = ctx.mcp_sessions
        return JSONResponse(envelope({
            "sessions": registry.sessions() if registry is not None else [],
            "presence_seconds": (
                registry.presence_seconds if registry is not None else None
            ),
            "observed_at": ctx.now().isoformat(),
        }))

    return [
        Route("/api/v1/handler-catalog", handler_catalog, methods=["GET"]),
        Route("/api/v1/live", live_cursor, methods=["GET"]),
        Route("/api/v1/mcp/sessions", mcp_session_list, methods=["GET"]),
        Route("/api/v1/ops/status", ops_status, methods=["GET"]),
        Route("/api/v1/capabilities", capability_read, methods=["GET"]),
        Route("/api/v1/runtime/shutdown", runtime_shutdown, methods=["POST"]),
    ]
