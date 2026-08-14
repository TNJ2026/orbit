"""`ApiContext` — the stateful half of the `/api/v1` surface.

One context per mounted API: it owns every service the route modules project
onto HTTP, plus the cross-cutting helpers (authentication, paging, the write
boundary) that used to be closures shared by every route in the old single
file. Route modules receive the context and stay thin.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json

from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from starlette.requests import Request
from starlette.responses import JSONResponse

from ...workflow.api.dto import envelope
from ...workflow.api.workflow_catalog import WorkflowCatalogReadModelService
from ...workflow.api.routes import (
    ApiCommandExecutor, CommandInProgress, IdempotencyConflict, RateLimiter,
    RequestTooLarge, _bounded_json,
)
from ...workflow.application.authoring_job_service import AuthoringJobService
from ...workflow.catalogs.schemas import InMemorySchemaCatalog
from ...workflow.langgraph_runtime.service import ActiveGoalExists
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

from .common import Authorizer, authoring_timeout_seconds, error


class ApiContext:
    """Services and cross-cutting helpers shared by every `/api/v1` route."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        execution_registry=None,
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
        workflow_ui_mode: str = "multi-agent",
    ) -> None:
        path = Path(db_path)
        workflow_path = Path(workflow_db_path or db_path)
        self.path = path
        self.workflow_path = workflow_path
        # The sealed Handler registry, which is what the catalog and the
        # authoring surface actually asked the old application service for.
        self.execution_registry = execution_registry
        self.langgraph_service = langgraph_service
        self.workflow_ui_mode = workflow_ui_mode
        self.artifact_backend = artifact_backend
        self.workflow_reads = WorkflowCatalogReadModelService(
            workflow_path, schema_catalog or InMemorySchemaCatalog({}),
            # Optional on purpose: an embedder may wire a service that only
            # runs workflows, and the catalog then simply reports no usage.
            usage_source=getattr(langgraph_service, "workflow_usage", None),
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

    def recent_handler_attempts(
        self,
    ) -> tuple[dict[str, Mapping[str, Any]], dict[str, int], dict[str, int]]:
        """Latest attempt plus total and failed counts per Handler.

        Read from the engine that runs them. The counts used to come from the
        job table of a second engine, and once that engine stopped running
        they were structurally zero — a Handler page that reported "never
        used" for a Handler used every day.
        """

        source = getattr(self.langgraph_service, "handler_attempts", None)
        if source is None:
            return {}, {}, {}
        stats = source()
        recent = {
            name: entry["recent"]
            for name, entry in stats.items() if entry["recent"] is not None
        }
        counts = {name: entry["total"] for name, entry in stats.items()}
        failed = {name: entry["failed"] for name, entry in stats.items()}
        return recent, counts, failed

    def change_marker(self) -> Mapping[str, Any]:
        """A value that changes exactly when something a client polls changed.

        Audit position covers authoring and drafts; the engine's own run clock
        covers execution. It used to read the job and timer tables of the
        engine that has since been deleted, which froze the marker and made
        every poll answer "nothing changed".
        """

        with connect_workflow_database(self.path) as connection:
            audit_position = connection.execute(
                "SELECT COUNT(*) FROM audit_records"
            ).fetchone()[0]
        source = getattr(self.langgraph_service, "last_change", None)
        engine_updated = "" if source is None else (source() or "")
        return {
            "audit_position": int(audit_position),
            "engine_updated": engine_updated,
        }

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
        except ActiveGoalExists as exc:
            # The run holding the slot travels with the refusal: the client
            # takes the person to it rather than reporting a dead end.
            return error(
                "active_goal_exists", str(exc), 409,
                active_goal=exc.active_goal,
            )
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
        except ValueError as exc:
            # A run refused because its definition names a Handler build that
            # is no longer registered is not "you raced someone" — retrying
            # cannot help. Give it a code of its own so the client stops
            # telling the operator to reload and confirm.
            if "HANDLER_UNAVAILABLE" in str(exc):
                return error("handler_unavailable", str(exc), 409)
            return error("invalid_command", str(exc), 409)
        self.record_audit(actor, action, {"path": request.url.path, "key": key})
        return JSONResponse(envelope(result), status_code=status)
