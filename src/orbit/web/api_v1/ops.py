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
        registry = getattr(ctx.durable_service, "execution_registry", None)
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
            jobs = {
                row["status"]: int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
                )
            }
            timers = {
                row["status"]: int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM durable_timers GROUP BY status"
                )
            }
            active_leases = int(connection.execute(
                "SELECT COUNT(*) FROM job_leases WHERE status='active'"
            ).fetchone()[0])
            unknown_results = int(connection.execute(
                "SELECT COUNT(*) FROM node_attempts WHERE status='unknown_external_result'"
            ).fetchone()[0])
            migration_version = int(connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM workflow_schema_migrations"
            ).fetchone()[0])
        return JSONResponse(envelope({
            "observed_at": ctx.now().isoformat(),
            "integrity": {
                "status": "ok" if quick == "ok" else "failed",
                "check": "sqlite_quick_check", "checked_at": integrity_checked_at,
                "migration_version": migration_version,
            },
            "capacity": {
                "configured_workers": ctx.operational_config.get("worker_count"),
                "poll_seconds": ctx.operational_config.get("poll_seconds"),
                "ready_jobs": jobs.get("ready", 0),
                "running_jobs": jobs.get("running", 0),
                "leased_jobs": jobs.get("leased", 0),
                "benchmark": {"available": False, "reason": "no_persisted_capacity_report"},
            },
            "durable": {
                "jobs_by_status": jobs, "timers_by_status": timers,
                "active_leases": active_leases,
                "unknown_external_results": unknown_results,
            },
            "server_config": {
                "worker_count": ctx.operational_config.get("worker_count"),
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

    return [
        Route("/api/v1/handler-catalog", handler_catalog, methods=["GET"]),
        Route("/api/v1/live", live_cursor, methods=["GET"]),
        Route("/api/v1/ops/status", ops_status, methods=["GET"]),
        Route("/api/v1/capabilities", capability_read, methods=["GET"]),
        Route("/api/v1/runtime/shutdown", runtime_shutdown, methods=["POST"]),
    ]
