"""Versioned HTTP query/command façade with fail-closed mutation boundaries."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any, Callable, Mapping

from starlette.requests import Request

from ..domain.serialization import canonical_json, definition_hash
from ..persistence.database import connect_workflow_database


MAX_REQUEST_BYTES = 1024 * 1024


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
