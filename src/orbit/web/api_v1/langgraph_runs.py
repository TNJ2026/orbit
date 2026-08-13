"""Optional HTTP projection for the isolated LangGraph workflow service."""

from __future__ import annotations

from typing import Any, Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
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
    if can_write and run.status in {"running", "waiting", "interrupted"}:
        commands.append({
            "command": "langgraph_run.cancel",
            "label": "Cancel LangGraph workflow",
            "method": "POST",
            "href": f"/api/v1/langgraph-runs/{run.run_id}/cancel",
            "target_aggregate_id": run.run_id,
            "expected_version": run.revision,
            "payload_schema": "langgraph-run-cancel/1.0",
            "confirmation": "explicit",
        })
    return {
        "run_id": run.run_id,
        "workflow_id": run.workflow_id,
        "workflow_version": run.workflow_version,
        "template_id": run.template_id,
        "status": run.status,
        "revision": run.revision,
        "result": run.result,
        "interrupts": list(run.interrupts),
        "error": run.error,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "allowed_commands": commands,
    }


def build_routes(ctx, service, template_service=None) -> list[Route]:
    async def list_templates(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        if template_service is None:
            return error("not_found", "single-Agent templates are not enabled", 404)
        data = dict(template_service.list())
        can_start = data["ready"] and ctx.guard.allows(actor, WRITE_SCOPE)
        data["templates"] = [
            {
                **item,
                "allowed_commands": ([
                    {
                        "command": "workflow_template.start",
                        "label": "Start template",
                        "method": "POST",
                        "href": "/api/v1/workflow-template-runs",
                        "target_aggregate_id": f"template:{item['template_id']}",
                        "payload_schema": "workflow-template-run/1.0",
                    },
                    {
                        "command": "workflow_template.publish",
                        "label": "Publish workflow",
                        "method": "POST",
                        "href": "/api/v1/workflow-template-publish",
                        "target_aggregate_id": f"template:{item['template_id']}",
                        "payload_schema": "workflow-template-publish/1.0",
                    },
                ] if can_start else []),
            }
            for item in data["templates"]
        ]
        data["published"] = [
            {
                **item,
                "allowed_commands": ([{
                    "command": "workflow_template.start",
                    "label": "Start workflow",
                    "method": "POST",
                    "href": "/api/v1/workflow-template-runs",
                    "target_aggregate_id": item["workflow_id"],
                    "payload_schema": "workflow-template-run/1.0",
                }] if can_start else []),
            }
            for item in data["published"]
        ]
        return JSONResponse(envelope(data))

    async def start_template(request: Request) -> JSONResponse:
        if template_service is None:
            return error("not_found", "single-Agent templates are not enabled", 404)

        def command(body: Mapping[str, Any], actor: str, key: str):
            run = template_service.start(
                str(body.get("template_id") or "") or None,
                str(body.get("goal") or ""),
                actor=actor, idempotency_key=key,
                workflow_id=str(body.get("workflow_id") or "") or None,
            )
            return {"run": _dto(run, can_write=True)}

        return await ctx.mutate(
            request, WRITE_SCOPE, "workflow_template.start", command
        )

    async def publish_template(request: Request) -> JSONResponse:
        if template_service is None:
            return error("not_found", "single-Agent templates are not enabled", 404)

        def command(body: Mapping[str, Any], actor: str, _key: str):
            record = template_service.publish(
                str(body.get("template_id") or ""),
                str(body.get("name") or ""), actor=actor,
            )
            return {
                "workflow": {
                    "workflow_id": record.workflow_id,
                    "name": record.ir.name,
                    "template_id": record.ir.labels.get("orbit.template"),
                }
            }

        return await ctx.mutate(
            request, WRITE_SCOPE, "workflow_template.publish", command
        )

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
                actor=actor,
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
            # A run belonging to somebody else is not found — the same rule
            # the Artifact endpoints below already apply, and the reason it
            # matters here is that `interrupts` carry node input data.
            run = service.get(request.path_params["run_id"], actor=actor)
        except LookupError as exc:
            return error("not_found", str(exc), 404)
        return JSONResponse(envelope(
            _dto(run, can_write=ctx.guard.allows(actor, WRITE_SCOPE)),
            projection_version=run.revision,
        ))

    async def replay_run(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            unknown = set(request.query_params) - {"limit"}
            if unknown:
                raise ValueError(f"unknown query parameter: {sorted(unknown)[0]}")
            steps = service.replay(
                request.path_params["run_id"],
                actor=actor,
                limit=page_size(request.query_params.get("limit")),
            )
        except LookupError as exc:
            return error("not_found", str(exc), 404)
        except ValueError as exc:
            return error("invalid_request", str(exc))
        # A read, and only a read: replay derives state from what was recorded
        # and is forbidden to call a Handler or write anything, so it carries
        # no commands and is offered on the read scope.
        return JSONResponse(envelope({"steps": list(steps)}))

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
                    actor=actor,
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
                    interrupt_id=body.get("interrupt_id"),
                    actor=actor,
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

    async def cancel_run(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]

        def command(body: Mapping[str, Any], actor: str, key: str):
            if "expected_version" not in body:
                raise ValueError("expected_version is required")
            try:
                run = service.cancel(
                    run_id,
                    expected_revision=int(body["expected_version"]),
                    idempotency_key=key,
                    actor=actor,
                )
            except LookupError as exc:
                raise ValueError(str(exc)) from None
            return {"run": _dto(run, can_write=True)}

        return await ctx.mutate(
            request, WRITE_SCOPE, "langgraph_run.cancel", command
        )

    async def list_artifacts(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            unknown = set(request.query_params) - {"limit", "run_id"}
            if unknown:
                raise ValueError(f"unknown query parameter: {sorted(unknown)[0]}")
            items = service.artifacts.list(
                run_id=request.query_params.get("run_id") or None,
                limit=page_size(request.query_params.get("limit")),
                actor=actor,
            )
        except ValueError as exc:
            return error("invalid_request", str(exc))
        return JSONResponse(envelope({"artifacts": list(items)}))

    async def get_artifact(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            item = service.artifacts.get(
                request.path_params["artifact_id"], actor=actor,
            )
        except LookupError as exc:
            return error("not_found", str(exc), 404)
        return JSONResponse(envelope(item))

    async def artifact_content(request: Request) -> Response:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            item = service.artifacts.get(
                request.path_params["artifact_id"], actor=actor,
            )
            content = service.artifacts.read(item["artifact_id"], actor=actor)
        except LookupError as exc:
            return error("not_found", str(exc), 404)
        return Response(
            content,
            media_type=item["content_type"],
            headers={
                "Content-Length": str(item["size_bytes"]),
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "Cache-Control": "no-store",
            },
        )

    async def artifact_lineage(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            graph = service.artifacts.lineage(
                request.path_params["artifact_id"], actor=actor,
            )
        except LookupError as exc:
            return error("not_found", str(exc), 404)
        return JSONResponse(envelope(graph))

    routes = [
        Route("/api/v1/langgraph-runs", list_runs, methods=["GET"]),
        Route(
            "/api/v1/langgraph-runs/{run_id}/replay",
            replay_run,
            methods=["GET"],
            name="langgraph_run_replay",
        ),
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
        Route(
            "/api/v1/langgraph-runs/{run_id}/cancel",
            cancel_run,
            methods=["POST"],
        ),
    ]
    if template_service is not None:
        routes[:0] = [
            Route("/api/v1/workflow-templates", list_templates, methods=["GET"]),
            Route("/api/v1/workflow-template-runs", start_template, methods=["POST"]),
            Route("/api/v1/workflow-template-publish", publish_template, methods=["POST"]),
        ]
    if getattr(service, "artifacts", None) is not None:
        routes.extend([
            Route("/api/v1/langgraph-artifacts", list_artifacts, methods=["GET"]),
            Route(
                "/api/v1/langgraph-artifacts/{artifact_id}",
                get_artifact, methods=["GET"],
            ),
            Route(
                "/api/v1/langgraph-artifacts/{artifact_id}/content",
                artifact_content, methods=["GET"],
            ),
            Route(
                "/api/v1/langgraph-artifacts/{artifact_id}/lineage",
                artifact_lineage, methods=["GET"],
            ),
        ])
    return routes
