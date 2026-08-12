"""`ApiContext` — the stateful half of the `/api/v1` surface.

One context per mounted API: it owns every service the route modules project
onto HTTP, plus the cross-cutting helpers (authentication, paging, the write
boundary) that used to be closures shared by every route in the old single
file. Route modules receive the context and stay thin.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from starlette.requests import Request
from starlette.responses import JSONResponse

from ...workflow.api.dto import CursorError, envelope, page_size
from ...workflow.api.artifact_read_models import (
    ArtifactReadModelService, PREVIEW_LIMIT_BYTES,
)
from ...workflow.api.plan_read_models import PlanReadModelService
from ...workflow.api.dynamic_read_models import DynamicReadModelService
from ...workflow.api.read_models import ReadModelService
from ...workflow.api.workflow_catalog import WorkflowCatalogReadModelService
from ...workflow.persistence.attempt_output import SQLiteAttemptOutputStore
from ...workflow.api.routes import (
    ApiCommandExecutor, CommandInProgress, IdempotencyConflict, RateLimiter,
    RequestTooLarge, _bounded_json,
)
from ...workflow.application.budget_service import (
    BudgetService, BudgetVersionConflict,
)
from ...workflow.application.foreach_service import ForeachService
from ...workflow.application.human_service import HumanTaskService
from ...workflow.application.authoring_job_service import AuthoringJobService
from ...workflow.application.run_service import (
    ActiveGoalExistsError,
    RunApplicationService,
    RunStartError,
)
from ...workflow.catalogs.schemas import InMemorySchemaCatalog
from ...workflow.domain.ids import EntityId
from ...workflow.authoring import (
    AuthoringFailedError, AuthoringUnavailableError,
    UnknownGenerationAgentError,
)
from ...workflow.application.workflow_draft_service import (
    DraftAlreadyActiveError, DraftNotFoundError, DraftNotValidatedError,
    DraftSourceTooLargeError, DraftVersionConflictError, RevisionNotFoundError,
    RevisionUnavailableError, SourceUnavailableError,
    WorkflowVersionConflictError,
)
from ...workflow.persistence.database import connect_workflow_database
from ...workflow.persistence.control import audit as persist_audit
from ...workflow.recovery.manager import RecoveryManager

from .common import (
    OPS_WRITE_SCOPE, READ_SCOPE, WRITE_SCOPE, Authorizer,
    authoring_timeout_seconds, error,
)


class ApiContext:
    """Services and cross-cutting helpers shared by every `/api/v1` route."""

    def __init__(
        self,
        db_path: Path | str,
        durable_service,
        *,
        workflow_db_path: Path | str | None = None,
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
        authoring_jobs=None,
        shutdown_request: Callable[[], None] | None = None,
        langgraph_service=None,
        legacy_execution: bool = True,
    ) -> None:
        path = Path(db_path)
        workflow_path = Path(workflow_db_path or db_path)
        self.path = path
        self.workflow_path = workflow_path
        self.durable_service = durable_service
        self.langgraph_service = langgraph_service
        self.legacy_execution = bool(legacy_execution)
        self.artifact_backend = artifact_backend or getattr(
            durable_service, "artifact_backend", None
        )
        self.reads = ReadModelService(path)
        self.artifact_reads = ArtifactReadModelService(
            path, blob_reader=self.read_preview_blob
        )
        self.runs = RunApplicationService(
            path, durable_service, enforce_single_goal=single_goal_mode,
            workflow_db_path=workflow_path,
        )
        self.plans = PlanReadModelService(path)
        self.dynamic_reads = DynamicReadModelService(path)
        self.workflow_reads = WorkflowCatalogReadModelService(
            workflow_path, schema_catalog or InMemorySchemaCatalog({}),
            usage_path=path,
        )
        # One per process, never one per protocol. Constructing a second
        # against the same database means two recoveries: each restarts every
        # queued job on its own thread, so one authoring job runs the Agent
        # CLI twice, and the one built second marks the first one's running
        # jobs as failed.
        self.authoring_jobs = authoring_jobs or (
            AuthoringJobService(
                path, authoring_service, workflow_publisher,
                workflow_db_path=workflow_path,
                # Without a deadline an authoring job has no terminal state of
                # its own: the generator CLI bounds each call, but nothing
                # bounds the retries and the publish that follow them, so a
                # wedged job stays `running` for as long as the process lives.
                timeout_seconds=authoring_timeout_seconds(operational_config),
                clock=clock,
            )
            if authoring_service is not None and workflow_publisher is not None
            else None
        )
        self.humans = HumanTaskService(path)
        # Handler console output: an observation store, not a projection of
        # events, so it is read directly rather than through ReadModelService.
        self.attempt_output = SQLiteAttemptOutputStore(path)
        self.budgets = BudgetService(path)
        # Every service a finding can be applied through. Recovery that
        # detects a problem it cannot act on is worse than not detecting it:
        # the operator is told the runtime knows, and the fix fails.
        self.recovery = RecoveryManager(
            path, durable_service=durable_service, human_service=self.humans,
            foreach_service=ForeachService(path),
            # A takeover is answered by a person. The composition root is the
            # only place that knows who that is here.
            takeover_participants=tuple(operator_actors),
        )
        self.limiter = rate_limiter or RateLimiter()
        # The limit exists to keep a shared deployment from being drowned by
        # one caller. An actor the composition root vouches for — the single
        # operator on loopback — is not that caller, and its own UI polling
        # should never lock it out of its own Runtime.
        self.exempt_from_limit = frozenset(unlimited_actors)
        # Actors the composition root vouches for as the person at the
        # keyboard. They still need the token — the Runtime just stops asking
        # them to carry it back and forth to themselves.
        self.token_exempt_actors = frozenset(token_exempt_actors)
        self.operators = frozenset(operator_actors)
        self.executor = ApiCommandExecutor(path, fault_hook=fault_hook)
        self.guard = authorizer or Authorizer()
        self.record_audit = audit or (lambda actor, action, detail: None)
        self.now = clock or (lambda: datetime.now(timezone.utc))
        self.operational_config = dict(operational_config or {})
        self.authenticator = authenticator
        self.agent_catalog = agent_catalog
        self.capabilities = capabilities
        self.schema_catalog = schema_catalog
        self.draft_service = draft_service
        self.workflow_publisher = workflow_publisher
        self.authoring_service = authoring_service
        self.shutdown_request = shutdown_request
        self.single_goal_mode = single_goal_mode

    def read_preview_blob(self, blob_key: str) -> bytes:
        if self.artifact_backend is None:
            raise LookupError("Artifact store is unavailable")
        return self.artifact_backend.read(
            blob_key, max_size_bytes=PREVIEW_LIMIT_BYTES
        )

    def recent_handler_attempts(
        self,
    ) -> tuple[dict[str, Mapping[str, Any]], dict[str, int], dict[str, int]]:
        """Latest durable attempt plus total and failed job counts per handler.

        A job is one scheduled execution of a node (retries stay inside it),
        so the count answers "how many times has this handler run" without
        ever posing as a heartbeat.
        """

        with connect_workflow_database(self.path) as connection:
            count_rows = connection.execute(
                "SELECT job_kind AS handler_name, COUNT(*) AS total,"
                " SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed"
                " FROM jobs GROUP BY job_kind"
            ).fetchall()
            counts = {
                str(row["handler_name"]): int(row["total"])
                for row in count_rows
            }
            failed_counts = {
                str(row["handler_name"]): int(row["failed"])
                for row in count_rows
            }
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
        recent = {
            str(row["handler_name"]): {
                "run_id": row["run_id"], "node_id": row["node_id"],
                "attempt_id": row["attempt_id"], "status": row["status"],
                "occurred_at": row["updated_at"],
            }
            for row in rows
        }
        return recent, counts, failed_counts

    def change_marker(self) -> Mapping[str, Any]:
        with connect_workflow_database(self.path) as connection:
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
        self,
        actor: str, action: str, artifact_id: str, decision: str,
        *, run_id: str | None = None, details: Mapping[str, Any] | None = None,
    ) -> None:
        # Denied ids are hashed so the audit store cannot become an oracle for
        # Artifact identities the actor was not allowed to enumerate.
        target = artifact_id if decision == "allowed" else (
            "artifact_ref_hash:" + hashlib.sha256(artifact_id.encode()).hexdigest()
        )
        with connect_workflow_database(self.path) as connection:
            persist_audit(
                connection,
                run_id=None if run_id is None else EntityId.parse(run_id),
                actor=actor, action=action, target_id=target,
                decision=decision, details=dict(details or {}),
                occurred_at=self.now(),
            )
            connection.commit()

    def authenticate(self, request: Request, scope: str) -> str | JSONResponse:
        if self.authenticator is None:
            return error("unauthenticated", "authentication is not configured", 401)
        actor = self.authenticator(request)
        if not actor or not actor.strip():
            return error("unauthenticated", "valid actor credentials are required", 401)
        if not self.guard.allows(actor, scope):
            return error("forbidden", f"actor lacks scope {scope}", 403)
        if actor not in self.exempt_from_limit and not self.limiter.allow(actor):
            return error("rate_limited", "request rate limit exceeded", 429)
        return actor

    def read_params(self, request: Request) -> tuple[str | None, int]:
        return (
            request.query_params.get("cursor") or None,
            page_size(request.query_params.get("limit")),
        )

    def command_factory(self, actor: str):
        """Commands are authorised before they are advertised (plan B1).

        A reader who cannot execute a mutation must not be shown its button:
        an inbox full of buttons that 403 teaches people the UI lies. The
        server still re-checks scope on submission — this only shapes what is
        offered.
        """
        if self.guard.allows(actor, WRITE_SCOPE):
            return None  # read model default: full command set
        return lambda record, *, run_id, run_version: ()

    def paged_read(
        self,
        loader, scope: str = READ_SCOPE, *, missing_is_not_found=False,
        pass_actor: bool = False,
    ):
        async def handler(request: Request) -> JSONResponse:
            actor = self.authenticate(request, scope)
            if isinstance(actor, JSONResponse):
                return actor
            try:
                cursor, limit = self.read_params(request)
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

    async def mutate(
        self, request: Request, scope: str, action: str, handler
    ) -> JSONResponse:
        actor = self.authenticate(request, scope)
        if isinstance(actor, JSONResponse):
            return actor
        key = request.headers.get("idempotency-key", "").strip()
        if not key:
            return error("invalid_command", "idempotency-key header is required")
        try:
            body = await _bounded_json(request)
            status, result = self.executor.execute(
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
            # A run refused because its plan names a Handler build that is no
            # longer registered is not "you raced someone" — retrying cannot
            # help. Give it a code of its own so the client stops telling the
            # operator to reload and confirm.
            if "HANDLER_UNAVAILABLE" in str(exc):
                return error("handler_unavailable", str(exc), 409)
            return error("invalid_command", str(exc), 409)
        self.record_audit(actor, action, {"path": request.url.path, "key": key})
        return JSONResponse(envelope(result), status_code=status)
