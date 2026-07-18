"""Versioned HTTP query/command façade with fail-closed mutation boundaries."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any, Callable, Mapping

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..application.run_view_service import RunViewService
from ..domain.ids import EntityId
from ..domain.serialization import canonical_json, definition_hash
from ..observability.diagnostics import DiagnosticsService
from ..persistence.database import connect_workflow_database


MAX_REQUEST_BYTES = 1024 * 1024


def _error(code: str, message: str, status: int = 400, details=None) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "details": details or {}}},
        status_code=status,
    )


class RateLimiter:
    """Bounded per-actor sliding window. IDs never become metric labels."""

    def __init__(self, *, requests: int = 60, window_seconds: float = 60) -> None:
        if requests < 1 or window_seconds <= 0:
            raise ValueError("invalid rate limit")
        self.requests = requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, actor: str, now: float | None = None) -> bool:
        current = monotonic() if now is None else now
        with self._lock:
            hits = self._hits[actor]
            cutoff = current - self.window_seconds
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.requests:
                return False
            hits.append(current)
            return True


class ApiCommandExecutor:
    """At-most-once API execution with a durable pending receipt.

    The pending row is committed before business execution. A crash after the
    business command cannot cause automatic duplicate execution: retries see
    ``command_in_progress``. Business services must additionally implement the
    same idempotency key so an operator can safely reconcile the pending row.
    """

    def __init__(self, path: Path | str, *, fault_hook=None) -> None:
        self.path = Path(path)
        self.fault_hook = fault_hook

    def execute(
        self,
        *,
        actor: str,
        idempotency_key: str,
        method: str,
        request_path: str,
        body: Mapping[str, Any],
        handler: Callable[[Mapping[str, Any], str, str], Mapping[str, Any]],
    ) -> tuple[int, Mapping[str, Any]]:
        request_hash = definition_hash(
            {"method": method, "path": request_path, "body": body}
        ).value
        with connect_workflow_database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                """SELECT * FROM api_command_receipts
                   WHERE actor = ? AND idempotency_key = ?""",
                (actor, idempotency_key),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != request_hash:
                    connection.rollback()
                    raise IdempotencyConflict("key reused with different request")
                if prior["status_code"] == 102:
                    connection.rollback()
                    raise CommandInProgress("prior command outcome requires reconciliation")
                result = json.loads(prior["response_json"])
                status = prior["status_code"]
                connection.commit()
                return status, result
            connection.execute(
                """INSERT INTO api_command_receipts(
                       actor, idempotency_key, request_hash, status_code,
                       response_json, created_at
                   ) VALUES (?, ?, ?, 102, ?, ?)""",
                (
                    actor,
                    idempotency_key,
                    request_hash,
                    canonical_json({"state": "pending"}),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()

        try:
            result = handler(body, actor, idempotency_key)
        except (ValueError, PermissionError):
            # These application errors are transactionally known not to have
            # committed a business mutation, so the key may be retried.
            with connect_workflow_database(self.path) as connection:
                connection.execute(
                    """DELETE FROM api_command_receipts
                       WHERE actor = ? AND idempotency_key = ?
                         AND request_hash = ? AND status_code = 102""",
                    (actor, idempotency_key, request_hash),
                )
            raise
        if self.fault_hook is not None:
            self.fault_hook("after_business_before_api_receipt")
        primitive = json.loads(canonical_json(result))
        with connect_workflow_database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """UPDATE api_command_receipts
                   SET status_code = 200, response_json = ?
                   WHERE actor = ? AND idempotency_key = ?
                     AND request_hash = ? AND status_code = 102""",
                (canonical_json(primitive), actor, idempotency_key, request_hash),
            )
            if updated.rowcount != 1:
                connection.rollback()
                raise RuntimeError("API receipt finalize conflict")
            connection.commit()
        return 200, primitive

    def reconcile_pending(
        self,
        *,
        actor: str,
        idempotency_key: str,
        verifier: Callable[[str], Mapping[str, Any] | None],
    ) -> tuple[int, Mapping[str, Any]]:
        """Finalize a crash-window receipt only from verified domain facts."""
        with connect_workflow_database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM api_command_receipts
                   WHERE actor=? AND idempotency_key=?""",
                (actor, idempotency_key),
            ).fetchone()
            if row is None or row["status_code"] != 102:
                raise ValueError("pending API receipt not found")
            result = verifier(row["request_hash"])
            if result is None:
                connection.rollback()
                raise ValueError("business outcome cannot be proven")
            primitive = json.loads(canonical_json(result))
            connection.execute(
                """UPDATE api_command_receipts
                   SET status_code=200,response_json=?
                   WHERE actor=? AND idempotency_key=? AND status_code=102""",
                (canonical_json(primitive), actor, idempotency_key),
            )
            connection.commit()
            return 200, primitive


class IdempotencyConflict(ValueError):
    pass


class CommandInProgress(RuntimeError):
    pass


async def _bounded_json(request: Request) -> Mapping[str, Any]:
    raw_length = request.headers.get("content-length")
    if raw_length is not None and int(raw_length) > MAX_REQUEST_BYTES:
        raise RequestTooLarge
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        raise RequestTooLarge
    value = json.loads(raw)
    if not isinstance(value, Mapping):
        raise ValueError("request body must be a JSON object")
    return value


class RequestTooLarge(ValueError):
    pass


def build_workflow_api(
    path: Path | str,
    *,
    workflow_service=None,
    human_service=None,
    budget_service=None,
    capability_service=None,
    authenticator: Callable[[Request], str | None] | None = None,
    rate_limiter: RateLimiter | None = None,
    fault_hook=None,
) -> Starlette:
    db_path = Path(path)
    views = RunViewService(db_path)
    diagnostics = DiagnosticsService(db_path)
    limiter = rate_limiter or RateLimiter()
    executor = ApiCommandExecutor(db_path, fault_hook=fault_hook)

    async def actor_for(request: Request) -> str | JSONResponse:
        if authenticator is None:
            return _error("unauthenticated", "mutation authentication is not configured", 401)
        actor = authenticator(request)
        if actor is None or not actor.strip():
            return _error("unauthenticated", "valid actor credentials are required", 401)
        if not limiter.allow(actor):
            return _error("rate_limited", "mutation rate limit exceeded", 429)
        return actor

    async def mutate(request: Request, handler) -> JSONResponse:
        actor = await actor_for(request)
        if isinstance(actor, JSONResponse):
            return actor
        key = request.headers.get("idempotency-key", "").strip()
        if not key:
            return _error("invalid_command", "idempotency-key is required")
        try:
            body = await _bounded_json(request)
            status, result = executor.execute(
                actor=actor,
                idempotency_key=key,
                method=request.method,
                request_path=request.url.path,
                body=body,
                handler=handler,
            )
            return JSONResponse(result, status_code=status)
        except RequestTooLarge:
            return _error("request_too_large", f"request exceeds {MAX_REQUEST_BYTES} bytes", 413)
        except json.JSONDecodeError:
            return _error("invalid_json", "request body must be JSON")
        except IdempotencyConflict as exc:
            return _error("idempotency_conflict", str(exc), 409)
        except CommandInProgress as exc:
            return _error("command_in_progress", str(exc), 409)
        except PermissionError as exc:
            return _error("forbidden", str(exc), 403)
        except ValueError as exc:
            return _error("invalid_command", str(exc), 409)

    async def run_view(request: Request):
        try:
            return JSONResponse(
                views.get(
                    EntityId.parse(request.path_params["run_id"]),
                    after_event=int(request.query_params.get("after", 0)),
                    event_limit=int(request.query_params.get("limit", 200)),
                    plan_version=(
                        None
                        if "plan_version" not in request.query_params
                        else int(request.query_params["plan_version"])
                    ),
                )
            )
        except ValueError as exc:
            return _error("not_found", str(exc), 404)

    async def why(request: Request):
        try:
            return JSONResponse(
                diagnostics.why(EntityId.parse(request.path_params["run_id"]))
            )
        except ValueError as exc:
            return _error("not_found", str(exc), 404)

    async def submit_human(request: Request):
        if human_service is None:
            return _error("unavailable", "HumanTask service unavailable", 503)

        def command(body, actor, key):
            status = human_service.submit(
                EntityId.parse(request.path_params["task_id"]),
                body["submission_token"],
                body["decision"],
                body.get("value"),
                actor=actor,
                expected_version=int(body["expected_version"]),
                now=datetime.now(timezone.utc),
            )
            return {"status": status.value}

        return await mutate(request, command)

    async def add_budget(request: Request):
        if budget_service is None:
            return _error("unavailable", "Budget service unavailable", 503)

        def command(body, actor, key):
            account = budget_service.add_budget(
                EntityId.parse(request.path_params["run_id"]),
                int(body["amount_microunits"]),
                actor=actor,
                now=datetime.now(timezone.utc),
                idempotency_key=key,
            )
            return {"account": account}

        return await mutate(request, command)

    async def list_workflows(request: Request):
        limit = min(500, max(1, int(request.query_params.get("limit", 100))))
        with connect_workflow_database(db_path, read_only=True) as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    """SELECT d.workflow_id, d.name, MAX(v.version) latest_version
                       FROM workflow_definitions d
                       JOIN workflow_versions v ON v.workflow_id = d.workflow_id
                       GROUP BY d.workflow_id, d.name
                       ORDER BY d.workflow_id LIMIT ?""",
                    (limit,),
                )
            ]
        return JSONResponse({"items": rows})

    async def validate_workflow(request: Request):
        if workflow_service is None:
            return _error("unavailable", "Workflow service unavailable", 503)
        try:
            body = await _bounded_json(request)
            compiled = workflow_service.validate_workflow(
                body["source"],
                source_name=body.get("source_name", "api"),
                source_format=body.get("source_format"),
            )
            return JSONResponse(
                {
                    "definition_hash": compiled.definition_hash.value,
                    "ir": json.loads(canonical_json(compiled.ir)),
                }
            )
        except RequestTooLarge:
            return _error("request_too_large", "request is too large", 413)
        except Exception as exc:
            return _error("workflow_invalid", str(exc), 422)

    async def publish_workflow(request: Request):
        if workflow_service is None:
            return _error("unavailable", "Workflow service unavailable", 503)

        def command(body, actor, key):
            record = workflow_service.publish_workflow(
                body["source"],
                source_name=body.get("source_name", "api"),
                source_format=body["source_format"],
                expected_latest_version=int(body["expected_latest_version"]),
                actor=actor,
            )
            return {"version": record}

        return await mutate(request, command)

    async def collection(request: Request):
        run_id = str(EntityId.parse(request.path_params["run_id"]))
        kind = request.path_params["kind"]
        mapping = {
            "events": (
                "SELECT * FROM run_events WHERE run_id=? AND global_position>? ORDER BY global_position LIMIT ?",
                "global_position",
            ),
            "plans": (
                "SELECT * FROM execution_plans WHERE run_id=? AND plan_version>? ORDER BY plan_version LIMIT ?",
                "plan_version",
            ),
            "proposals": (
                "SELECT rowid AS cursor,* FROM planner_proposals WHERE run_id=? AND rowid>? ORDER BY rowid LIMIT ?",
                "cursor",
            ),
            "human-tasks": (
                "SELECT rowid AS cursor,* FROM human_tasks WHERE run_id=? AND rowid>? ORDER BY rowid LIMIT ?",
                "cursor",
            ),
        }
        if kind not in mapping:
            return _error("not_found", "collection not found", 404)
        cursor = int(request.query_params.get("after", 0))
        limit = min(500, max(1, int(request.query_params.get("limit", 100))))
        sql, cursor_field = mapping[kind]
        with connect_workflow_database(db_path, read_only=True) as connection:
            rows = []
            for row in connection.execute(sql, (run_id, cursor, limit)):
                item = dict(row)
                for key, value in tuple(item.items()):
                    if key.endswith("_json") and value is not None:
                        item[key[:-5]] = json.loads(value)
                        del item[key]
                rows.append(item)
        return JSONResponse(
            {
                "items": rows,
                "next_cursor": (
                    None if len(rows) < limit else rows[-1].get(cursor_field)
                ),
            }
        )

    return Starlette(
        routes=[
            Route("/api/v1/workflows", list_workflows),
            Route("/api/v1/workflows/validate", validate_workflow, methods=["POST"]),
            Route("/api/v1/workflows/publish", publish_workflow, methods=["POST"]),
            Route("/api/v1/runs/{run_id}", run_view),
            Route("/api/v1/runs/{run_id}/diagnostics", why),
            Route("/api/v1/runs/{run_id}/{kind}", collection),
            Route("/api/v1/human-tasks/{task_id}/submit", submit_human, methods=["POST"]),
            Route("/api/v1/runs/{run_id}/budget", add_budget, methods=["POST"]),
        ]
    )
