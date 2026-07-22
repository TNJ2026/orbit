"""`/api/v1` — the only HTTP surface that may change Runtime state.

Reads are paged and versioned; writes go through one command boundary that
enforces authentication, authorisation, an idempotency key and an expected
version. Actions are advertised through `allowed_commands` rather than being
inferred by the client, so the server stays the only authority on what an
actor may do.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

import anyio

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ..workflow.api.dto import CursorError, decode_cursor, encode_cursor, envelope, page_size
from ..workflow.api.draft_graph import draft_graph
from ..workflow.api.artifact_read_models import (
    ArtifactNotVisible, ArtifactReadModelService, PREVIEW_LIMIT_BYTES,
)
from ..workflow.api.plan_read_models import PlanNotFound, PlanReadModelService
from ..workflow.api.dynamic_read_models import DynamicReadModelService
from ..workflow.api.read_models import ReadModelService
from ..workflow.api.workflow_catalog import WorkflowCatalogReadModelService
from ..workflow.persistence.attempt_output import SQLiteAttemptOutputStore
from ..workflow.api.routes import (
    ApiCommandExecutor, CommandInProgress, IdempotencyConflict, RateLimiter,
    RequestTooLarge, _bounded_json,
)
from ..workflow.application.budget_service import (
    BudgetService, BudgetVersionConflict,
)
from ..workflow.application.foreach_service import ForeachService
from ..workflow.application.human_service import HumanTaskService
from ..workflow.application.run_service import (
    ActiveGoalExistsError,
    RunApplicationService,
    RunStartError,
)
from ..workflow.catalogs.schemas import InMemorySchemaCatalog
from ..workflow.domain.ids import EntityId
from ..workflow.domain.serialization import to_primitive
from ..workflow.domain.versions import DefinitionHash
from ..workflow.artifacts.local_cas import BlobIntegrityError
from ..workflow.authoring import (
    AuthoringFailedError, AuthoringUnavailableError,
    UnknownGenerationAgentError,
)
from ..workflow.application.workflow_draft_service import (
    DraftAlreadyActiveError, DraftNotFoundError, DraftNotValidatedError,
    DraftSourceTooLargeError, DraftVersionConflictError, RevisionNotFoundError,
    RevisionUnavailableError, SourceUnavailableError,
    WorkflowVersionConflictError,
)
from ..workflow.persistence.database import connect_workflow_database
from ..workflow.persistence.control import audit as persist_audit
from ..workflow.recovery.manager import RecoveryManager


READ_SCOPE = "runtime.read"
WRITE_SCOPE = "runtime.write"
# Reads that expose more than run metadata get their own scope so a viewer
# token cannot pull artifact contents or raw planner responses.
SENSITIVE_SCOPE = "runtime.read.sensitive"
OPS_READ_SCOPE = "runtime.ops.read"
OPS_WRITE_SCOPE = "runtime.ops.write"


class ClosingStreamingResponse(StreamingResponse):
    """A stream response that closes its source even on client disconnect."""

    def __init__(self, source, iterator, **kwargs):
        self._source = source
        super().__init__(iterator, **kwargs)

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await anyio.to_thread.run_sync(self._source.close)


def _generation_agent(body: Mapping[str, Any]) -> str | None:
    """The Agent the author picked to write the DSL, by name only.

    A name is the whole of what a caller may contribute: the command behind it
    was fixed at composition from the discovery allowlist. Omitting it keeps
    this Runtime's default Agent.
    """

    agent = body.get("agent")
    if agent is None:
        return None
    if not isinstance(agent, str) or not agent.strip():
        raise ValueError("agent must be a non-empty string")
    return agent.strip()


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
    unlimited_actors: Sequence[str] = (),
    token_exempt_actors: Sequence[str] = (),
    operator_actors: Sequence[str] = (),
    audit: Callable[[str, str, Mapping[str, Any]], None] | None = None,
    fault_hook: Callable[[str], None] | None = None,
    clock: Callable[[], datetime] | None = None,
    agent_catalog: Sequence[Mapping[str, Any]] = (),
    capabilities: Mapping[str, Mapping[str, Any]] | None = None,
    schema_catalog=None,
    artifact_backend=None,
    operational_config: Mapping[str, Any] | None = None,
    authoring_service=None,
    workflow_publisher=None,
    draft_service=None,
    single_goal_mode: bool = True,
) -> list[Route]:
    """Routes for `/api/v1`, ready to mount on the composition root."""

    path = Path(db_path)
    reads = ReadModelService(path)
    artifact_reads = ArtifactReadModelService(path)
    artifact_backend = artifact_backend or getattr(durable_service, "artifact_backend", None)
    runs = RunApplicationService(
        path, durable_service, enforce_single_goal=single_goal_mode
    )
    plans = PlanReadModelService(path)
    dynamic_reads = DynamicReadModelService(path)
    workflow_reads = WorkflowCatalogReadModelService(
        path, schema_catalog or InMemorySchemaCatalog({})
    )
    humans = HumanTaskService(path)
    # Handler console output: an observation store, not a projection of
    # events, so it is read directly rather than through ReadModelService.
    attempt_output = SQLiteAttemptOutputStore(path)
    budgets = BudgetService(path)
    # Every service a finding can be applied through. Recovery that detects a
    # problem it cannot act on is worse than not detecting it: the operator is
    # told the runtime knows, and the fix fails.
    recovery = RecoveryManager(
        path, durable_service=durable_service, human_service=humans,
        foreach_service=ForeachService(path),
        # A takeover is answered by a person. The composition root is the only
        # place that knows who that is here.
        takeover_participants=tuple(operator_actors),
    )
    limiter = rate_limiter or RateLimiter()
    # The limit exists to keep a shared deployment from being drowned by one
    # caller. An actor the composition root vouches for — the single operator
    # on loopback — is not that caller, and its own UI polling should never
    # lock it out of its own Runtime.
    exempt_from_limit = frozenset(unlimited_actors)
    # Actors the composition root vouches for as the person at the keyboard.
    # They still need the token — the Runtime just stops asking them to carry
    # it back and forth to themselves.
    token_exempt_actors = frozenset(token_exempt_actors)
    executor = ApiCommandExecutor(path, fault_hook=fault_hook)
    guard = authorizer or Authorizer()
    record_audit = audit or (lambda actor, action, detail: None)
    now = clock or (lambda: datetime.now(timezone.utc))
    operational_config = dict(operational_config or {})

    def recent_handler_attempts() -> dict[str, Mapping[str, Any]]:
        """Latest durable attempt per handler name, never a heartbeat proxy."""

        with connect_workflow_database(path) as connection:
            rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT j.job_kind AS handler_name, nr.run_id, nr.node_id,
                           a.attempt_id, a.status, a.updated_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY j.job_kind
                               ORDER BY a.updated_at DESC, a.attempt_id DESC
                           ) AS rank
                    FROM node_attempts a
                    JOIN node_runs nr ON nr.node_run_id = a.node_run_id
                    JOIN jobs j ON j.current_attempt_id = a.attempt_id
                )
                SELECT handler_name, run_id, node_id, attempt_id, status, updated_at
                FROM ranked WHERE rank = 1
                """
            ).fetchall()
        return {
            str(row["handler_name"]): {
                "run_id": row["run_id"], "node_id": row["node_id"],
                "attempt_id": row["attempt_id"], "status": row["status"],
                "occurred_at": row["updated_at"],
            }
            for row in rows
        }

    def change_marker() -> Mapping[str, Any]:
        with connect_workflow_database(path) as connection:
            event_position = connection.execute(
                "SELECT COALESCE(MAX(global_position), 0) FROM run_events"
            ).fetchone()[0]
            durable_updated = connection.execute(
                """
                SELECT COALESCE(MAX(value), '') FROM (
                    SELECT MAX(updated_at) AS value FROM jobs
                    UNION ALL SELECT MAX(updated_at) FROM node_attempts
                    UNION ALL SELECT MAX(updated_at) FROM durable_timers
                )
                """
            ).fetchone()[0]
        return {"event_position": int(event_position), "durable_updated": durable_updated}

    def audit_artifact_read(
        actor: str, action: str, artifact_id: str, decision: str,
        *, run_id: str | None = None, details: Mapping[str, Any] | None = None,
    ) -> None:
        # Denied ids are hashed so the audit store cannot become an oracle for
        # Artifact identities the actor was not allowed to enumerate.
        target = artifact_id if decision == "allowed" else (
            "artifact_ref_hash:" + hashlib.sha256(artifact_id.encode()).hexdigest()
        )
        with connect_workflow_database(path) as connection:
            persist_audit(
                connection,
                run_id=None if run_id is None else EntityId.parse(run_id),
                actor=actor, action=action, target_id=target,
                decision=decision, details=dict(details or {}), occurred_at=now(),
            )
            connection.commit()

    def authenticate(request: Request, scope: str) -> str | JSONResponse:
        if authenticator is None:
            return error("unauthenticated", "authentication is not configured", 401)
        actor = authenticator(request)
        if not actor or not actor.strip():
            return error("unauthenticated", "valid actor credentials are required", 401)
        if not guard.allows(actor, scope):
            return error("forbidden", f"actor lacks scope {scope}", 403)
        if actor not in exempt_from_limit and not limiter.allow(actor):
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
            allowed_params = {"cursor", "limit", "active", "q", "status", "responsibility"}
            unknown = set(request.query_params) - allowed_params
            if unknown:
                raise ValueError(f"unknown run query parameter: {sorted(unknown)[0]}")
            cursor, limit = read_params(request)
            active_raw = request.query_params.get("active")
            if active_raw not in {None, "true", "false"}:
                raise ValueError("active must be true or false")
            active = active_raw == "true"
            items, next_cursor = reads.list_runs(
                cursor=cursor,
                limit=limit,
                active_only=active,
                q=request.query_params.get("q", ""),
                status=request.query_params.get("status") or None,
                responsibility=request.query_params.get("responsibility") or None,
                can_act=guard.allows(actor, WRITE_SCOPE),
            )
        except CursorError as exc:
            return error("invalid_cursor", str(exc))
        except ValueError as exc:
            return error("invalid_request", str(exc))
        return JSONResponse(envelope({"runs": items}, next_cursor=next_cursor))

    async def dashboard(request: Request) -> JSONResponse:
        actor = authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        dashboard_value = reads.dashboard(can_act=guard.allows(actor, WRITE_SCOPE))
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
            }] if guard.allows(actor, WRITE_SCOPE) else [])
        return JSONResponse(
            envelope(dashboard_value)
        )

    async def run_summary(request: Request) -> JSONResponse:
        actor = authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            summary = reads.run_summary(
                EntityId.parse(request.path_params["run_id"]),
                can_act=guard.allows(actor, WRITE_SCOPE),
            )
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
                command_factory=_command_factory(actor), actor=actor,
            )
        except ValueError as exc:
            return error("not_found", str(exc), 404)
        return JSONResponse(envelope({"responsibilities": items}))

    def _paged_read(
        loader, scope: str = READ_SCOPE, *, missing_is_not_found=False,
        pass_actor: bool = False,
    ):
        async def handler(request: Request) -> JSONResponse:
            actor = authenticate(request, scope)
            if isinstance(actor, JSONResponse):
                return actor
            try:
                cursor, limit = read_params(request)
                arguments = {"cursor": cursor, "limit": limit}
                if pass_actor:
                    arguments["actor"] = actor
                items, next_cursor = loader(
                    EntityId.parse(request.path_params["run_id"]), **arguments
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
            raw_position = request.query_params.get("as_of_global_position")
            as_of = None if raw_position is None else int(raw_position)
            payload = plans.overlay(
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

    async def foreach_items(request: Request) -> JSONResponse:
        actor = authenticate(request, SENSITIVE_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            cursor, limit = read_params(request)
            items, next_cursor = dynamic_reads.foreach_items(
                EntityId.parse(request.path_params["run_id"]),
                EntityId.parse(request.path_params["group_id"]),
                cursor=cursor, limit=limit,
            )
        except CursorError as exc:
            return error("invalid_cursor", str(exc))
        except ValueError as exc:
            return error("not_found", str(exc), 404)
        return JSONResponse(envelope({"items": items}, next_cursor=next_cursor))

    async def run_graph(request: Request) -> JSONResponse:
        """Server-projected graph facts; clients must not replay events."""

        actor = authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            payload = plans.graph(
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

    async def inbox(request: Request) -> JSONResponse:
        actor = authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            cursor, limit = read_params(request)
            report = recovery.scan(now(), limit=200, apply=False)
            recovery_items = []
            can_recover = guard.allows(actor, OPS_WRITE_SCOPE)
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
            projected, _ = reads.inbox(
                limit=1_000_000, command_factory=_command_factory(actor),
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

    async def data_lineage(request: Request) -> JSONResponse:
        actor = authenticate(request, SENSITIVE_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            payload = reads.lineage(
                EntityId.parse(request.path_params["run_id"]),
                EntityId.parse(request.path_params["data_id"]),
                actor=actor,
            )
        except ValueError as exc:
            return error("not_found", str(exc), 404)
        return JSONResponse(envelope(payload))

    async def artifact_list(request: Request) -> JSONResponse:
        actor = authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            unknown = set(request.query_params) - {
                "cursor", "limit", "q", "run_id", "content_type",
            }
            if unknown:
                raise ValueError(f"unknown Artifact query parameter: {sorted(unknown)[0]}")
            cursor, limit = read_params(request)
            items, next_cursor = artifact_reads.list(
                actor, cursor=cursor, limit=limit,
                q=request.query_params.get("q", ""),
                run_id=request.query_params.get("run_id", ""),
                content_type=request.query_params.get("content_type", ""),
            )
        except CursorError as exc:
            return error("invalid_cursor", str(exc))
        except ValueError as exc:
            return error("invalid_request", str(exc))
        audit_artifact_read(
            actor, "artifact.list", "artifact_catalog", "allowed",
            details={"returned": len(items)},
        )
        return JSONResponse(envelope({"artifacts": items}, next_cursor=next_cursor))

    def _artifact_id(request: Request) -> EntityId:
        value = EntityId.parse(request.path_params["artifact_id"])
        if value.kind != "artifact":
            raise ValueError("Artifact not found")
        return value

    async def artifact_detail(request: Request) -> JSONResponse:
        actor = authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            payload = artifact_reads.detail(actor, _artifact_id(request))
        except (ArtifactNotVisible, ValueError):
            audit_artifact_read(
                actor, "artifact.metadata.read",
                request.path_params["artifact_id"], "denied",
            )
            # Same body for nonexistent, uncommitted and unauthorized ids.
            return error("artifact_not_found", "Artifact not found", 404)
        audit_artifact_read(
            actor, "artifact.metadata.read", payload["artifact_id"], "allowed",
            run_id=payload["run_id"],
        )
        return JSONResponse(envelope(payload))

    async def artifact_lineage(request: Request) -> JSONResponse:
        actor = authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            payload = artifact_reads.lineage(actor, _artifact_id(request))
        except (ArtifactNotVisible, ValueError):
            audit_artifact_read(
                actor, "artifact.lineage.read",
                request.path_params["artifact_id"], "denied",
            )
            return error("artifact_not_found", "Artifact not found", 404)
        audit_artifact_read(
            actor, "artifact.lineage.read", payload["artifact"]["artifact_id"],
            "allowed", run_id=payload["artifact"]["run_id"],
        )
        return JSONResponse(envelope(payload))

    async def artifact_content(request: Request) -> Response:
        actor = authenticate(request, SENSITIVE_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        if artifact_backend is None:
            return error("artifact_store_unavailable", "Artifact store is unavailable", 503)
        download = request.query_params.get("download") == "true"
        if set(request.query_params) - {"download"}:
            return error("invalid_request", "unknown Artifact content parameter")
        try:
            record = artifact_reads.authorized_record(actor, _artifact_id(request))
        except (ArtifactNotVisible, ValueError):
            audit_artifact_read(
                actor, "artifact.content.read",
                request.path_params["artifact_id"], "denied",
                details={"download": download},
            )
            return error("artifact_not_found", "Artifact not found", 404)
        if not download:
            content_type = record["content_type"]
            if not (content_type.startswith("text/") or content_type == "application/json"):
                return error("preview_unsupported", "Artifact is not text-previewable", 415)
            if int(record["size_bytes"]) > PREVIEW_LIMIT_BYTES:
                return error(
                    "preview_too_large", "Artifact exceeds the preview limit", 413,
                    size_bytes=int(record["size_bytes"]), limit_bytes=PREVIEW_LIMIT_BYTES,
                )
        try:
            audit_artifact_read(
                actor, "artifact.content.read", record["artifact_id"], "allowed",
                run_id=record["run_id"], details={"download": download},
            )
            if download:
                if not hasattr(artifact_backend, "open_verified_stream"):
                    return error(
                        "artifact_stream_unavailable",
                        "Artifact backend does not support validated streaming", 503,
                    )
                source = await anyio.to_thread.run_sync(
                    artifact_backend.open_verified_stream,
                    record["blob_key"], DefinitionHash(record["checksum"]),
                    int(record["size_bytes"]),
                )
                def chunks():
                    try:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            yield chunk
                    finally:
                        source.close()

                filename = quote(f"{record['artifact_id']}.bin", safe="")
                return ClosingStreamingResponse(
                    source, chunks(), media_type=record["content_type"],
                    headers={
                        "X-Content-Type-Options": "nosniff",
                        "Content-Length": str(record["size_bytes"]),
                        "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
                    },
                )
            content = artifact_backend.read(
                record["blob_key"], max_size_bytes=PREVIEW_LIMIT_BYTES
            )
        except BlobIntegrityError as exc:
            if "missing" in str(exc).lower():
                return error("blob_missing", "Artifact Blob is missing", 410)
            return error("artifact_integrity_failed", "Artifact integrity check failed", 409)
        if len(content) != int(record["size_bytes"]):
            return error("artifact_integrity_failed", "Artifact integrity check failed", 409)
        headers = {"X-Content-Type-Options": "nosniff"}
        return Response(content, media_type=record["content_type"], headers=headers)

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
                envelope({
                    "handlers": [],
                    "agents": [
                        {**dict(item), "registration_status": "discovered"}
                        for item in agent_catalog
                    ],
                    "status_semantics": "registration_only",
                })
            )
        recent = recent_handler_attempts()
        handlers = [
            {
                "name": entry.manifest.name,
                "version": entry.manifest.version,
                "manifest_fingerprint": entry.manifest.fingerprint,
                "node_kinds": list(entry.manifest.node_kinds),
                "inputs": dict(entry.manifest.inputs),
                "outputs": dict(entry.manifest.outputs),
                # Handler manifests recursively freeze JSON objects as
                # mappingproxy values.  Convert the full schema at the HTTP
                # boundary; dict() only thaws its outermost object.
                "config_schema": to_primitive(entry.manifest.config_schema),
                "execution_safety": entry.manifest.execution_safety.value,
                "capabilities": list(entry.manifest.capabilities),
                "required_secrets": list(entry.manifest.required_secrets),
                "supports_cancel": entry.manifest.supports_cancel,
                "supports_recover": entry.manifest.supports_recover,
                "registration_status": "registered",
                "recent_attempt": recent.get(entry.manifest.name),
            }
            for entry in registry.entries()
        ]
        return JSONResponse(
            envelope({
                "handlers": handlers,
                "agents": [
                    {**dict(item), "registration_status": "discovered"}
                    for item in agent_catalog
                ],
                "status_semantics": "registration_only",
            })
        )

    async def live_cursor(request: Request) -> JSONResponse:
        actor = authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            previous = decode_cursor(request.query_params.get("cursor"))
        except CursorError as exc:
            return error("invalid_cursor", str(exc))
        marker = change_marker()
        cursor = encode_cursor(marker)
        return JSONResponse(envelope({
            "cursor": cursor,
            "changed": bool(previous) and previous != marker,
            "observed_at": now().isoformat(),
        }))

    # quick_check walks the whole database file; on a grown runtime.db that is
    # seconds, not milliseconds, and Ops/Settings render on every visit. The
    # verdict is cached briefly — counts below stay live on every call.
    integrity_cache: dict[str, Any] = {"verdict": None, "checked_at": None}
    INTEGRITY_TTL_SECONDS = 300.0

    def integrity_verdict() -> tuple[str, str]:
        current = now()
        checked_at = integrity_cache["checked_at"]
        if (
            integrity_cache["verdict"] is None
            or (current - checked_at).total_seconds() >= INTEGRITY_TTL_SECONDS
        ):
            with connect_workflow_database(path, read_only=True) as connection:
                integrity_cache["verdict"] = connection.execute(
                    "PRAGMA quick_check(1)"
                ).fetchone()[0]
            integrity_cache["checked_at"] = current
        return integrity_cache["verdict"], integrity_cache["checked_at"].isoformat()

    async def ops_status(request: Request) -> JSONResponse:
        actor = authenticate(request, OPS_READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        quick, integrity_checked_at = integrity_verdict()
        with connect_workflow_database(path) as connection:
            jobs = {
                row["status"]: int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
                )
            }
            timers = {
                row["status"]: int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM durable_timers GROUP BY status"
                )
            }
            active_leases = int(connection.execute(
                "SELECT COUNT(*) FROM job_leases WHERE status='active'"
            ).fetchone()[0])
            unknown_results = int(connection.execute(
                "SELECT COUNT(*) FROM node_attempts WHERE status='unknown_external_result'"
            ).fetchone()[0])
            migration_version = int(connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM workflow_schema_migrations"
            ).fetchone()[0])
        return JSONResponse(envelope({
            "observed_at": now().isoformat(),
            "integrity": {
                "status": "ok" if quick == "ok" else "failed",
                "check": "sqlite_quick_check", "checked_at": integrity_checked_at,
                "migration_version": migration_version,
            },
            "capacity": {
                "configured_workers": operational_config.get("worker_count"),
                "poll_seconds": operational_config.get("poll_seconds"),
                "ready_jobs": jobs.get("ready", 0),
                "running_jobs": jobs.get("running", 0),
                "leased_jobs": jobs.get("leased", 0),
                "benchmark": {"available": False, "reason": "no_persisted_capacity_report"},
            },
            "durable": {
                "jobs_by_status": jobs, "timers_by_status": timers,
                "active_leases": active_leases,
                "unknown_external_results": unknown_results,
            },
            "server_config": {
                "worker_count": operational_config.get("worker_count"),
                "poll_seconds": operational_config.get("poll_seconds"),
                "artifact_store_configured": artifact_backend is not None,
            },
        }))

    async def capability_read(request: Request) -> JSONResponse:
        """What this deployment can actually do, and why not when it cannot.

        The delivery plan's empty states need three distinguishable answers —
        no data, no permission, not provided — and the client must never learn
        "not provided" by probing for 404s (plan §8, API-7). Capabilities are
        composition facts injected at build time, not guesses.
        """
        actor = authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        # The actor rides along so the shell can display who is signed in
        # without a separate whoami endpoint.
        return JSONResponse(envelope({
            "actor": actor,
            "capabilities": dict(capabilities or {}),
            "permissions": {
                "start_run": guard.allows(actor, WRITE_SCOPE),
                "ops_read": guard.allows(actor, OPS_READ_SCOPE),
                "ops_write": guard.allows(actor, OPS_WRITE_SCOPE),
                # Whether this actor must carry the approval token back. The
                # client is told; it never infers this from being on loopback.
                "human_token_required": actor not in token_exempt_actors,
            },
        }))

    async def workflow_catalog(request: Request) -> JSONResponse:
        actor = authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        may_start = guard.allows(actor, WRITE_SCOPE)
        workflows = workflow_reads.list()
        for item in workflows:
            item["allowed_commands"] = ([{
                    "command": "run.start",
                    "label": "Start run",
                    "method": "POST",
                    "href": "/api/v1/runs",
                    "target_aggregate_id": item["workflow_id"],
                    "expected_version": 0,
                    "payload_schema": "run-start/1.0",
                }] if may_start else [])
        # Generation is a catalog-level act — there is no aggregate yet — so
        # its command is advertised beside the list, not on an entry.
        catalog_commands = ([{
            "command": "workflow.generate",
            "label": "Generate workflow",
            "method": "POST",
            "href": "/api/v1/workflows/generate",
            "target_aggregate_id": "workflow_catalog",
            "expected_version": 0,
            "payload_schema": "workflow-generate/1.0",
        }] if authoring_service is not None and may_start else [])
        return JSONResponse(envelope({
            "workflows": workflows,
            "allowed_commands": catalog_commands,
        }))

    def _publish_command(workflow_id: str, expected_latest_version: int) -> dict[str, Any]:
        return {
            "command": "workflow.publish",
            "label": "Publish workflow",
            "method": "POST",
            "href": f"/api/v1/workflows/{quote(workflow_id, safe=':')}/versions",
            "target_aggregate_id": workflow_id,
            "expected_version": expected_latest_version,
            "payload_schema": "workflow-publish/1.0",
        }

    def _validate_command(workflow_id: str, expected_latest_version: int) -> dict[str, Any]:
        return {
            "command": "workflow.validate",
            "label": "Validate workflow draft",
            "method": "POST",
            "href": "/api/v1/workflows/validate",
            "target_aggregate_id": workflow_id,
            "expected_version": expected_latest_version,
            "payload_schema": "workflow-validate/1.0",
        }

    def _draft_commands(workflow_id: str, latest: int) -> list[dict[str, Any]]:
        return [
            _publish_command(workflow_id, latest),
            _validate_command(workflow_id, latest),
        ]

    async def workflow_generate(request: Request) -> JSONResponse:
        """Natural language → validated DSL draft. Never publishes.

        The draft comes back with the compiler's verdict and a server-advertised
        publish command carrying the current latest version, so the confirming
        click stays inside the AllowedCommand discipline like every other
        mutation.
        """
        if authoring_service is None:
            return error(
                "generation_unavailable",
                "no generation-capable agent CLI was discovered", 503,
            )

        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            from ..workflow.authoring import AuthoringFailedError

            instruction = str(body.get("instruction", ""))
            preferred_handler = body.get("default_agent")
            if preferred_handler is not None and not isinstance(preferred_handler, str):
                raise ValueError("default_agent must be a string")
            description = body.get("description")
            if description is not None and not isinstance(description, str):
                raise ValueError("description must be a string")
            try:
                outcome = authoring_service.generate(
                    instruction, preferred_handler=preferred_handler,
                    agent=_generation_agent(body), description=description,
                )
            except AuthoringFailedError as exc:
                # A model that cannot satisfy the compiler is a client-visible
                # result, not a server fault: return the findings for repair.
                raise ValueError(json.dumps({
                    "message": str(exc),
                    "diagnostics": list(exc.diagnostics),
                }, ensure_ascii=False))
            existing = {
                item["workflow_id"]: item["latest_version"]
                for item in workflow_reads.list()
            }
            latest = existing.get(outcome.workflow_id, 0)
            return {
                "source": outcome.source,
                "workflow_id": outcome.workflow_id,
                "definition_hash": outcome.definition_hash,
                "node_count": outcome.node_count,
                "attempts": outcome.attempts,
                "latest_version": latest,
                "allowed_commands": _draft_commands(outcome.workflow_id, latest),
            }

        return await mutate(request, WRITE_SCOPE, "workflow.generate", command)

    async def workflow_validate(request: Request) -> JSONResponse:
        """Compile an edited draft without publishing or changing state."""

        if workflow_publisher is None:
            return error(
                "validation_unavailable", "workflow validation is not wired", 503,
            )

        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            from ..workflow.dsl import DiagnosticError

            source = body.get("source")
            if not isinstance(source, str) or not source.strip():
                raise ValueError("source is required")
            expected = _required_version(body)
            try:
                compiled = workflow_publisher.validate_workflow(
                    source, source_name="<api-draft>", source_format="json",
                )
            except DiagnosticError as exc:
                raise ValueError(json.dumps({
                    "message": "workflow source failed validation",
                    "diagnostics": [item.to_dict() for item in exc.diagnostics],
                }, ensure_ascii=False))
            workflow_id = compiled.ir.workflow_id
            latest = next((
                item["latest_version"] for item in workflow_reads.list()
                if item["workflow_id"] == workflow_id
            ), 0)
            if expected != latest:
                raise ValueError(
                    f"draft version conflict: expected {expected}, actual {latest}"
                )
            return {
                "source": source,
                "workflow_id": workflow_id,
                "definition_hash": compiled.definition_hash.value,
                "node_count": len(compiled.ir.nodes),
                "latest_version": latest,
                "allowed_commands": _draft_commands(workflow_id, latest),
            }

        return await mutate(request, WRITE_SCOPE, "workflow.validate", command)

    async def workflow_publish(request: Request) -> JSONResponse:
        if workflow_publisher is None:
            return error(
                "publish_unavailable", "workflow publishing is not wired", 503,
            )
        workflow_id = request.path_params["workflow_id"]

        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            from ..workflow.dsl import DiagnosticError
            from ..workflow.persistence import PublishConflictError

            source = body.get("source")
            if not isinstance(source, str) or not source.strip():
                raise ValueError("source is required")
            expected = _required_version(body)
            # Compile-and-check before any write: a body that fails validation
            # or compiles to a different workflow than the route names must
            # leave nothing behind.
            try:
                compiled = workflow_publisher.compile_workflow(
                    source, source_name="<api>", source_format="json",
                )
            except DiagnosticError as exc:
                raise ValueError(json.dumps({
                    "message": "workflow source failed validation",
                    "diagnostics": [item.to_dict() for item in exc.diagnostics],
                }, ensure_ascii=False))
            if compiled.ir.workflow_id != workflow_id:
                raise ValueError(
                    f"source declares {compiled.ir.workflow_id}, route names {workflow_id}"
                )
            try:
                record = workflow_publisher.publish_workflow(
                    source, source_name="<api>", source_format="json",
                    expected_latest_version=expected, actor=actor,
                )
            except PublishConflictError as exc:
                raise ValueError(
                    f"publish conflict: expected {exc.expected}, actual {exc.actual}"
                )
            return {
                "workflow_id": record.workflow_id,
                "version": record.version.value,
                "definition_hash": record.definition_hash.value,
            }

        return await mutate(request, WRITE_SCOPE, "workflow.publish", command)

    async def workflow_detail(request: Request) -> JSONResponse:
        actor = authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            workflow_id = str(EntityId.parse(request.path_params["workflow_id"]))
            if not workflow_id.startswith("workflow:"):
                raise ValueError("workflow id is required")
            raw_version = request.query_params.get("version")
            version = None if raw_version is None else int(raw_version)
            if version is not None and version < 1:
                raise ValueError("version must be positive")
            item = workflow_reads.detail(workflow_id, version)
        except ValueError as exc:
            return error("not_found", str(exc), 404)
        item["allowed_commands"] = ([{
            "command": "run.start",
            "label": "Start run",
            "method": "POST",
            "href": "/api/v1/runs",
            "target_aggregate_id": item["workflow_id"],
            "expected_version": 0,
            "payload_schema": "run-start/1.0",
        }] if guard.allows(actor, WRITE_SCOPE) else [])
        if (
            draft_service is not None
            and getattr(draft_service, "reviser", None) is not None
            and item.get("source_available")
            and guard.allows(actor, WRITE_SCOPE)
        ):
            item["allowed_commands"].append({
                "command": "workflow.draft.create",
                "label": "Edit workflow",
                "method": "POST",
                "href": f"/api/v1/workflows/{quote(workflow_id, safe=':')}/drafts",
                "target_aggregate_id": workflow_id,
                "expected_version": item["selected_version"],
                "payload_schema": "workflow-draft-create/1.0",
            })
        return JSONResponse(envelope(item))

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
        pending, history, undoable = draft_service.revision_context(
            EntityId.parse(record.draft_id), actor=actor,
        )
        commands: list[dict[str, Any]] = []
        if record.status == "active" and guard.allows(actor, WRITE_SCOPE):
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
            elif getattr(draft_service, "reviser", None) is not None:
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
        if draft_service is None:
            return error("drafts_unavailable", "workflow drafts are not wired", 503)
        if getattr(draft_service, "reviser", None) is None:
            return error(
                "generation_unavailable", "no agent reviser is configured", 503,
            )
        workflow_id = request.path_params["workflow_id"]

        def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
            base = body.get("base_version")
            record = draft_service.create_or_resume(
                workflow_id,
                base_version=None if base is None else int(base),
                actor=actor, now=now(),
            )
            return _draft_dto(record, actor)

        return await mutate(request, WRITE_SCOPE, "workflow.draft.create", command)

    async def workflow_draft_read(request: Request) -> JSONResponse:
        if draft_service is None:
            return error("drafts_unavailable", "workflow drafts are not wired", 503)
        actor = authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        from ..workflow.application.workflow_draft_service import DraftNotFoundError

        try:
            record = draft_service.get(
                EntityId.parse(request.path_params["draft_id"]),
                actor=actor, now=now(),
            )
        except (DraftNotFoundError, ValueError) as exc:
            return error("workflow_draft_not_found", str(exc), 404)
        return JSONResponse(envelope(_draft_dto(record, actor)))

    def _draft_mutation(action: str, invoke) -> Callable:
        async def handler(request: Request) -> JSONResponse:
            if draft_service is None:
                return error(
                    "drafts_unavailable", "workflow drafts are not wired", 503,
                )
            draft_id = request.path_params["draft_id"]

            def command(body: Mapping[str, Any], actor: str, key: str) -> Mapping[str, Any]:
                return invoke(EntityId.parse(draft_id), body, actor)

            return await mutate(request, WRITE_SCOPE, action, command)

        return handler

    def _draft_publish(draft_id, body, actor):
        record, version = draft_service.publish(
            draft_id, expected_revision=_required_version(body),
            actor=actor, now=now(),
        )
        return {**_draft_dto(record, actor), "published": version}

    def _draft_discard(draft_id, body, actor):
        record = draft_service.discard(
            draft_id, expected_revision=_required_version(body),
            actor=actor, now=now(),
        )
        return _draft_dto(record, actor)

    def _draft_revise(draft_id, body, actor):
        instruction = body.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("instruction is required")
        agent = _generation_agent(body)
        if agent is not None and authoring_service is not None:
            agent = authoring_service.ensure_agent(agent)
        record = draft_service.revise(
            draft_id, instruction, expected_revision=_required_version(body),
            actor=actor, now=now(), agent=agent,
        )
        return _draft_dto(record, actor)

    def _draft_accept(draft_id, body, actor):
        record = draft_service.accept_revision(
            draft_id, expected_revision=_required_version(body),
            actor=actor, now=now(),
        )
        return _draft_dto(record, actor)

    def _draft_reject(draft_id, body, actor):
        record = draft_service.reject_revision(
            draft_id, expected_revision=_required_version(body),
            actor=actor, now=now(),
        )
        return _draft_dto(record, actor)

    def _draft_undo(draft_id, body, actor):
        record = draft_service.undo_revision(
            draft_id, expected_revision=_required_version(body),
            actor=actor, now=now(),
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
        record = draft_service.cancel_revision(
            draft_id, EntityId.parse(revision_id), actor=actor, now=now(),
        )
        return _draft_dto(record, actor)

    workflow_draft_reject = _draft_mutation("workflow.draft.reject", _draft_reject)
    workflow_draft_undo = _draft_mutation("workflow.draft.undo", _draft_undo)
    workflow_draft_cancel_revision = _draft_mutation(
        "workflow.draft.cancel-revision", _draft_cancel_revision,
    )

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
        except AuthoringFailedError as exc:
            # The agent could not produce a compilable revision. Return its
            # findings so the editor can show them, not a bare 500.
            return error(
                "workflow_revision_failed", str(exc), 422,
                diagnostics=list(exc.diagnostics),
            )
        except UnknownGenerationAgentError as exc:
            return error(
                "unknown_generation_agent", str(exc), 400,
                available=list(exc.available),
            )
        except (AuthoringUnavailableError, RevisionUnavailableError) as exc:
            return error("generation_unavailable", str(exc), 503)
        except ActiveGoalExistsError as exc:
            return error(
                "active_goal_exists", str(exc), 409,
                active_goal=exc.active_goal,
            )
        except (DraftNotFoundError, RevisionNotFoundError) as exc:
            return error("workflow_draft_not_found", str(exc), 404)
        except DraftVersionConflictError as exc:
            return error(
                "draft_version_conflict", str(exc), 409,
                expected=exc.expected, actual=exc.actual,
            )
        except DraftAlreadyActiveError as exc:
            return error("draft_already_active", str(exc), 409, draft=exc.draft)
        except DraftNotValidatedError as exc:
            return error("draft_not_validated", str(exc), 409)
        except DraftSourceTooLargeError as exc:
            return error("workflow_source_too_large", str(exc), 413, size=exc.size)
        except WorkflowVersionConflictError as exc:
            return error(
                "workflow_version_conflict", str(exc), 409,
                base_version=exc.base_version, latest_version=exc.latest_version,
            )
        except SourceUnavailableError as exc:
            return error("source_unavailable", str(exc), 409)
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
            with connect_workflow_database(path) as connection:
                connection.execute(
                    "INSERT OR IGNORE INTO run_artifact_subjects"
                    "(run_id,subject,role,created_at) VALUES (?,?,'owner',?)",
                    (started.run_id, actor, now().isoformat()),
                )
                # Covers run-ingress Artifacts committed inside start_run,
                # before this ownership projection could be written.
                connection.execute(
                    "INSERT OR IGNORE INTO artifact_acl"
                    "(artifact_id,subject,permission,granted_by,created_at)"
                    " SELECT artifact_id,?,'read',?,? FROM artifacts"
                    " WHERE run_id=? AND status='committed'",
                    (actor, actor, now().isoformat(), started.run_id),
                )
                connection.commit()
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

    async def run_output(request: Request) -> JSONResponse:
        """What the Handlers' processes printed, in the order they printed it.

        Not paged by opaque cursor like the projections: this is a tail, and a
        client following a running Agent asks "what is new since chunk N".
        Sensitive scope, because a console holds whatever the Agent echoed.
        """

        actor = authenticate(request, SENSITIVE_SCOPE)
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
        chunks, next_after = attempt_output.read(
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
            return runs.retry_node_run(
                run_id, node_run_id, _required_version(body),
                actor=actor, idempotency_key=key,
                reason=str(body.get("reason", "retried by operator")),
            )

        return await mutate(request, WRITE_SCOPE, "node.retry", command)

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
            version = _required_version(body)
            parsed_task_id = EntityId.parse(task_id)
            if not token and actor in token_exempt_actors:
                # A single-operator Runtime hands this person the token the
                # moment they ask for it, so carrying it back is ceremony, not
                # a check. The Runtime spends one on their behalf instead.
                #
                # Deliberately not a bypass of the token itself: it is minted,
                # rotated and verified exactly as always, and *who may decide*
                # is still the workflow's own answer — an actor the task does
                # not name is refused here as loudly as anywhere else.
                issued = humans.reissue_token(
                    parsed_task_id, actor=actor, expected_version=version,
                    now=now(),
                )
                token = issued["submission_token"]
                version = int(issued["expected_version"])
            if not token:
                raise ValueError("submission_token is required")
            linked = humans.linked_scope(parsed_task_id)
            if linked is not None:
                _node_run_id, run_id = linked
                return durable_service.submit_human_task(
                    parsed_task_id, run_id,
                    version, token=token, decision=decision,
                    value=body.get("value"), actor=actor,
                    idempotency_key=key, now=now(),
                )
            status = humans.submit(
                parsed_task_id, token, decision, body.get("value"),
                actor=actor, expected_version=version, now=now(),
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

        actor = authenticate(request, OPS_READ_SCOPE)
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
                                if guard.allows(actor, OPS_WRITE_SCOPE)
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

            results = recovery.apply_findings(selected, now(), actor=actor)
            return {"results": [result.to_dict() for result in results]}

        return await mutate(request, OPS_WRITE_SCOPE, "recovery.apply", command)

    return [
        Route("/api/v1/dashboard", dashboard, methods=["GET"]),
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
            _paged_read(
                reads.data, SENSITIVE_SCOPE, missing_is_not_found=True,
                pass_actor=True,
            ),
            methods=["GET"],
        ),
        Route(
            "/api/v1/runs/{run_id}/data/{data_id}/lineage", data_lineage,
            methods=["GET"],
        ),
        Route("/api/v1/runs/{run_id}/output", run_output, methods=["GET"]),
        Route("/api/v1/artifacts", artifact_list, methods=["GET"]),
        Route("/api/v1/artifacts/{artifact_id}", artifact_detail, methods=["GET"]),
        Route(
            "/api/v1/artifacts/{artifact_id}/lineage", artifact_lineage,
            methods=["GET"],
        ),
        Route(
            "/api/v1/artifacts/{artifact_id}/content", artifact_content,
            methods=["GET"],
        ),
        Route("/api/v1/runs/{run_id}/cancel", cancel_run, methods=["POST"]),
        Route(
            "/api/v1/runs/{run_id}/node-runs/{node_run_id}/retry",
            retry_node_run, methods=["POST"],
        ),
        Route("/api/v1/runs/{run_id}/plan", plan_definition, methods=["GET"]),
        Route("/api/v1/runs/{run_id}/plan/overlay", plan_overlay, methods=["GET"]),
        Route("/api/v1/runs/{run_id}/plan/diff", plan_diff, methods=["GET"]),
        Route(
            "/api/v1/runs/{run_id}/planner-decisions",
            _paged_read(dynamic_reads.planner_decisions), methods=["GET"],
        ),
        Route(
            "/api/v1/runs/{run_id}/foreach",
            _paged_read(dynamic_reads.foreach_groups), methods=["GET"],
        ),
        Route(
            "/api/v1/runs/{run_id}/foreach/{group_id}/items",
            foreach_items, methods=["GET"],
        ),
        Route(
            "/api/v1/runs/{run_id}/subflows",
            _paged_read(dynamic_reads.subflows), methods=["GET"],
        ),
        Route("/api/v1/runs/{run_id}/graph", run_graph, methods=["GET"]),
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
        Route("/api/v1/live", live_cursor, methods=["GET"]),
        Route("/api/v1/ops/status", ops_status, methods=["GET"]),
        Route("/api/v1/workflows", workflow_catalog, methods=["GET"]),
        Route(
            "/api/v1/workflows/validate", workflow_validate, methods=["POST"]
        ),
        # /generate before /{workflow_id}: Starlette matches in order, and the
        # literal segment must not be captured as a workflow id.
        Route("/api/v1/workflows/generate", workflow_generate, methods=["POST"]),
        Route(
            "/api/v1/workflows/{workflow_id}", workflow_detail, methods=["GET"]
        ),
        Route(
            "/api/v1/workflows/{workflow_id}/versions", workflow_publish,
            methods=["POST"],
        ),
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
        Route("/api/v1/capabilities", capability_read, methods=["GET"]),
    ]
