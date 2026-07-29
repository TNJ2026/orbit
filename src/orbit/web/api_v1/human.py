"""Inbox and human-task routes for the `/api/v1` surface.

The inbox is the actor-shaped read: it merges recovery findings with the
HumanTask projection and advertises only the commands the caller may run.
The human-task routes are the write side — claim, submit, and token reissue —
all through the same command boundary as every other mutation.
"""

from __future__ import annotations

from typing import Any, Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ...workflow.api.dto import CursorError, decode_cursor, encode_cursor, envelope
from ...workflow.domain.ids import EntityId

from .common import OPS_WRITE_SCOPE, READ_SCOPE, WRITE_SCOPE, _required_version, error


def build_routes(ctx) -> list[Route]:
    async def inbox(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            cursor, limit = ctx.read_params(request)
            report = ctx.recovery.scan(ctx.now(), limit=200, apply=False)
            recovery_items = []
            can_recover = ctx.guard.allows(actor, OPS_WRITE_SCOPE)
            for finding in report.findings:
                command = None
                if can_recover and finding.actionable:
                    takeover = not finding.safe_to_apply
                    command = {
                        "command": "recovery.takeover" if takeover else "recovery.apply",
                        "label": "Create takeover" if takeover else "Apply recovery",
                        "method": "POST", "href": "/api/v1/recovery/apply",
                        "target_aggregate_id": finding.action_id,
                        "expected_version": finding.expected_version,
                        "payload_schema": "recovery-apply/1.0",
                        "confirmation": "explicit",
                    }
                recovery_items.append({
                    "action_id": finding.action_id, "code": finding.code,
                    "run_id": finding.run_id, "entity_id": finding.entity_id,
                    "expected_version": finding.expected_version,
                    "safe_to_apply": finding.safe_to_apply,
                    "details": finding.details,
                    "allowed_commands": [] if command is None else [command],
                })
            # Build once without a cursor so total_count and the visible page
            # are guaranteed to describe the same actor-shaped projection.
            projected, _ = ctx.reads.inbox(
                limit=1_000_000, command_factory=ctx.command_factory(actor),
                actor=actor, recovery_findings=recovery_items,
            )
            after = str(decode_cursor(cursor).get("item_id", ""))
            remaining = [item for item in projected if item["item_id"] > after]
            items = remaining[:limit]
            next_cursor = (
                encode_cursor({"item_id": items[-1]["item_id"]})
                if len(remaining) > limit else None
            )
        except CursorError as exc:
            return error("invalid_cursor", str(exc))
        except ValueError as exc:
            return error("invalid_request", str(exc))
        return JSONResponse(envelope(
            {
                "items": items,
                "total_count": len(projected),
                "action_count": sum(item["requires_actor_action"] for item in projected),
            },
            next_cursor=next_cursor,
        ))

    async def claim_human_task(request: Request) -> JSONResponse:
        task_id = request.path_params["task_id"]

        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            ctx.humans.claim(
                EntityId.parse(task_id), actor=actor,
                expected_version=_required_version(body), now=ctx.now(),
            )
            return {"task_id": task_id, "status": "claimed"}

        return await ctx.mutate(request, WRITE_SCOPE, "human.claim", command)

    async def submit_human_task(request: Request) -> JSONResponse:
        """Approve, reject, or answer a HumanTask.

        Approval is not a separate endpoint: an approval task is a HumanTask
        whose decision happens to be approve/reject, and giving it its own
        route would mean two paths into one state machine.
        """

        task_id = request.path_params["task_id"]

        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            token = str(body.get("submission_token", ""))
            decision = str(body.get("decision", ""))
            version = _required_version(body)
            parsed_task_id = EntityId.parse(task_id)
            if not token and actor in ctx.token_exempt_actors:
                # A single-operator Runtime hands this person the token the
                # moment they ask for it, so carrying it back is ceremony, not
                # a check. The Runtime spends one on their behalf instead.
                #
                # Deliberately not a bypass of the token itself: it is minted,
                # rotated and verified exactly as always, and *who may decide*
                # is still the workflow's own answer — an actor the task does
                # not name is refused here as loudly as anywhere else.
                issued = ctx.humans.reissue_token(
                    parsed_task_id, actor=actor, expected_version=version,
                    now=ctx.now(),
                )
                token = issued["submission_token"]
                version = int(issued["expected_version"])
            if not token:
                raise ValueError("submission_token is required")
            linked = ctx.humans.linked_scope(parsed_task_id)
            if linked is not None:
                _node_run_id, run_id = linked
                return ctx.durable_service.submit_human_task(
                    parsed_task_id, run_id,
                    version, token=token, decision=decision,
                    value=body.get("value"), actor=actor,
                    idempotency_key=key, now=ctx.now(),
                )
            status = ctx.humans.submit(
                parsed_task_id, token, decision, body.get("value"),
                actor=actor, expected_version=version, now=ctx.now(),
            )
            return {"task_id": task_id, "decision": decision, "status": status.value}

        return await ctx.mutate(request, WRITE_SCOPE, "human.submit", command)

    async def reissue_human_token(request: Request) -> JSONResponse:
        """Hand the submission token to an authorised participant.

        The kernel stores only the token's hash, and the in-memory delivery
        adapter does not survive a restart — without this route a waiting run
        could become permanently unsubmittable. Rotation semantics live in
        HumanTaskService.reissue_token.
        """

        task_id = request.path_params["task_id"]

        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            return ctx.humans.reissue_token(
                EntityId.parse(task_id), actor=actor,
                expected_version=_required_version(body), now=ctx.now(),
            )

        return await ctx.mutate(request, WRITE_SCOPE, "human.token", command)

    return [
        Route("/api/v1/inbox", inbox, methods=["GET"]),
        Route("/api/v1/human-tasks/{task_id}/claim", claim_human_task, methods=["POST"]),
        Route("/api/v1/human-tasks/{task_id}/submit", submit_human_task, methods=["POST"]),
        Route("/api/v1/human-tasks/{task_id}/token", reissue_human_token, methods=["POST"]),
    ]
