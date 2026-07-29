"""Workflow-draft routes for `/api/v1` (editor plan §8).

A draft is the editable face of a published workflow: reads project the
draft record plus its revision context, and every mutation crosses the
single command boundary through one `_draft_mutation` factory.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ...workflow.api.draft_graph import draft_graph
from ...workflow.api.dto import envelope
from ...workflow.domain.ids import EntityId

from .common import (
    READ_SCOPE, WRITE_SCOPE, _generation_agent, _required_version, error,
)


def build_routes(ctx) -> list[Route]:
    # -- workflow drafts (editor plan §8) ----------------------------------

    def _draft_command(record, command: str, label: str) -> dict[str, Any]:
        return {
            "command": f"workflow.draft.{command}",
            "label": label,
            "method": "POST",
            "href": (
                f"/api/v1/workflow-drafts/{quote(record.draft_id, safe=':')}/{command}"
            ),
            "target_aggregate_id": record.draft_id,
            "expected_version": record.revision,
            "payload_schema": f"workflow-draft-{command}/1.0",
        }

    def _draft_dto(record, actor: str) -> dict[str, Any]:
        pending, history, undoable = ctx.draft_service.revision_context(
            EntityId.parse(record.draft_id), actor=actor,
        )
        commands: list[dict[str, Any]] = []
        if record.status == "active" and ctx.guard.allows(actor, WRITE_SCOPE):
            if pending is not None and pending.in_flight:
                # Still with the Agent: the only thing to offer is stopping it.
                commands.append(
                    _draft_command(record, "cancel-revision", "Cancel revision")
                )
            elif pending is not None:
                commands.extend([
                    _draft_command(record, "accept", "Accept revision"),
                    _draft_command(record, "reject", "Reject revision"),
                ])
            elif getattr(ctx.draft_service, "reviser", None) is not None:
                commands.append(_draft_command(record, "revise", "Revise"))
            # Publish is advertised only when this exact source passed the
            # compiler (editor plan §8.2); the server re-checks on submit.
            if (
                pending is None
                and record.validation_status == "valid"
                and record.validated_source_hash == record.source_hash
            ):
                commands.append(_draft_command(record, "publish", "Publish"))
            if pending is None and undoable:
                commands.append(_draft_command(record, "undo", "Undo revision"))
            commands.append(_draft_command(record, "discard", "Discard"))
        pending_dto = None if pending is None else {
            "revision_id": pending.revision_id,
            "instruction": pending.instruction_text,
            "instruction_hash": pending.instruction_hash,
            "base_draft_revision": pending.base_draft_revision,
            "previous_source": pending.previous_source_text,
            "previous_source_hash": pending.previous_source_hash,
            "source": pending.proposed_source_text,
            "source_hash": pending.proposed_source_hash,
            # The proposal drawn the same way the published workflow is, so
            # accepting a revision is not the first time its shape is visible.
            "graph": draft_graph(pending.proposed_source_text),
            "previous_graph": draft_graph(pending.previous_source_text),
            "definition_hash": pending.proposed_definition_hash,
            "attempts": pending.attempts,
            "status": pending.status,
            "created_at": pending.created_at,
            # Job facts: a reloaded editor can tell queued from running from
            # failed, show how long it took and say why it stopped.
            "in_flight": pending.in_flight,
            "cancel_requested": pending.cancel_requested,
            "agent_command": pending.agent_command,
            "model_id": pending.model_id,
            "requested_agent": pending.requested_agent,
            "started_at": pending.started_at,
            "finished_at": pending.finished_at,
            "duration_ms": pending.duration_ms,
            "error_code": pending.error_code,
            "error_message": pending.error_message,
        }
        return {
            "draft_id": record.draft_id,
            "workflow_id": record.workflow_id,
            "base_version": record.base_version,
            "actor": record.actor,
            "source_format": record.source_format,
            "source": record.source_text,
            "source_hash": record.source_hash,
            "graph": draft_graph(record.source_text),
            "validation_status": record.validation_status,
            "validated_definition_hash": record.validated_definition_hash,
            "diagnostics": list(record.diagnostics),
            "revision": record.revision,
            "status": record.status,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "published_version": record.published_version,
            "pending_revision": pending_dto,
            "revision_history": [{
                "revision_id": item.revision_id,
                "instruction": item.instruction_text,
                "instruction_hash": item.instruction_hash,
                "previous_source_hash": item.previous_source_hash,
                "source_hash": item.proposed_source_hash,
                "definition_hash": item.proposed_definition_hash,
                "attempts": item.attempts,
                "status": item.status,
                "created_at": item.created_at,
                "decided_at": item.decided_at,
                "decided_by": item.decided_by,
                "duration_ms": item.duration_ms,
                "error_code": item.error_code,
            } for item in history],
            "allowed_commands": commands,
        }

    async def workflow_draft_create(request: Request) -> JSONResponse:
        if ctx.draft_service is None:
            return error("drafts_unavailable", "workflow drafts are not wired", 503)
        if getattr(ctx.draft_service, "reviser", None) is None:
            return error(
                "generation_unavailable", "no agent reviser is configured", 503,
            )
        workflow_id = request.path_params["workflow_id"]

        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            base = body.get("base_version")
            record = ctx.draft_service.create_or_resume(
                workflow_id,
                base_version=None if base is None else int(base),
                actor=actor, now=ctx.now(),
            )
            return _draft_dto(record, actor)

        return await ctx.mutate(request, WRITE_SCOPE, "workflow.draft.create", command)

    async def workflow_draft_read(request: Request) -> JSONResponse:
        if ctx.draft_service is None:
            return error("drafts_unavailable", "workflow drafts are not wired", 503)
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        from ...workflow.application.workflow_draft_service import DraftNotFoundError

        try:
            record = ctx.draft_service.get(
                EntityId.parse(request.path_params["draft_id"]),
                actor=actor, now=ctx.now(),
            )
        except (DraftNotFoundError, ValueError) as exc:
            return error("workflow_draft_not_found", str(exc), 404)
        return JSONResponse(envelope(_draft_dto(record, actor)))

    def _draft_mutation(action: str, invoke) -> Callable:
        async def handler(request: Request) -> JSONResponse:
            if ctx.draft_service is None:
                return error(
                    "drafts_unavailable", "workflow drafts are not wired", 503,
                )
            draft_id = request.path_params["draft_id"]

            def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
                return invoke(EntityId.parse(draft_id), body, actor)

            return await ctx.mutate(request, WRITE_SCOPE, action, command)

        return handler

    def _draft_publish(draft_id, body, actor):
        record, version = ctx.draft_service.publish(
            draft_id, expected_revision=_required_version(body),
            actor=actor, now=ctx.now(),
        )
        return {**_draft_dto(record, actor), "published": version}

    def _draft_discard(draft_id, body, actor):
        record = ctx.draft_service.discard(
            draft_id, expected_revision=_required_version(body),
            actor=actor, now=ctx.now(),
        )
        return _draft_dto(record, actor)

    def _draft_revise(draft_id, body, actor):
        instruction = body.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("instruction is required")
        agent = _generation_agent(body)
        if agent is not None and ctx.authoring_service is not None:
            agent = ctx.authoring_service.ensure_agent(agent)
        record = ctx.draft_service.revise(
            draft_id, instruction, expected_revision=_required_version(body),
            actor=actor, now=ctx.now(), agent=agent,
        )
        return _draft_dto(record, actor)

    def _draft_accept(draft_id, body, actor):
        record = ctx.draft_service.accept_revision(
            draft_id, expected_revision=_required_version(body),
            actor=actor, now=ctx.now(),
        )
        return _draft_dto(record, actor)

    def _draft_reject(draft_id, body, actor):
        record = ctx.draft_service.reject_revision(
            draft_id, expected_revision=_required_version(body),
            actor=actor, now=ctx.now(),
        )
        return _draft_dto(record, actor)

    def _draft_undo(draft_id, body, actor):
        record = ctx.draft_service.undo_revision(
            draft_id, expected_revision=_required_version(body),
            actor=actor, now=ctx.now(),
        )
        return _draft_dto(record, actor)

    workflow_draft_publish = _draft_mutation("workflow.draft.publish", _draft_publish)
    workflow_draft_discard = _draft_mutation("workflow.draft.discard", _draft_discard)
    workflow_draft_revise = _draft_mutation("workflow.draft.revise", _draft_revise)
    workflow_draft_accept = _draft_mutation("workflow.draft.accept", _draft_accept)

    def _draft_cancel_revision(draft_id, body, actor):
        revision_id = body.get("revision_id")
        if not isinstance(revision_id, str) or not revision_id.strip():
            raise ValueError("revision_id is required")
        record = ctx.draft_service.cancel_revision(
            draft_id, EntityId.parse(revision_id), actor=actor, now=ctx.now(),
        )
        return _draft_dto(record, actor)

    workflow_draft_reject = _draft_mutation("workflow.draft.reject", _draft_reject)
    workflow_draft_undo = _draft_mutation("workflow.draft.undo", _draft_undo)
    workflow_draft_cancel_revision = _draft_mutation(
        "workflow.draft.cancel-revision", _draft_cancel_revision,
    )

    return [
        Route(
            "/api/v1/workflows/{workflow_id}/drafts", workflow_draft_create,
            methods=["POST"],
        ),
        Route(
            "/api/v1/workflow-drafts/{draft_id}", workflow_draft_read,
            methods=["GET"],
        ),
        Route(
            "/api/v1/workflow-drafts/{draft_id}/publish", workflow_draft_publish,
            methods=["POST"],
        ),
        Route(
            "/api/v1/workflow-drafts/{draft_id}/discard", workflow_draft_discard,
            methods=["POST"],
        ),
        Route(
            "/api/v1/workflow-drafts/{draft_id}/revise", workflow_draft_revise,
            methods=["POST"],
        ),
        Route(
            "/api/v1/workflow-drafts/{draft_id}/accept", workflow_draft_accept,
            methods=["POST"],
        ),
        Route(
            "/api/v1/workflow-drafts/{draft_id}/reject", workflow_draft_reject,
            methods=["POST"],
        ),
        Route(
            "/api/v1/workflow-drafts/{draft_id}/undo", workflow_draft_undo,
            methods=["POST"],
        ),
        Route(
            "/api/v1/workflow-drafts/{draft_id}/cancel-revision",
            workflow_draft_cancel_revision, methods=["POST"],
        ),
    ]
