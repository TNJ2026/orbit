"""Operator recovery: scan for stuck runs and apply selected findings."""

from __future__ import annotations

from typing import Any, Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ...workflow.api.dto import envelope, page_size

from .common import OPS_READ_SCOPE, OPS_WRITE_SCOPE, error


def build_routes(ctx) -> list[Route]:
    async def recovery_scan(request: Request) -> JSONResponse:
        """What the runtime believes is stuck, without changing anything."""

        actor = ctx.authenticate(request, OPS_READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            limit = page_size(request.query_params.get("limit"))
        except ValueError as exc:
            return error("invalid_request", str(exc))
        report = ctx.recovery.scan(
            ctx.now(), after_run_id=request.query_params.get("after_run_id", ""),
            limit=limit, apply=False,
        )
        return JSONResponse(
            envelope(
                {
                    "findings": [
                        {
                            "action_id": finding.action_id,
                            "code": finding.code,
                            "run_id": finding.run_id,
                            "entity_id": finding.entity_id,
                            "expected_version": finding.expected_version,
                            "safe_to_apply": finding.safe_to_apply,
                            "details": finding.details,
                            "allowed_commands": (
                                [{
                                    "command": (
                                        "recovery.apply" if finding.safe_to_apply
                                        else "recovery.takeover"
                                    ),
                                    "label": (
                                        "Apply recovery" if finding.safe_to_apply
                                        else "Create takeover"
                                    ),
                                    "method": "POST",
                                    "href": "/api/v1/recovery/apply",
                                    "target_aggregate_id": finding.entity_id,
                                    "expected_version": finding.expected_version,
                                    "payload_schema": "recovery-apply/1.0",
                                    "action_id": finding.action_id,
                                }]
                                if ctx.guard.allows(actor, OPS_WRITE_SCOPE)
                                and finding.actionable else []
                            ),
                        }
                        for finding in report.findings
                    ],
                    "scanned_runs": report.scanned_runs,
                    "deadline_reached": report.deadline_reached,
                },
                next_cursor=report.next_cursor,
            )
        )

    async def recovery_apply(request: Request) -> JSONResponse:
        """Apply the findings the operator selected — not the whole scan.

        `action_id` is the compare-and-set token: it embeds the version the
        scan reported, so a finding whose entity has moved on comes back
        `stale` instead of being acted on with a version nobody saw.
        """

        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            selected = body.get("action_ids")
            if not isinstance(selected, list) or not selected:
                raise ValueError(
                    "action_ids must list the findings to apply; applying an"
                    " entire scan would act on findings the operator never saw"
                )
            if not all(isinstance(item, str) and item.strip() for item in selected):
                raise ValueError("every action_id must be a non-empty string")
            if len(selected) > 200:
                raise ValueError("too many findings in one request")

            results = ctx.recovery.apply_findings(selected, ctx.now(), actor=actor)
            return {"results": [result.to_dict() for result in results]}

        return await ctx.mutate(request, OPS_WRITE_SCOPE, "recovery.apply", command)

    return [
        Route("/api/v1/recovery", recovery_scan, methods=["GET"]),
        Route("/api/v1/recovery/apply", recovery_apply, methods=["POST"]),
    ]
