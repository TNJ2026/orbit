"""Optional HTTP projection for the isolated LangGraph workflow service."""

from __future__ import annotations

from typing import Any, Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ...workflow.api.dto import envelope, page_size

from .common import OPS_WRITE_SCOPE, READ_SCOPE, WRITE_SCOPE, error


def _dto(run, *, can_write: bool) -> dict[str, Any]:
    commands = []
    if can_write and run.status == "interrupted":
        commands.append({
            "command": "langgraph_run.resume",
            "label": "Resume LangGraph workflow",
            "method": "POST",
            "href": f"/api/v1/langgraph-runs/{run.run_id}/resume",
            "target_aggregate_id": run.run_id,
            "expected_version": run.revision,
            "payload_schema": "langgraph-run-resume/1.0",
        })
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
        "allowed_commands": commands,
    }


def build_routes(ctx, service) -> list[Route]:
    async def list_runs(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            unknown = set(request.query_params) - {"limit", "status"}
            if unknown:
                raise ValueError(f"unknown query parameter: {sorted(unknown)[0]}")
            limit = page_size(request.query_params.get("limit"))
            runs = service.list_runs(
                status=request.query_params.get("status") or None,
                limit=limit,
            )
        except ValueError as exc:
            return error("invalid_request", str(exc))
        can_write = ctx.guard.allows(actor, WRITE_SCOPE)
        return JSONResponse(envelope({
            "runs": [_dto(run, can_write=can_write) for run in runs]
        }))

    async def get_run(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            run = service.get(request.path_params["run_id"])
        except LookupError as exc:
            return error("not_found", str(exc), 404)
        return JSONResponse(envelope(
            _dto(run, can_write=ctx.guard.allows(actor, WRITE_SCOPE)),
            projection_version=run.revision,
        ))

    async def start_run(request: Request) -> JSONResponse:
        def command(body: Mapping[str, Any], actor: str, key: str):
            workflow_id = str(body.get("workflow_id") or "").strip()
            if not workflow_id:
                raise ValueError("workflow_id is required")
            version = body.get("workflow_version")
            try:
                run = service.start(
                    workflow_id,
                    body.get("input") or {},
                    workflow_version=None if version is None else int(version),
                    idempotency_key=key,
                )
            except LookupError as exc:
                raise ValueError(str(exc)) from None
            return {"run": _dto(run, can_write=True)}

        return await ctx.mutate(
            request, WRITE_SCOPE, "langgraph_run.start", command
        )

    async def resume_run(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]

        def command(body: Mapping[str, Any], actor: str, key: str):
            if "expected_version" not in body:
                raise ValueError("expected_version is required")
            try:
                run = service.resume(
                    run_id,
                    body.get("value"),
                    expected_revision=int(body["expected_version"]),
                    idempotency_key=key,
                )
            except LookupError as exc:
                raise ValueError(str(exc)) from None
            return {"run": _dto(run, can_write=True)}

        return await ctx.mutate(
            request, WRITE_SCOPE, "langgraph_run.resume", command
        )

    async def recover_run(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]

        def command(body: Mapping[str, Any], actor: str, key: str):
            try:
                run = service.recover(run_id)
            except LookupError as exc:
                raise ValueError(str(exc)) from None
            return {"run": _dto(run, can_write=True)}

        return await ctx.mutate(
            request, OPS_WRITE_SCOPE, "langgraph_run.recover", command
        )

    return [
        Route("/api/v1/langgraph-runs", list_runs, methods=["GET"]),
        Route("/api/v1/langgraph-runs", start_run, methods=["POST"]),
        Route("/api/v1/langgraph-runs/{run_id}", get_run, methods=["GET"]),
        Route(
            "/api/v1/langgraph-runs/{run_id}/resume",
            resume_run,
            methods=["POST"],
        ),
        Route(
            "/api/v1/langgraph-runs/{run_id}/recover",
            recover_run,
            methods=["POST"],
        ),
    ]
