"""Optional HTTP projection for the isolated LangGraph workflow service."""

from __future__ import annotations

from typing import Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ...workflow.api.dto import (
    CursorError, decode_cursor, encode_cursor, envelope, page_size,
)

from .common import (
    OPS_WRITE_SCOPE, READ_SCOPE, SENSITIVE_SCOPE, WRITE_SCOPE, error,
)
from ..run_projection import langgraph_run_dto


CURSOR_KIND = "langgraph-runs-v1"


def build_routes(ctx, service) -> list[Route]:
    async def list_runs(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            unknown = set(request.query_params) - {"limit", "status", "cursor", "q"}
            if unknown:
                raise ValueError(f"unknown query parameter: {sorted(unknown)[0]}")
            limit = page_size(request.query_params.get("limit"))
            cursor = decode_cursor(request.query_params.get("cursor"))
            runs = service.list_runs(
                status=request.query_params.get("status") or None,
                limit=limit,
                actor=actor,
                after=(
                    (cursor["created_at"], cursor["run_id"]) if cursor else None
                ),
                query=request.query_params.get("q") or "",
            )
        except CursorError as exc:
            return error("invalid_cursor", str(exc))
        except (KeyError, ValueError) as exc:
            return error("invalid_request", str(exc))
        can_write = ctx.guard.allows(actor, WRITE_SCOPE)
        # A full page means there may be another; a short one is the end.
        next_cursor = encode_cursor({
            "kind": CURSOR_KIND,
            "created_at": runs[-1].created_at,
            "run_id": runs[-1].run_id,
        }) if len(runs) == limit else None
        return JSONResponse(envelope(
            {"runs": [langgraph_run_dto(run, can_write=can_write) for run in runs]},
            next_cursor=next_cursor,
        ))

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
            langgraph_run_dto(run, can_write=ctx.guard.allows(actor, WRITE_SCOPE)),
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

    async def run_steps(request: Request) -> JSONResponse:
        """Where this run got to, derived from its definition and checkpoint.

        Read on the ordinary read scope, not the sensitive one: a step says
        which node ran, how it ended, and what it was asked — never what it
        produced. What a node printed is behind `/output`, and what it made is
        an Artifact. The instruction is the definition's, readable at this
        same scope from the catalog, so carrying it here grants nothing new.
        """

        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            run_id = request.path_params["run_id"]
            unknown = set(request.query_params)
            if unknown:
                raise ValueError(f"unknown query parameter: {sorted(unknown)[0]}")
            run = service.get(run_id, actor=actor)
            steps = service.steps(run_id, actor=actor)
        except LookupError as exc:
            return error("not_found", str(exc), 404)
        except ValueError as exc:
            return error("invalid_request", str(exc))
        return JSONResponse(envelope(
            {"steps": list(steps)}, projection_version=run.revision,
        ))

    async def run_edges(request: Request) -> JSONResponse:
        """Which branches this run took, and which it silently did not.

        Same scope as `/steps` and for the same reason: an edge report says
        which way the run went, never what flowed along it.
        """

        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            run_id = request.path_params["run_id"]
            unknown = set(request.query_params)
            if unknown:
                raise ValueError(f"unknown query parameter: {sorted(unknown)[0]}")
            run = service.get(run_id, actor=actor)
            edges = service.edges(run_id, actor=actor)
        except LookupError as exc:
            return error("not_found", str(exc), 404)
        except ValueError as exc:
            return error("invalid_request", str(exc))
        return JSONResponse(envelope(
            {"edges": list(edges)}, projection_version=run.revision,
        ))

    async def run_graph(request: Request) -> JSONResponse:
        """The definition this run executed, drawn from the run itself.

        Not the workflow's current definition: those are the same thing only
        until somebody republishes.
        """

        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            run_id = request.path_params["run_id"]
            unknown = set(request.query_params)
            if unknown:
                raise ValueError(f"unknown query parameter: {sorted(unknown)[0]}")
            run = service.get(run_id, actor=actor)
            graph = service.graph(run_id, actor=actor)
        except LookupError as exc:
            return error("not_found", str(exc), 404)
        except ValueError as exc:
            return error("invalid_request", str(exc))
        return JSONResponse(envelope(
            {"graph": graph}, projection_version=run.revision,
        ))

    async def run_output(request: Request) -> JSONResponse:
        """What this run's Handlers printed, followed rather than paged.

        `after` is the last chunk the caller already has, so a console tails
        forwards without re-reading what it has shown. `has_more` says a
        further page is waiting now; without it a follower cannot tell a
        full page from the end of the output.
        """

        actor = ctx.authenticate(request, SENSITIVE_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        run_id = request.path_params["run_id"]
        try:
            unknown = set(request.query_params) - {"after", "limit", "node_id"}
            if unknown:
                raise ValueError(f"unknown query parameter: {sorted(unknown)[0]}")
            # Whose run this is decides whether its console may be read, and
            # it is asked before the console is touched.
            service.get(run_id, actor=actor)
            after = request.query_params.get("after") or "0"
            if not after.isdigit():
                raise ValueError("after must be a chunk id")
            console = getattr(service, "console", None)
            if console is None:
                chunks, position, has_more = [], int(after), False
            else:
                chunks, position, has_more = console.read(
                    run_id, after_chunk_id=int(after),
                    limit=page_size(request.query_params.get("limit")),
                    node_id=request.query_params.get("node_id") or None,
                )
        except LookupError as exc:
            return error("not_found", str(exc), 404)
        except ValueError as exc:
            return error("invalid_request", str(exc))
        return JSONResponse(envelope({
            "chunks": chunks, "after": position, "has_more": has_more,
        }))

    async def start_run(request: Request) -> JSONResponse:
        def command(body: Mapping[str, Any], actor: str, key: str):
            workflow_id = str(body.get("workflow_id") or "").strip()
            if not workflow_id:
                raise ValueError("workflow_id is required")
            version = body.get("workflow_version")
            # `wait: false` asks for the run rather than its outcome. The
            # page that starts a goal wants to watch it; an agent calling the
            # same command over MCP wants the answer, and waiting is still
            # what it gets unless it says otherwise.
            wait = body.get("wait")
            if wait is not None and not isinstance(wait, bool):
                raise ValueError("wait must be true or false")
            try:
                run = service.start(
                    workflow_id,
                    body.get("input") or {},
                    workflow_version=None if version is None else int(version),
                    idempotency_key=key,
                    actor=actor,
                    goal=str(body.get("goal") or ""),
                    wait=True if wait is None else wait,
                )
            except LookupError as exc:
                raise ValueError(str(exc)) from None
            return {"run": langgraph_run_dto(run, can_write=True)}

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
            return {"run": langgraph_run_dto(run, can_write=True)}

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
            return {"run": langgraph_run_dto(run, can_write=True)}

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
            return {"run": langgraph_run_dto(run, can_write=True)}

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
            "/api/v1/langgraph-runs/{run_id}/steps", run_steps, methods=["GET"],
        ),
        Route(
            "/api/v1/langgraph-runs/{run_id}/edges", run_edges, methods=["GET"],
        ),
        Route(
            "/api/v1/langgraph-runs/{run_id}/graph", run_graph, methods=["GET"],
        ),
        Route(
            "/api/v1/langgraph-runs/{run_id}/output", run_output, methods=["GET"],
        ),
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
