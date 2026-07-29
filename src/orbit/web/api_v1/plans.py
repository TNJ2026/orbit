"""Plan read models: the plan as authored, what the run did, and diffs."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ...workflow.api.dto import CursorError, envelope
from ...workflow.api.plan_read_models import PlanNotFound
from ...workflow.domain.ids import EntityId

from .common import READ_SCOPE, SENSITIVE_SCOPE, error


def build_routes(ctx) -> list[Route]:
    def _plan_version(request: Request) -> int | None:
        raw = request.query_params.get("plan_version")
        return None if raw is None else int(raw)

    async def plan_definition(request: Request) -> JSONResponse:
        """The plan as authored — never mixed with what the run did to it."""

        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            payload = ctx.plans.definition(
                EntityId.parse(request.path_params["run_id"]),
                plan_version=_plan_version(request),
            )
        except PlanNotFound as exc:
            return error("not_found", str(exc), 404)
        except ValueError as exc:
            return error("invalid_request", str(exc))
        return JSONResponse(envelope(payload))

    async def plan_overlay(request: Request) -> JSONResponse:
        """What the run did, keyed by node id and stamped with a plan version.

        Separate from the definition so a client cannot render one version's
        graph with another version's statuses without noticing.
        """

        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            raw_position = request.query_params.get("as_of_global_position")
            as_of = None if raw_position is None else int(raw_position)
            payload = ctx.plans.overlay(
                EntityId.parse(request.path_params["run_id"]),
                plan_version=_plan_version(request),
                as_of_global_position=as_of,
            )
        except PlanNotFound as exc:
            return error("not_found", str(exc), 404)
        except ValueError as exc:
            return error("invalid_request", str(exc))
        return JSONResponse(envelope(payload))

    async def plan_diff(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            base = int(request.query_params["base_version"])
            target = int(request.query_params["target_version"])
        except (KeyError, ValueError):
            return error(
                "invalid_request", "base_version and target_version are required"
            )
        try:
            payload = ctx.plans.diff(
                EntityId.parse(request.path_params["run_id"]),
                base_version=base, target_version=target,
            )
        except PlanNotFound as exc:
            return error("not_found", str(exc), 404)
        return JSONResponse(envelope(payload))

    async def foreach_items(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, SENSITIVE_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            cursor, limit = ctx.read_params(request)
            items, next_cursor = ctx.dynamic_reads.foreach_items(
                EntityId.parse(request.path_params["run_id"]),
                EntityId.parse(request.path_params["group_id"]),
                cursor=cursor, limit=limit,
            )
        except CursorError as exc:
            return error("invalid_cursor", str(exc))
        except ValueError as exc:
            return error("not_found", str(exc), 404)
        return JSONResponse(envelope({"items": items}, next_cursor=next_cursor))

    return [
        Route("/api/v1/runs/{run_id}/plan", plan_definition, methods=["GET"]),
        Route("/api/v1/runs/{run_id}/plan/overlay", plan_overlay, methods=["GET"]),
        Route("/api/v1/runs/{run_id}/plan/diff", plan_diff, methods=["GET"]),
        Route("/api/v1/runs/{run_id}/planner-decisions", ctx.paged_read(ctx.dynamic_reads.planner_decisions), methods=["GET"]),
        Route("/api/v1/runs/{run_id}/foreach", ctx.paged_read(ctx.dynamic_reads.foreach_groups), methods=["GET"]),
        Route("/api/v1/runs/{run_id}/foreach/{group_id}/items", foreach_items, methods=["GET"]),
        Route("/api/v1/runs/{run_id}/subflows", ctx.paged_read(ctx.dynamic_reads.subflows), methods=["GET"]),
    ]
