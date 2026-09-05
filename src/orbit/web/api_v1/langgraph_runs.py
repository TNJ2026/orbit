"""Optional HTTP projection for the isolated LangGraph workflow service."""

from __future__ import annotations

from typing import Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ...platform.project_occupancy import ProjectOccupancyError
from ...workflow.api.dto import (
    CursorError, decode_cursor, encode_cursor, envelope, page_size,
)

from .common import (
    OPS_WRITE_SCOPE, READ_SCOPE, SENSITIVE_SCOPE, WRITE_SCOPE, error,
)
from ..run_projection import langgraph_run_dto
from ..run_visibility import reading_actor, writing_actor


CURSOR_KIND = "langgraph-runs-v1"



def build_routes(ctx, service) -> list[Route]:
    async def list_runs(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            owner = reading_actor(actor)
            unknown = set(request.query_params) - {"limit", "status", "cursor", "q"}
            if unknown:
                raise ValueError(f"unknown query parameter: {sorted(unknown)[0]}")
            limit = page_size(request.query_params.get("limit"))
            cursor = decode_cursor(request.query_params.get("cursor"))
            runs = service.list_runs(
                status=request.query_params.get("status") or None,
                limit=limit,
                actor=owner,
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
            owner = reading_actor(actor)
            # A run belonging to somebody else is not found — the same rule
            # the Artifact endpoints below already apply, and the reason it
            # matters here is that `interrupts` carry node input data.
            run = service.get(request.path_params["run_id"], actor=owner)
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
            owner = reading_actor(actor)
            unknown = set(request.query_params) - {"limit"}
            if unknown:
                raise ValueError(f"unknown query parameter: {sorted(unknown)[0]}")
            steps = service.replay(
                request.path_params["run_id"],
                actor=owner,
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
            owner = reading_actor(actor)
            run_id = request.path_params["run_id"]
            unknown = set(request.query_params)
            if unknown:
                raise ValueError(f"unknown query parameter: {sorted(unknown)[0]}")
            run = service.get(run_id, actor=owner)
            steps = service.steps(run_id, actor=owner)
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
            owner = reading_actor(actor)
            run_id = request.path_params["run_id"]
            unknown = set(request.query_params)
            if unknown:
                raise ValueError(f"unknown query parameter: {sorted(unknown)[0]}")
            run = service.get(run_id, actor=owner)
            edges = service.edges(run_id, actor=owner)
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
            owner = reading_actor(actor)
            run_id = request.path_params["run_id"]
            unknown = set(request.query_params)
            if unknown:
                raise ValueError(f"unknown query parameter: {sorted(unknown)[0]}")
            run = service.get(run_id, actor=owner)
            graph = service.graph(run_id, actor=owner)
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
            owner = reading_actor(actor)
            unknown = set(request.query_params) - {"after", "limit", "node_id"}
            if unknown:
                raise ValueError(f"unknown query parameter: {sorted(unknown)[0]}")
            # Whose run this is decides whether its console may be read, and
            # it is asked before the console is touched.
            service.get(run_id, actor=owner)
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
                    execution_mode=body.get("execution_mode", "default"),
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
                    actor=writing_actor(actor),
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
                    actor=writing_actor(actor),
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
            owner = reading_actor(actor)
            unknown = set(request.query_params) - {"limit", "run_id"}
            if unknown:
                raise ValueError(f"unknown query parameter: {sorted(unknown)[0]}")
            items = service.artifacts.list(
                run_id=request.query_params.get("run_id") or None,
                limit=page_size(request.query_params.get("limit")),
                actor=owner,
            )
        except ValueError as exc:
            return error("invalid_request", str(exc))
        return JSONResponse(envelope({"artifacts": list(items)}))

    async def get_artifact(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            owner = reading_actor(actor)
            item = service.artifacts.get(
                request.path_params["artifact_id"], actor=owner,
            )
        except LookupError as exc:
            return error("not_found", str(exc), 404)
        return JSONResponse(envelope(item))

    async def artifact_content(request: Request) -> Response:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            owner = reading_actor(actor)
            item = service.artifacts.get(
                request.path_params["artifact_id"], actor=owner,
            )
            content = service.artifacts.read(item["artifact_id"], actor=owner)
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
            owner = reading_actor(actor)
            graph = service.artifacts.lineage(
                request.path_params["artifact_id"], actor=owner,
            )
        except LookupError as exc:
            return error("not_found", str(exc), 404)
        return JSONResponse(envelope(graph))

    async def project_access(request: Request) -> JSONResponse:
        """Who holds the project directory, and what the way back covers.

        §7. Everything here already existed — in a coordinator's memory, in
        an occupancy record, in a git ref — and nowhere a person could see
        it. A project held by a run somebody has to go and answer is the
        thing most worth being able to look up.
        """

        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        access = getattr(service, "project_access", None)
        if access is None:
            return JSONResponse(envelope({
                "enabled": False,
                "reason": "this Runtime grants no project-directory access",
                "allowed_commands": [],
            }))
        registry = access.registry
        records, corrupt_records = registry.inspect(access.project_root)
        occupancies = [
            {
                "run_id": item.run_id,
                "project_root": str(item.identity.real_path),
                "claimed_at": item.claimed_at,
                "state": item.state,
                # Whether a Runtime still holds it, asked of the lock rather
                # than of the recorded pid. False here is not "free": it is a
                # claim whose Runtime is gone, which §4 requires somebody to
                # resolve rather than step over.
                "holder_live": registry.holder_is_live(item),
                "recovery": item.recovery,
                # Which generation of this run's hold you are looking at. A
                # resolve quotes it back; see the resolve route for why.
                "claim_token": token,
            }
            for item, token in records
        ]
        commands = []
        if ctx.guard.allows(actor, OPS_WRITE_SCOPE):
            # The token travels with the affordance because this read is the
            # observation the confirmation will be about: a client that comes
            # back with this payload is saying "the claim I saw here".
            targets = [
                {"run_id": item["run_id"], "claim_token": item["claim_token"]}
                for item in occupancies if not item["holder_live"]
            ] + [
                {"record_id": item["record_id"],
                 "claim_token": item["claim_token"]}
                for item in corrupt_records
            ]
            for target in targets:
                commands.append({
                    "command": "project_access.resolve",
                    "label": "Resolve abandoned project claim",
                    "method": "POST",
                    "href": "/api/v1/project-access/resolve",
                    "target_aggregate_id": next(iter(target.values())),
                    "payload_schema": "project-access-resolve/1.0",
                    # Do not pre-fill true: discovery is not confirmation
                    # that the old Agent processes have actually stopped.
                    "payload": {**target, "processes_stopped": False},
                    "confirmation": "explicit",
                    "confirmation_message": (
                        "Confirm the claim's Agent processes have stopped, "
                        "then set processes_stopped to true. The server "
                        "rechecks the ownership lock before clearing the "
                        "claim, and refuses if the claim itself changed after "
                        "this page was read."
                    ),
                })
        return JSONResponse(envelope({
            "enabled": True,
            "project_root": str(access.project_root),
            "write_granted": access.write_granted,
            "allowed_commands": commands,
            "occupancies": occupancies,
            "corrupt_records": list(corrupt_records),
            "recovery_required": bool(corrupt_records) or any(
                not item["holder_live"] for item in occupancies
            ),
            "needs_recovery": [
                item["run_id"] for item in occupancies
                if not item["holder_live"]
            ],
        }))

    async def run_changes(request: Request) -> JSONResponse:
        """What one run changed, in the two kinds of answer there are (§5).

        `git` is what Orbit compared for itself against the run's recovery
        point. Everything outside that comparison — a non-git project,
        ignored files, submodule working trees — can only come from what the
        Agent said it did, so the payload keeps them apart and marks the whole
        thing as covering a limited range rather than being a filesystem diff.
        """

        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        run_id = request.path_params["run_id"]
        try:
            service.get(run_id, actor=reading_actor(actor))
        except LookupError as exc:
            return error("not_found", str(exc), 404)
        summary = service.project_summary(run_id)
        return JSONResponse(envelope({
            "run_id": run_id,
            "complete_record": False,
            "git": summary,
            "note": (
                "Paths the git comparison covers are Orbit's own observation. "
                "Anything outside it — a non-git project, ignored files, "
                "submodule working trees — is only what the Agent reported, "
                "and this is not a complete filesystem diff."
            ),
        }))

    async def resolve_project_access(request: Request) -> JSONResponse:
        """Clear a claim whose Runtime is gone, once a person has checked.

        §4 requires an abandoned claim to be resolved rather than stepped
        over, and the registry could do it from the first commit — but only
        from a Python prompt, which is not somewhere an operator reading
        `GET /api/v1/project-access` can go. This is that page's other half.

        `processes_stopped` is the whole point of the endpoint rather than
        ceremony around it. Nothing in this process can tell whether the Agent
        subprocess the claim was standing in front of has actually stopped: a
        released lock only means the Runtime that held it is gone, and
        `abandon` releases it deliberately. Clearing the record is what lets
        the next run write the files that process may still be writing, so the
        caller has to say it looked.

        And `claim_token` says which claim it looked at. A confirmation is
        about one hold of the project, and one run can take it more than once
        — resolved, recovered, claimed again. A page read before that,
        answered after it, would otherwise carry a promise about the old
        Agent's processes onto a claim held by a new one nobody has checked.
        The registry re-reads the token under its own lock, so the check
        cannot be raced either.
        """

        def command(body: Mapping[str, Any], actor: str, key: str):
            access = getattr(service, "project_access", None)
            if access is None:
                raise ValueError(
                    "this Runtime grants no project-directory access"
                )
            run_id = str(body.get("run_id") or "").strip()
            # For a record too broken to read, its own run id is not something
            # anybody can recover — the page reports the file name instead.
            record_id = str(body.get("record_id") or "").strip()
            if bool(run_id) == bool(record_id):
                raise ValueError("give exactly one of run_id or record_id")
            if body.get("processes_stopped") is not True:
                raise ValueError(
                    "processes_stopped must be true: confirm the run's Agent "
                    "processes have stopped before its claim is cleared"
                )
            claim_token = str(body.get("claim_token") or "").strip()
            if not claim_token:
                raise ValueError(
                    "claim_token is required: name the claim you looked at, "
                    "from GET /api/v1/project-access"
                )
            try:
                if run_id:
                    cleared = access.registry.resolve(
                        run_id, expected_claim=claim_token,
                        processes_stopped=True,
                    )
                else:
                    access.registry.resolve_record(
                        record_id, expected_claim=claim_token,
                        processes_stopped=True,
                    )
                    cleared = (record_id,)
            except ProjectOccupancyError as exc:
                # `ProjectBusy` and `ProjectNeedsRecovery` both land here: a
                # refusal to clear is a 409 with the registry's own sentence,
                # which already says which of the two it was.
                raise ValueError(str(exc)) from None
            return {"resolved": list(cleared)}

        return await ctx.mutate(
            request, OPS_WRITE_SCOPE, "project_access.resolve", command,
        )

    routes = [
        Route("/api/v1/project-access", project_access, methods=["GET"]),
        Route(
            "/api/v1/project-access/resolve", resolve_project_access,
            methods=["POST"],
        ),
        Route(
            "/api/v1/langgraph-runs/{run_id}/changes", run_changes,
            methods=["GET"],
        ),
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
