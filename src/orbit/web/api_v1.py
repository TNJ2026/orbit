"""`/api/v1` — the only HTTP surface that may change Runtime state.

Reads are paged and versioned; writes go through one command boundary that
enforces authentication, authorisation, an idempotency key and an expected
version. Actions are advertised through `allowed_commands` rather than being
inferred by the client, so the server stays the only authority on what an
actor may do.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..workflow.api.dto import CursorError, envelope, page_size
from ..workflow.api.plan_read_models import PlanNotFound, PlanReadModelService
from ..workflow.api.read_models import ReadModelService
from ..workflow.api.routes import (
    ApiCommandExecutor, CommandInProgress, IdempotencyConflict, RateLimiter,
    RequestTooLarge, _bounded_json,
)
from ..workflow.application.budget_service import (
    BudgetService, BudgetVersionConflict,
)
from ..workflow.application.foreach_service import ForeachService
from ..workflow.application.human_service import HumanTaskService
from ..workflow.application.run_service import RunApplicationService, RunStartError
from ..workflow.domain.ids import EntityId
from ..workflow.persistence.database import connect_workflow_database
from ..workflow.recovery.manager import RecoveryManager


READ_SCOPE = "runtime.read"
WRITE_SCOPE = "runtime.write"
# Reads that expose more than run metadata get their own scope so a viewer
# token cannot pull artifact contents or raw planner responses.
SENSITIVE_SCOPE = "runtime.read.sensitive"


def _required_version(body: Mapping[str, Any]) -> int:
    """Every write against an existing aggregate carries the version it saw.

    Without it a stale UI tab would silently overwrite a decision someone else
    already made.
    """

    expected = body.get("expected_version")
    if expected is None:
        raise ValueError("expected_version is required")
    return int(expected)


def error(code: str, message: str, status: int = 400, **details: Any) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "details": details}},
        status_code=status,
    )


class Authorizer:
    """Scope check for every request, read and write alike.

    Default-deny: an adapter with no authorizer configured refuses everything
    rather than falling back to "local means trusted".
    """

    def __init__(self, scopes_for: Callable[[str], Sequence[str]] | None = None) -> None:
        self._scopes_for = scopes_for

    def allows(self, actor: str, scope: str) -> bool:
        if self._scopes_for is None:
            return False
        return scope in set(self._scopes_for(actor))


def build_api_v1(
    db_path: Path | str,
    durable_service,
    *,
    authenticator: Callable[[Request], str | None] | None = None,
    authorizer: Authorizer | None = None,
    rate_limiter: RateLimiter | None = None,
    audit: Callable[[str, str, Mapping[str, Any]], None] | None = None,
    fault_hook: Callable[[str], None] | None = None,
    clock: Callable[[], datetime] | None = None,
    agent_catalog: Sequence[Mapping[str, Any]] = (),
) -> list[Route]:
    """Routes for `/api/v1`, ready to mount on the composition root."""

    path = Path(db_path)
    reads = ReadModelService(path)
    runs = RunApplicationService(path, durable_service)
    plans = PlanReadModelService(path)
    humans = HumanTaskService(path)
    budgets = BudgetService(path)
    # Every service a finding can be applied through. Recovery that detects a
    # problem it cannot act on is worse than not detecting it: the operator is
    # told the runtime knows, and the fix fails.
    recovery = RecoveryManager(
        path, durable_service=durable_service, human_service=humans,
        foreach_service=ForeachService(path),
    )
    limiter = rate_limiter or RateLimiter()
    executor = ApiCommandExecutor(path, fault_hook=fault_hook)
    guard = authorizer or Authorizer()
    record_audit = audit or (lambda actor, action, detail: None)
    now = clock or (lambda: datetime.now(timezone.utc))

    def authenticate(request: Request, scope: str) -> str | JSONResponse:
        if authenticator is None:
            return error("unauthenticated", "authentication is not configured", 401)
        actor = authenticator(request)
        if not actor or not actor.strip():
            return error("unauthenticated", "valid actor credentials are required", 401)
        if not guard.allows(actor, scope):
            return error("forbidden", f"actor lacks scope {scope}", 403)
        if not limiter.allow(actor):
            return error("rate_limited", "request rate limit exceeded", 429)
        return actor

    def read_params(request: Request) -> tuple[str | None, int]:
        return (
            request.query_params.get("cursor") or None,
            page_size(request.query_params.get("limit")),
        )

    # -- reads ------------------------------------------------------------

    async def list_runs(request: Request) -> JSONResponse:
        actor = authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            cursor, limit = read_params(request)
            active = request.query_params.get("active") == "true"
            items, next_cursor = reads.list_runs(
                cursor=cursor, limit=limit, active_only=active
            )
        except CursorError as exc:
            return error("invalid_cursor", str(exc))
        except ValueError as exc:
            return error("invalid_request", str(exc))
        return JSONResponse(envelope({"runs": items}, next_cursor=next_cursor))

    async def run_summary(request: Request) -> JSONResponse:
        actor = authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            summary = reads.run_summary(EntityId.parse(request.path_params["run_id"]))
        except ValueError as exc:
            return error("not_found", str(exc), 404)
        return JSONResponse(
            envelope(summary, projection_version=summary["projection_version"])
        )

    def _command_factory(actor: str):
        """Commands are authorised before they are advertised (plan B1).

        A reader who cannot execute a mutation must not be shown its button:
        an inbox full of buttons that 403 teaches people the UI lies. The
        server still re-checks scope on submission — this only shapes what is
        offered.
        """
        if guard.allows(actor, WRITE_SCOPE):
            return None  # read model default: full command set
        return lambda record, *, run_id, run_version: ()

    async def run_responsibilities(request: Request) -> JSONResponse:
        actor = authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            items = reads.responsibilities(
                EntityId.parse(request.path_params["run_id"]),
                command_factory=_command_factory(actor),
            )
        except ValueError as exc:
            return error("not_found", str(exc), 404)
        return JSONResponse(envelope({"responsibilities": items}))

    def _paged_read(loader, scope: str = READ_SCOPE, *, missing_is_not_found=False):
        async def handler(request: Request) -> JSONResponse:
            actor = authenticate(request, scope)
            if isinstance(actor, JSONResponse):
                return actor
            try:
                cursor, limit = read_params(request)
                items, next_cursor = loader(
                    EntityId.parse(request.path_params["run_id"]),
                    cursor=cursor, limit=limit,
                )
            except CursorError as exc:
                return error("invalid_cursor", str(exc))
            except ValueError as exc:
                if missing_is_not_found:
                    return error("not_found", str(exc), 404)
                return error("invalid_request", str(exc))
            return JSONResponse(envelope({"items": items}, next_cursor=next_cursor))

        return handler

    def _plan_version(request: Request) -> int | None:
        raw = request.query_params.get("plan_version")
        return None if raw is None else int(raw)

    async def plan_definition(request: Request) -> JSONResponse:
        """The plan as authored — never mixed with what the run did to it."""

        actor = authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            payload = plans.definition(
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

        actor = authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            payload = plans.overlay(
                EntityId.parse(request.path_params["run_id"]),
                plan_version=_plan_version(request),
            )
        except PlanNotFound as exc:
            return error("not_found", str(exc), 404)
        except ValueError as exc:
            return error("invalid_request", str(exc))
        return JSONResponse(envelope(payload))

    async def plan_diff(request: Request) -> JSONResponse:
        actor = authenticate(request, READ_SCOPE)
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
            payload = plans.diff(
                EntityId.parse(request.path_params["run_id"]),
                base_version=base, target_version=target,
            )
        except PlanNotFound as exc:
            return error("not_found", str(exc), 404)
        return JSONResponse(envelope(payload))

    async def inbox(request: Request) -> JSONResponse:
        actor = authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            cursor, limit = read_params(request)
            items, next_cursor = reads.inbox(
                cursor=cursor, limit=limit,
                command_factory=_command_factory(actor),
            )
        except CursorError as exc:
            return error("invalid_cursor", str(exc))
        except ValueError as exc:
            return error("invalid_request", str(exc))
        return JSONResponse(envelope({"items": items}, next_cursor=next_cursor))

    async def data_lineage(request: Request) -> JSONResponse:
        actor = authenticate(request, SENSITIVE_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            payload = reads.lineage(
                EntityId.parse(request.path_params["run_id"]),
                EntityId.parse(request.path_params["data_id"]),
            )
        except ValueError as exc:
            return error("not_found", str(exc), 404)
        return JSONResponse(envelope(payload))

    async def handler_catalog(request: Request) -> JSONResponse:
        """Installed handlers for the authoring UI.

        Identity and capabilities only: no secrets, and nothing a caller could
        paste together into a shell command.
        """

        actor = authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        registry = getattr(durable_service, "execution_registry", None)
        if registry is None or not registry.sealed:
            return JSONResponse(
                envelope({"handlers": [], "agents": list(agent_catalog)})
            )
        handlers = [
            {
                "name": entry.manifest.name,
                "version": entry.manifest.version,
                "manifest_fingerprint": entry.manifest.fingerprint,
                "node_kinds": list(entry.manifest.node_kinds),
                "execution_safety": entry.manifest.execution_safety.value,
                "capabilities": list(entry.manifest.capabilities),
                "required_secrets": list(entry.manifest.required_secrets),
                "supports_cancel": entry.manifest.supports_cancel,
                "supports_recover": entry.manifest.supports_recover,
            }
            for entry in registry.entries()
        ]
        return JSONResponse(
            envelope({"handlers": handlers, "agents": list(agent_catalog)})
        )

    async def workflow_catalog(request: Request) -> JSONResponse:
        actor = authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        may_start = guard.allows(actor, WRITE_SCOPE)
        with connect_workflow_database(path, read_only=True) as connection:
            rows = connection.execute(
                """SELECT workflow_id, version, definition_hash
                   FROM workflow_versions current
                   WHERE version = (
                     SELECT MAX(version) FROM workflow_versions
                     WHERE workflow_id = current.workflow_id
                   )
                   ORDER BY workflow_id"""
            ).fetchall()
        workflows = [
            {
                "workflow_id": row["workflow_id"],
                "latest_version": row["version"],
                "definition_hash": row["definition_hash"],
                "allowed_commands": ([{
                    "command": "run.start",
                    "label": "Start run",
                    "method": "POST",
                    "href": "/api/v1/runs",
                    "target_aggregate_id": row["workflow_id"],
                    "expected_version": 0,
                    "payload_schema": "run-start/1.0",
                }] if may_start else []),
            }
            for row in rows
        ]
        return JSONResponse(envelope({"workflows": workflows}))

    # -- writes -----------------------------------------------------------

    async def mutate(request: Request, scope: str, action: str, handler) -> JSONResponse:
        actor = authenticate(request, scope)
        if isinstance(actor, JSONResponse):
            return actor
        key = request.headers.get("idempotency-key", "").strip()
        if not key:
            return error("invalid_command", "idempotency-key header is required")
        try:
            body = await _bounded_json(request)
            status, result = executor.execute(
                actor=actor, idempotency_key=key, method=request.method,
                request_path=request.url.path, body=body,
                handler=lambda payload, who, idem: handler(payload, who, idem),
            )
        except RequestTooLarge:
            return error("request_too_large", "request body is too large", 413)
        except json.JSONDecodeError:
            return error("invalid_json", "request body must be JSON")
        except IdempotencyConflict as exc:
            return error("idempotency_conflict", str(exc), 409)
        except CommandInProgress as exc:
            return error("command_in_progress", str(exc), 409)
        except PermissionError as exc:
            return error("forbidden", str(exc), 403)
        except BudgetVersionConflict as exc:
            return error("version_conflict", str(exc), 409)
        except (RunStartError, ValueError) as exc:
            return error("invalid_command", str(exc), 409)
        record_audit(actor, action, {"path": request.url.path, "key": key})
        return JSONResponse(envelope(result), status_code=status)

    async def start_run(request: Request) -> JSONResponse:
        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            workflow_id = str(body.get("workflow_id", "")).strip()
            if not workflow_id:
                raise RunStartError("workflow_id is required")
            version = body.get("workflow_version")
            started = runs.start_run(
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
            return started.to_dict()

        return await mutate(request, WRITE_SCOPE, "run.start", command)

    async def cancel_run(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]

        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            expected = body.get("expected_version")
            if expected is None:
                raise ValueError("expected_version is required")
            return runs.cancel_run(
                run_id, int(expected), actor=actor, idempotency_key=key,
                reason=str(body.get("reason", "cancelled by operator")),
            )

        return await mutate(request, WRITE_SCOPE, "run.cancel", command)

    async def claim_human_task(request: Request) -> JSONResponse:
        task_id = request.path_params["task_id"]

        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            humans.claim(
                EntityId.parse(task_id), actor=actor,
                expected_version=_required_version(body), now=now(),
            )
            return {"task_id": task_id, "status": "claimed"}

        return await mutate(request, WRITE_SCOPE, "human.claim", command)

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
            if not token:
                raise ValueError("submission_token is required")
            parsed_task_id = EntityId.parse(task_id)
            linked = humans.linked_scope(parsed_task_id)
            if linked is not None:
                _node_run_id, run_id = linked
                return durable_service.submit_human_task(
                    parsed_task_id, run_id,
                    _required_version(body), token=token, decision=decision,
                    value=body.get("value"), actor=actor,
                    idempotency_key=key, now=now(),
                )
            status = humans.submit(
                parsed_task_id, token, decision, body.get("value"),
                actor=actor, expected_version=_required_version(body), now=now(),
            )
            return {"task_id": task_id, "decision": decision, "status": status.value}

        return await mutate(request, WRITE_SCOPE, "human.submit", command)

    async def reissue_human_token(request: Request) -> JSONResponse:
        """Hand the submission token to an authorised participant.

        The kernel stores only the token's hash, and the in-memory delivery
        adapter does not survive a restart — without this route a waiting run
        could become permanently unsubmittable. Rotation semantics live in
        HumanTaskService.reissue_token.
        """

        task_id = request.path_params["task_id"]

        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            return humans.reissue_token(
                EntityId.parse(task_id), actor=actor,
                expected_version=_required_version(body), now=now(),
            )

        return await mutate(request, WRITE_SCOPE, "human.token", command)

    async def add_budget(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]

        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            amount = body.get("amount_microunits")
            if amount is None:
                raise ValueError("amount_microunits is required")
            account = budgets.add_budget(
                EntityId.parse(run_id), int(amount),
                # The account's own version, not the run's — the allowed
                # command carries it as `expected_version` against
                # `budget_account:<run>`.
                expected_version=_required_version(body),
                actor=actor, now=now(),
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

        return await mutate(request, WRITE_SCOPE, "budget.add", command)

    async def recovery_scan(request: Request) -> JSONResponse:
        """What the runtime believes is stuck, without changing anything."""

        actor = authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            limit = page_size(request.query_params.get("limit"))
        except ValueError as exc:
            return error("invalid_request", str(exc))
        report = recovery.scan(
            now(), after_run_id=request.query_params.get("after_run_id", ""),
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
                                    "command": "recovery.apply",
                                    "label": "Apply recovery",
                                    "method": "POST",
                                    "href": "/api/v1/recovery/apply",
                                    "target_aggregate_id": finding.entity_id,
                                    "expected_version": finding.expected_version,
                                    "payload_schema": "recovery-apply/1.0",
                                    "action_id": finding.action_id,
                                }]
                                if finding.safe_to_apply
                                and guard.allows(actor, WRITE_SCOPE) else []
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

            results = recovery.apply_findings(selected, now(), actor=actor)
            return {"results": [result.to_dict() for result in results]}

        return await mutate(request, WRITE_SCOPE, "recovery.apply", command)

    return [
        Route("/api/v1/runs", list_runs, methods=["GET"]),
        Route("/api/v1/runs", start_run, methods=["POST"]),
        Route("/api/v1/runs/{run_id}", run_summary, methods=["GET"]),
        Route(
            "/api/v1/runs/{run_id}/responsibilities", run_responsibilities,
            methods=["GET"],
        ),
        Route("/api/v1/runs/{run_id}/timeline", _paged_read(reads.timeline), methods=["GET"]),
        Route("/api/v1/runs/{run_id}/errors", _paged_read(reads.errors), methods=["GET"]),
        Route(
            "/api/v1/runs/{run_id}/data",
            _paged_read(reads.data, SENSITIVE_SCOPE, missing_is_not_found=True),
            methods=["GET"],
        ),
        Route(
            "/api/v1/runs/{run_id}/data/{data_id}/lineage", data_lineage,
            methods=["GET"],
        ),
        Route("/api/v1/runs/{run_id}/cancel", cancel_run, methods=["POST"]),
        Route("/api/v1/runs/{run_id}/plan", plan_definition, methods=["GET"]),
        Route("/api/v1/runs/{run_id}/plan/overlay", plan_overlay, methods=["GET"]),
        Route("/api/v1/runs/{run_id}/plan/diff", plan_diff, methods=["GET"]),
        Route("/api/v1/runs/{run_id}/budget", add_budget, methods=["POST"]),
        Route("/api/v1/inbox", inbox, methods=["GET"]),
        Route(
            "/api/v1/human-tasks/{task_id}/claim", claim_human_task, methods=["POST"]
        ),
        Route(
            "/api/v1/human-tasks/{task_id}/submit", submit_human_task, methods=["POST"]
        ),
        Route(
            "/api/v1/human-tasks/{task_id}/token", reissue_human_token,
            methods=["POST"],
        ),
        Route("/api/v1/recovery", recovery_scan, methods=["GET"]),
        Route("/api/v1/recovery/apply", recovery_apply, methods=["POST"]),
        Route("/api/v1/handler-catalog", handler_catalog, methods=["GET"]),
        Route("/api/v1/workflows", workflow_catalog, methods=["GET"]),
    ]
