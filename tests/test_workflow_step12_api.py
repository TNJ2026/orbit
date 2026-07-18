from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from starlette.requests import Request

from orbit.workflow.api.routes import (
    ApiCommandExecutor, CommandInProgress, IdempotencyConflict,
    MAX_REQUEST_BYTES, RateLimiter, RequestTooLarge, _bounded_json,
)
from orbit.workflow.api import build_workflow_api
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.migrations import migrate_workflow_database


class ApiBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "api.db"
        with connect_workflow_database(self.path) as connection:
            migrate_workflow_database(connection)

    def test_receipt_replays_without_reexecuting_handler(self):
        calls = []
        executor = ApiCommandExecutor(self.path)

        def handler(body, actor, key):
            calls.append((body, actor, key))
            return {"ok": True}

        first = executor.execute(
            actor="user:1", idempotency_key="same", method="POST",
            request_path="/command", body={"x": 1}, handler=handler,
        )
        second = executor.execute(
            actor="user:1", idempotency_key="same", method="POST",
            request_path="/command", body={"x": 1}, handler=handler,
        )
        self.assertEqual(first, second)
        self.assertEqual(1, len(calls))

    def test_same_key_with_different_request_conflicts(self):
        executor = ApiCommandExecutor(self.path)
        executor.execute(
            actor="user:1", idempotency_key="same", method="POST",
            request_path="/command", body={"x": 1},
            handler=lambda *_: {"ok": True},
        )
        with self.assertRaises(IdempotencyConflict):
            executor.execute(
                actor="user:1", idempotency_key="same", method="POST",
                request_path="/command", body={"x": 2},
                handler=lambda *_: {"ok": True},
            )

    def test_crash_after_business_never_automatically_executes_twice(self):
        calls = []

        def fault(point):
            if point == "after_business_before_api_receipt":
                raise RuntimeError("kill")

        executor = ApiCommandExecutor(self.path, fault_hook=fault)
        with self.assertRaisesRegex(RuntimeError, "kill"):
            executor.execute(
                actor="user:1", idempotency_key="crash", method="POST",
                request_path="/command", body={"x": 1},
                handler=lambda *_: calls.append("business") or {"ok": True},
            )
        with self.assertRaises(CommandInProgress):
            executor.execute(
                actor="user:1", idempotency_key="crash", method="POST",
                request_path="/command", body={"x": 1},
                handler=lambda *_: calls.append("duplicate") or {"ok": True},
            )
        self.assertEqual(["business"], calls)

    def test_pending_receipt_reconciliation_requires_verified_domain_fact(self):
        executor = ApiCommandExecutor(
            self.path,
            fault_hook=lambda point: (_ for _ in ()).throw(RuntimeError("kill")),
        )
        with self.assertRaises(RuntimeError):
            executor.execute(
                actor="user:1", idempotency_key="pending", method="POST",
                request_path="/command", body={"x": 1},
                handler=lambda *_: {"ledger_entry_id": "ledger_entry:1"},
            )
        with self.assertRaisesRegex(ValueError, "cannot be proven"):
            executor.reconcile_pending(
                actor="user:1", idempotency_key="pending",
                verifier=lambda request_hash: None,
            )
        reconciled = executor.reconcile_pending(
            actor="user:1", idempotency_key="pending",
            verifier=lambda request_hash: {"verified": request_hash},
        )
        self.assertEqual(200, reconciled[0])

    def test_known_application_rejection_releases_pending_key(self):
        executor = ApiCommandExecutor(self.path)
        with self.assertRaisesRegex(ValueError, "invalid"):
            executor.execute(
                actor="user:1", idempotency_key="retry", method="POST",
                request_path="/command", body={},
                handler=lambda *_: (_ for _ in ()).throw(ValueError("invalid")),
            )
        status, result = executor.execute(
            actor="user:1", idempotency_key="retry", method="POST",
            request_path="/command", body={}, handler=lambda *_: {"ok": True},
        )
        self.assertEqual((200, {"ok": True}), (status, result))

    def test_rate_limiter_is_per_actor_and_windowed(self):
        limiter = RateLimiter(requests=2, window_seconds=10)
        self.assertTrue(limiter.allow("a", now=0))
        self.assertTrue(limiter.allow("a", now=1))
        self.assertFalse(limiter.allow("a", now=2))
        self.assertTrue(limiter.allow("b", now=2))
        self.assertTrue(limiter.allow("a", now=11))

    def test_body_size_is_checked_even_without_content_length(self):
        body = b"{" + b'"x":"' + b"a" * MAX_REQUEST_BYTES + b'"}'
        request = self._request(body)
        with self.assertRaises(RequestTooLarge):
            asyncio.run(_bounded_json(request))

    def test_mutation_authentication_is_fail_closed_not_actor_header_trust(self):
        api = build_workflow_api(self.path, budget_service=object())
        route = next(item for item in api.routes if item.path.endswith("/budget"))
        request = self._request(b"{}")
        request.scope["path_params"] = {"run_id": "run:test"}
        response = asyncio.run(route.endpoint(request))
        self.assertEqual(401, response.status_code)
        self.assertEqual("unauthenticated", json.loads(response.body)["error"]["code"])

    @staticmethod
    def _request(body: bytes) -> Request:
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return Request(
            {
                "type": "http", "http_version": "1.1", "method": "POST",
                "scheme": "http", "path": "/", "raw_path": b"/",
                "query_string": b"", "headers": [],
                "client": ("test", 1), "server": ("test", 80),
            },
            receive,
        )


if __name__ == "__main__":
    unittest.main()
