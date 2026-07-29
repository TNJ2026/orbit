"""Run lifecycle routes: dashboard, run reads, and run writes."""

from __future__ import annotations

from typing import Any, Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ...workflow.api.dto import CursorError, envelope, page_size
from ...workflow.api.plan_read_models import PlanNotFound
from ...workflow.application.run_service import RunStartError
from ...workflow.domain.ids import EntityId
from ...workflow.persistence.database import connect_workflow_database

from .common import (
    OPS_WRITE_SCOPE, READ_SCOPE, SENSITIVE_SCOPE, WRITE_SCOPE,
    _required_version, error,
)


def build_routes(ctx) -> list[Route]:

    def _plan_version(request: Request) -> int | None:
        raw = request.query_params.get("plan_version")
        return None if raw is None else int(raw)

    async def list_runs(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            allowed_params = {
                "cursor", "limit", "active", "terminal", "q", "status",
                "responsibility",
            }
            unknown = set(request.query_params) - allowed_params
            if unknown:
                raise ValueError(f"unknown run query parameter: {sorted(unknown)[0]}")
            cursor, limit = ctx.read_params(request)
            active_raw = request.query_params.get("active")
            if active_raw not in {None, "true", "false"}:
                raise ValueError("active must be true or false")
            active = active_raw == "true"
            terminal_raw = request.query_params.get("terminal")
            if terminal_raw not in {None, "true", "false"}:
                raise ValueError("terminal must be true or false")
            items, next_cursor = ctx.reads.list_runs(
                cursor=cursor,
                limit=limit,
                active_only=active,
                q=request.query_params.get("q", ""),
                status=request.query_params.get("status") or None,
                responsibility=request.query_params.get("responsibility") or None,
                terminal_only=terminal_raw == "true",
                can_act=ctx.guard.allows(actor, WRITE_SCOPE),
                actor=actor,
            )
        except CursorError as exc:
            return error("invalid_cursor", str(exc))
        except ValueError as exc:
            return error("invalid_request", str(exc))
        return JSONResponse(envelope({"runs": items}, next_cursor=next_cursor))

    async def dashboard(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        dashboard_value = ctx.reads.dashboard(can_act=ctx.guard.allows(actor, WRITE_SCOPE))
        active = dashboard_value.get("active_goal")
        if active is not None:
            active["allowed_commands"] = ([{
                "command": "run.cancel",
                "label": "Cancel run",
                "method": "POST",
                "href": f"/api/v1/runs/{active['run_id']}/cancel",
                "target_aggregate_id": active["run_id"],
                "expected_version": active["projection_version"],
                "payload_schema": "run-cancel/1.0",
                "confirmation": "explicit",
            }] if ctx.guard.allows(actor, WRITE_SCOPE) else [])
        return JSONResponse(
            envelope(dashboard_value)
        )

    async def run_summary(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            summary = ctx.reads.run_summary(
                EntityId.parse(request.path_params["run_id"]),
                can_act=ctx.guard.allows(actor, WRITE_SCOPE),
            )
        except ValueError as exc:
            return error("not_found", str(exc), 404)
        return JSONResponse(
            envelope(summary, projection_version=summary["projection_version"])
        )

    async def run_outcome(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            payload = ctx.reads.outcome(
                EntityId.parse(request.path_params["run_id"]),
                actor=actor,
                content_visible=ctx.guard.allows(actor, SENSITIVE_SCOPE),
            )
        except ValueError as exc:
            return error("not_found", str(exc), 404)
        return JSONResponse(envelope({"result": payload}))

    async def run_responsibilities(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            items = ctx.reads.responsibilities(
                EntityId.parse(request.path_params["run_id"]),
                command_factory=ctx.command_factory(actor), actor=actor,
            )
            run_id = request.path_params["run_id"]
            can_recover = ctx.guard.allows(actor, OPS_WRITE_SCOPE)
            for finding in ctx.recovery.scan(ctx.now(), limit=200, apply=False).findings:
                if finding.run_id != run_id:
                    continue
                command = None
                if can_recover and finding.actionable:
                    takeover = not finding.safe_to_apply
                    command = {
                        "command": (
                            "recovery.takeover" if takeover else "recovery.apply"
                        ),
                        "label": (
                            "Create takeover" if takeover else "Apply recovery"
                        ),
                        "method": "POST", "href": "/api/v1/recovery/apply",
                        "target_aggregate_id": finding.action_id,
                        "expected_version": finding.expected_version,
                        "payload_schema": "recovery-apply/1.0",
                        "confirmation": "explicit",
                    }
                items.append({
                    "responsibility_id": f"recovery:{finding.action_id}",
                    "kind": "recovery", "label": "Recovery required",
                    "status": "blocked", "detail": finding.code,
                    "expected_version": finding.expected_version,
                    "node_run_id": None,
                    "allowed_commands": [] if command is None else [command],
                })
        except ValueError as exc:
            return error("not_found", str(exc), 404)
        return JSONResponse(envelope({"responsibilities": items}))

    async def run_graph(request: Request) -> JSONResponse:
        """Server-projected graph facts; clients must not replay events."""

        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            payload = ctx.plans.graph(
                EntityId.parse(request.path_params["run_id"]),
                plan_version=_plan_version(request),
            )
        except PlanNotFound as exc:
            return error("not_found", str(exc), 404)
        except ValueError as exc:
            return error("invalid_request", str(exc))
        return JSONResponse(
            envelope(payload, projection_version=payload["projection_version"])
        )

    async def data_lineage(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, SENSITIVE_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            payload = ctx.reads.lineage(
                EntityId.parse(request.path_params["run_id"]),
                EntityId.parse(request.path_params["data_id"]),
                actor=actor,
            )
        except ValueError as exc:
            return error("not_found", str(exc), 404)
        return JSONResponse(envelope(payload))

    async def start_run(request: Request) -> JSONResponse:
        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            workflow_id = str(body.get("workflow_id", "")).strip()
            if not workflow_id:
                raise RunStartError("workflow_id is required")
            # A previously advertised start command may be replayed after the
            # Workflow is removed from the catalog. Historical versions remain
            # readable for existing runs, but cannot seed a new one.
            with connect_workflow_database(ctx.workflow_path, read_only=True) as connection:
                deleted = connection.execute(
                    "SELECT 1 FROM archived_workflows WHERE workflow_id=?",
                    (workflow_id,),
                ).fetchone()
            if deleted is not None:
                raise RunStartError("workflow is no longer available")
            version = body.get("workflow_version")
            started = ctx.runs.start_run(
                workflow_id=workflow_id,
                version=None if version is None else int(version),
                inputs=body.get("input") or {},
                goal=str(body.get("goal", "")),
                budget_microunits=(
                    None if body.get("budget_microunits") is None
                    else int(body["budget_microunits"])
                ),
                actor=actor, idempotency_key=key,
            )
            with connect_workflow_database(ctx.path) as connection:
                connection.execute(
                    "INSERT OR IGNORE INTO run_artifact_subjects"
                    "(run_id,subject,role,created_at) VALUES (?,?,'owner',?)",
                    (started.run_id, actor, ctx.now().isoformat()),
                )
                # Covers run-ingress Artifacts committed inside start_run,
                # before this ownership projection could be written.
                connection.execute(
                    "INSERT OR IGNORE INTO artifact_acl"
                    "(artifact_id,subject,permission,granted_by,created_at)"
                    " SELECT artifact_id,?,'read',?,? FROM artifacts"
                    " WHERE run_id=? AND status='committed'",
                    (actor, actor, ctx.now().isoformat(), started.run_id),
                )
                connection.commit()
            return started.to_dict()

        return await ctx.mutate(request, WRITE_SCOPE, "run.start", command)

    async def cancel_run(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]

        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            expected = body.get("expected_version")
            if expected is None:
                raise ValueError("expected_version is required")
            return ctx.runs.cancel_run(
                run_id, int(expected), actor=actor, idempotency_key=key,
                reason=str(body.get("reason", "cancelled by operator")),
            )

        return await ctx.mutate(request, WRITE_SCOPE, "run.cancel", command)

    async def run_output(request: Request) -> JSONResponse:
        """What the Handlers' processes printed, in the order they printed it.

        Not paged by opaque cursor like the projections: this is a tail, and a
        client following a running Agent asks "what is new since chunk N".
        Sensitive scope, because a console holds whatever the Agent echoed.
        """

        actor = ctx.authenticate(request, SENSITIVE_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        allowed_params = {"after", "limit", "node_run_id"}
        unknown = set(request.query_params) - allowed_params
        if unknown:
            return error(
                "invalid_request", f"unknown output parameter: {sorted(unknown)[0]}"
            )
        try:
            after = int(request.query_params.get("after") or 0)
            limit = page_size(request.query_params.get("limit"))
        except ValueError as exc:
            return error("invalid_request", str(exc))
        run_id = request.path_params["run_id"]
        chunks, next_after = ctx.attempt_output.read(
            run_id, after_chunk_id=after, limit=limit,
            node_run_id=request.query_params.get("node_run_id"),
        )
        return JSONResponse(envelope({
            "chunks": chunks,
            # The cursor a follower sends back. Present even when this page is
            # the last one, so a tail can keep asking without re-reading.
            "after": chunks[-1]["chunk_id"] if chunks else after,
            "has_more": next_after is not None,
        }))

    async def retry_node_run(request: Request) -> JSONResponse:
        """Re-run one NodeRun the Runtime could not settle.

        Deliberately an operator decision: only a person can know whether the
        Agent behind an unknown external result already acted.
        """

        run_id = request.path_params["run_id"]
        node_run_id = request.path_params["node_run_id"]

        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            return ctx.runs.retry_node_run(
                run_id, node_run_id, _required_version(body),
                actor=actor, idempotency_key=key,
                reason=str(body.get("reason", "retried by operator")),
                agent=(str(body["agent"]) if body.get("agent") else None),
            )

        return await ctx.mutate(request, WRITE_SCOPE, "node.retry", command)

    async def accept_unknown_result(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        node_run_id = request.path_params["node_run_id"]
        attempt_id = request.path_params["attempt_id"]

        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            return ctx.runs.accept_unknown_result(
                run_id, node_run_id, _required_version(body),
                attempt_id=attempt_id, actor=actor, idempotency_key=key,
            )

        return await ctx.mutate(request, WRITE_SCOPE, "unknown.accept", command)

    async def add_budget(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]

        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            amount = body.get("amount_microunits")
            if amount is None:
                raise ValueError("amount_microunits is required")
            account = ctx.budgets.add_budget(
                EntityId.parse(run_id), int(amount),
                # The account's own version, not the run's — the allowed
                # command carries it as `expected_version` against
                # `budget_account:<run>`.
                expected_version=_required_version(body),
                actor=actor, now=ctx.now(),
                # The caller's key is the ledger key, so a retried grant tops
                # the account up once rather than once per delivery.
                idempotency_key=key,
            )
            return {
                "run_id": run_id,
                "budget": {
                    "total_microunits": account.total_microunits,
                    "reserved_microunits": account.reserved_microunits,
                    "consumed_microunits": account.consumed_microunits,
                    "unit": "microunits",
                },
            }

        return await ctx.mutate(request, WRITE_SCOPE, "budget.add", command)

    return [
        Route("/api/v1/dashboard", dashboard, methods=["GET"]),
        Route("/api/v1/runs", list_runs, methods=["GET"]),
        Route("/api/v1/runs", start_run, methods=["POST"]),
        Route("/api/v1/runs/{run_id}", run_summary, methods=["GET"]),
        Route(
            "/api/v1/runs/{run_id}/responsibilities", run_responsibilities,
            methods=["GET"],
        ),
        Route("/api/v1/runs/{run_id}/outcome", run_outcome, methods=["GET"]),
        Route("/api/v1/runs/{run_id}/timeline", ctx.paged_read(ctx.reads.timeline), methods=["GET"]),
        Route("/api/v1/runs/{run_id}/errors", ctx.paged_read(ctx.reads.errors), methods=["GET"]),
        Route(
            "/api/v1/runs/{run_id}/data",
            ctx.paged_read(
                ctx.reads.data, SENSITIVE_SCOPE,
                missing_is_not_found=True, pass_actor=True,
            ),
            methods=["GET"],
        ),
        Route(
            "/api/v1/runs/{run_id}/data/{data_id}/lineage", data_lineage,
            methods=["GET"],
        ),
        Route("/api/v1/runs/{run_id}/output", run_output, methods=["GET"]),
        Route("/api/v1/runs/{run_id}/cancel", cancel_run, methods=["POST"]),
        Route(
            "/api/v1/runs/{run_id}/node-runs/{node_run_id}/retry",
            retry_node_run, methods=["POST"],
        ),
        Route(
            "/api/v1/runs/{run_id}/node-runs/{node_run_id}/accept/{attempt_id}",
            accept_unknown_result, methods=["POST"],
        ),
        Route("/api/v1/runs/{run_id}/graph", run_graph, methods=["GET"]),
        Route("/api/v1/runs/{run_id}/budget", add_budget, methods=["POST"]),
    ]
