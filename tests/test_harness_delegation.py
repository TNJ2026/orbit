from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
from threading import Thread
import time
import unittest

from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.definitions import IRHandlerRef, IRNode
from orbit.workflow.domain.handlers import CancelDisposition
from orbit.workflow.langgraph_runtime.harness_subagent import (
    DelegationQueue, HARNESS_SUBAGENT_MANIFEST, HarnessSubagentHandler,
)
from orbit.workflow.langgraph_runtime.wiring import trusted_handlers
from orbit.web.app import HandlerRegistration
from orbit.web.api_v1 import Authorizer, READ_SCOPE, WRITE_SCOPE
from orbit.web.mcp import build_mcp_dispatcher


class DelegationQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.queue = DelegationQueue(Path(self.temp.name) / "delegations.db")
        self.queue.configure_execution_lease(
            actor="session:1", lease_id="lease:1", workspace_id="workspace:1",
            allowed_providers=("codex",), max_delegations=100,
            max_wall_seconds=7200,
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_duplicate_submission_is_one_job_and_changed_payload_is_refused(self) -> None:
        first = self.queue.enqueue("delegation:1", actor="session:1", request={"task": 1})
        second = self.queue.enqueue("delegation:1", actor="session:1", request={"task": 1})
        self.assertEqual(first, second)
        with self.assertRaisesRegex(ValueError, "different request"):
            self.queue.enqueue("delegation:1", actor="session:1", request={"task": 2})

    def test_claim_is_actor_scoped_and_settles_exactly_once(self) -> None:
        self.queue.enqueue("delegation:1", actor="session:1", request={
            "input": {"task": 1}, "config": {"provider": "codex"},
        })
        self.assertIsNone(self.queue.claim(actor="session:2", worker_id="worker:2"))
        claimed = self.queue.claim(actor="session:1", worker_id="worker:1")
        self.assertEqual("leased", claimed["status"])
        self.assertIsNone(self.queue.claim(actor="session:1", worker_id="worker:other"))
        done = self.queue.complete(
            "delegation:1", actor="session:1", worker_id="worker:1",
            result={"answer": 42},
        )
        self.assertEqual("succeeded", done["status"])
        with self.assertRaisesRegex(ValueError, "no longer completable"):
            self.queue.complete(
                "delegation:1", actor="session:1", worker_id="worker:1",
                result={"answer": 43},
            )

    def test_expired_lease_becomes_unknown_and_is_never_requeued(self) -> None:
        self.queue.enqueue("delegation:1", actor="session:1", request={
            "input": {"task": 1}, "config": {"provider": "codex"},
        })
        self.queue.claim(actor="session:1", worker_id="worker:1")
        with self.queue._connect() as db:
            db.execute(
                "UPDATE harness_delegations SET lease_expires_at=? WHERE delegation_id=?",
                ("2000-01-01T00:00:00.000000Z", "delegation:1"),
            )
            db.commit()
        reopened = DelegationQueue(self.queue.path)
        self.assertIsNone(reopened.claim(actor="session:1", worker_id="worker:2"))
        item = reopened.get("delegation:1", actor="session:1")
        self.assertEqual("unknown", item["status"])
        self.assertIn("reconciliation required", item["error"])

    def test_cancelled_lease_is_observed_by_the_worker(self) -> None:
        self.queue.enqueue("delegation:1", actor="session:1", request={
            "input": {"task": 1}, "config": {"provider": "codex"},
        })
        self.queue.claim(actor="session:1", worker_id="worker:1")
        acknowledgement = self.queue.cancel("delegation:1")
        self.assertEqual(CancelDisposition.UNKNOWN, acknowledgement.disposition)
        renewed = self.queue.renew(
            "delegation:1", actor="session:1", worker_id="worker:1",
        )
        self.assertTrue(renewed["cancel_requested"])

    def test_harness_mcp_claims_and_completes_the_same_queue(self) -> None:
        self.queue.enqueue(
            "delegation:1", actor="session:1",
            request={"input": {"task": "fix"}, "config": {"provider": "codex"}},
        )
        dispatch = build_mcp_dispatcher(
            Path(self.temp.name) / "runtime.db",
            authorizer=Authorizer(lambda _actor: (READ_SCOPE, WRITE_SCOPE)),
            delegation_queue=self.queue, tool_profile="harness",
        )
        listed = dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "session:1")
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertIn("claim_delegation", names)
        self.assertIn("configure_execution_lease", names)
        configured = dispatch({
            "jsonrpc": "2.0", "id": 10, "method": "tools/call",
            "params": {"name": "configure_execution_lease", "arguments": {
                "lease_id": "lease:1", "workspace_id": "workspace:1",
                "allowed_providers": ["codex"], "max_delegations": 100,
                "max_wall_seconds": 7200,
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(hours=2)
                ).isoformat(),
            }},
        }, "session:1")
        self.assertEqual(
            "lease:1",
            configured["result"]["structuredContent"]["execution_lease"]["lease_id"],
        )
        claimed = dispatch({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "claim_delegation", "arguments": {"worker_id": "worker:1"}},
        }, "session:1")
        payload = claimed["result"]["structuredContent"]
        self.assertEqual("delegation:1", payload["delegation"]["delegation_id"])
        completed = dispatch({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "complete_delegation", "arguments": {
                "delegation_id": "delegation:1", "worker_id": "worker:1",
                "result": {"answer": 42},
            }},
        }, "session:1")
        self.assertEqual(
            "succeeded",
            completed["result"]["structuredContent"]["delegation"]["status"],
        )

    def test_execution_lease_rejects_unconfigured_and_disallowed_work(self) -> None:
        queue = DelegationQueue(Path(self.temp.name) / "unconfigured.db")
        queue.enqueue("delegation:none", actor="session:none", request={
            "input": {"task": 1}, "config": {"provider": "codex"},
        })
        self.assertIsNone(queue.claim(actor="session:none", worker_id="worker:1"))
        self.assertIn("not configured", queue.get(
            "delegation:none", actor="session:none",
        )["error"])

        self.queue.enqueue("delegation:denied", actor="session:1", request={
            "input": {"task": 1}, "config": {"provider": "unknown"},
        })
        self.assertIsNone(self.queue.claim(actor="session:1", worker_id="worker:1"))
        self.assertIn("not allowed", self.queue.get(
            "delegation:denied", actor="session:1",
        )["error"])

    def test_execution_lease_enforces_wall_and_delegation_budgets(self) -> None:
        queue = DelegationQueue(Path(self.temp.name) / "budget.db")
        queue.configure_execution_lease(
            actor="session:budget", lease_id="lease:budget", workspace_id="workspace:1",
            allowed_providers=("codex",), max_delegations=1, max_wall_seconds=10,
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        queue.enqueue("delegation:wall", actor="session:budget", request={
            "input": {"task": 1},
            "config": {"provider": "codex", "max_wall_seconds": 11},
        })
        self.assertIsNone(queue.claim(actor="session:budget", worker_id="worker:1"))
        self.assertIn("wall-clock", queue.get(
            "delegation:wall", actor="session:budget",
        )["error"])
        queue.enqueue("delegation:first", actor="session:budget", request={
            "input": {"task": 1},
            "config": {"provider": "codex", "max_wall_seconds": 5},
        })
        claimed = queue.claim(actor="session:budget", worker_id="worker:1")
        queue.complete(
            claimed["delegation_id"], actor="session:budget", worker_id="worker:1",
            result={"answer": 1},
        )
        queue.enqueue("delegation:second", actor="session:budget", request={
            "input": {"task": 2},
            "config": {"provider": "codex", "max_wall_seconds": 5},
        })
        self.assertIsNone(queue.claim(actor="session:budget", worker_id="worker:1"))
        self.assertIn("budget is exhausted", queue.get(
            "delegation:second", actor="session:budget",
        )["error"])

    def test_same_execution_lease_preserves_usage_and_active_replacement_is_refused(self) -> None:
        self.queue.enqueue("delegation:used", actor="session:1", request={
            "input": {"task": 1}, "config": {"provider": "codex"},
        })
        self.queue.claim(actor="session:1", worker_id="worker:1")
        refreshed = self.queue.configure_execution_lease(
            actor="session:1", lease_id="lease:1", workspace_id="workspace:1",
            allowed_providers=("codex",), max_delegations=100,
            max_wall_seconds=7200,
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        )
        self.assertEqual(1, refreshed["used_delegations"])
        with self.assertRaisesRegex(ValueError, "different active"):
            self.queue.configure_execution_lease(
                actor="session:1", lease_id="lease:other", workspace_id="workspace:1",
                allowed_providers=(), max_delegations=1, max_wall_seconds=1,
                expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            )
        with self.assertRaisesRegex(ValueError, "Workspace cannot change"):
            self.queue.configure_execution_lease(
                actor="session:1", lease_id="lease:1", workspace_id="workspace:other",
                allowed_providers=("codex",), max_delegations=100,
                max_wall_seconds=7200,
                expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            )
        with self.assertRaisesRegex(ValueError, "allowlist cannot change"):
            self.queue.configure_execution_lease(
                actor="session:1", lease_id="lease:1", workspace_id="workspace:1",
                allowed_providers=("codex", "new-provider"), max_delegations=100,
                max_wall_seconds=7200,
                expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            )
        with self.assertRaisesRegex(ValueError, "budgets cannot change"):
            self.queue.configure_execution_lease(
                actor="session:1", lease_id="lease:1", workspace_id="workspace:1",
                allowed_providers=("codex",), max_delegations=101,
                max_wall_seconds=7200,
                expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            )
        with self.assertRaisesRegex(ValueError, "cannot exceed 24 hours"):
            self.queue.configure_execution_lease(
                actor="session:long", lease_id="lease:long", workspace_id="workspace:1",
                allowed_providers=(), max_delegations=1, max_wall_seconds=1,
                expires_at=(datetime.now(timezone.utc) + timedelta(hours=25)).isoformat(),
            )

    def test_expired_execution_lease_refuses_new_work(self) -> None:
        with self.queue._connect() as db:
            db.execute(
                "UPDATE harness_execution_leases SET expires_at=? WHERE actor=?",
                ("2000-01-01T00:00:00.000000Z", "session:1"),
            )
            db.commit()
        self.queue.enqueue("delegation:expired-execution", actor="session:1", request={
            "input": {"task": 1}, "config": {"provider": "codex"},
        })
        self.assertIsNone(self.queue.claim(actor="session:1", worker_id="worker:1"))
        self.assertIn("execution lease expired", self.queue.get(
            "delegation:expired-execution", actor="session:1",
        )["error"])

    def test_unknown_delegation_reconciliation_is_actor_scoped_and_idempotent(self) -> None:
        self.queue.enqueue("delegation:unknown", actor="session:1", request={
            "input": {"task": 1}, "config": {"provider": "codex"},
        })
        self.queue.claim(actor="session:1", worker_id="worker:1")
        with self.queue._connect() as db:
            db.execute(
                "UPDATE harness_delegations SET lease_expires_at=? WHERE delegation_id=?",
                ("2000-01-01T00:00:00.000000Z", "delegation:unknown"),
            )
            db.commit()
        self.queue.get("delegation:unknown", actor="session:1")
        preview = self.queue.prune(before="2999-01-01T00:00:00+00:00")
        self.assertEqual(0, preview["delegations"])
        self.assertEqual(
            "unknown", self.queue.get("delegation:unknown", actor="session:1")["status"],
        )
        dispatch = build_mcp_dispatcher(
            Path(self.temp.name) / "reconcile-runtime.db",
            authorizer=Authorizer(lambda _actor: (READ_SCOPE, WRITE_SCOPE)),
            delegation_queue=self.queue, tool_profile="harness",
        )
        response = dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "reconcile_delegation", "arguments": {
                "delegation_id": "delegation:unknown",
                "outcome": "confirmed_succeeded", "note": "verified commit abc",
                "idempotency_key": "reconcile:1",
            }},
        }, "session:1")
        first = response["result"]["structuredContent"]["reconciliation"]
        repeated = self.queue.reconcile(
            "delegation:unknown", actor="session:1",
            outcome="confirmed_succeeded", note="verified commit abc",
            idempotency_key="reconcile:1",
        )
        self.assertEqual(first, repeated)
        with self.assertRaisesRegex(ValueError, "already reconciled differently"):
            self.queue.reconcile(
                "delegation:unknown", actor="session:1",
                outcome="confirmed_failed", note="", idempotency_key="reconcile:2",
            )
        self.assertIsNone(self.queue.reconciliation(
            "delegation:unknown", actor="session:2",
        ))
        self.assertEqual(1, self.queue.stats(actor="session:1")["reconciled"])
        removed = self.queue.prune(before="2999-01-01T00:00:00+00:00")
        self.assertEqual(1, removed["delegations"])
        with self.assertRaises(LookupError):
            self.queue.get("delegation:unknown", actor="session:1")


class HarnessSubagentHandlerTests(unittest.TestCase):
    def test_manifest_is_fixed_unknown_on_lease_loss(self) -> None:
        self.assertEqual("harness.subagent", HARNESS_SUBAGENT_MANIFEST.name)
        self.assertIs(
            ExecutionSafety.UNKNOWN_ON_LEASE_LOSS,
            HARNESS_SUBAGENT_MANIFEST.execution_safety,
        )

    def test_runtime_binding_is_not_retry_safe(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            queue = DelegationQueue(Path(directory) / "delegations.db")
            queue.configure_execution_lease(
                actor="session:1", lease_id="lease:1", workspace_id="workspace:1",
                allowed_providers=("codex",), max_delegations=1,
                max_wall_seconds=10,
                expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            )
            registry = trusted_handlers((HandlerRegistration(
                HARNESS_SUBAGENT_MANIFEST, HarnessSubagentHandler(queue),
                "harness.subagent@1.0.0",
            ),), attempt_db_path=Path(directory) / "attempts.db")
            node = IRNode(
                "delegate", "action", (), (), IRHandlerRef(
                    HARNESS_SUBAGENT_MANIFEST.name,
                    HARNESS_SUBAGENT_MANIFEST.version,
                    HARNESS_SUBAGENT_MANIFEST.fingerprint,
                ), {"provider": "codex"}, (), None,
            )
            self.assertFalse(registry.resolve(node).retry_safe)

    def test_write_and_concurrency_policies_fail_closed_at_validation(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            handler = HarnessSubagentHandler(
                DelegationQueue(Path(directory) / "delegations.db"),
            )
            invalid_isolation = handler.validate(HARNESS_SUBAGENT_MANIFEST, {
                "provider": "codex", "effects": "write",
                "isolation_mode": "shared",
            })
            invalid_concurrency = handler.validate(HARNESS_SUBAGENT_MANIFEST, {
                "provider": "codex", "max_concurrency": 2,
            })
            self.assertIn(
                "write delegations require exclusive or worktree isolation",
                {issue.message for issue in invalid_isolation.issues},
            )
            self.assertIn(
                "max_concurrency=1",
                " ".join(issue.message for issue in invalid_concurrency.issues),
            )

    def test_handler_hands_off_one_deterministic_delegation(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            queue = DelegationQueue(Path(directory) / "delegations.db")
            queue.configure_execution_lease(
                actor="session:1", lease_id="lease:1", workspace_id="workspace:1",
                allowed_providers=("codex",), max_delegations=1,
                max_wall_seconds=10,
                expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            )
            handler = HarnessSubagentHandler(queue, poll_seconds=0.005)
            request = SimpleNamespace(
                idempotency_key="attempt:stable", actor="session:1",
                input={"task": {"prompt": "fix it"}},
                config={"provider": "codex", "max_wall_seconds": 5},
            )
            prepared = handler.prepare(request, SimpleNamespace(request=request))
            captured = []
            thread = Thread(
                target=lambda: captured.append(handler.execute(prepared, object())),
            )
            thread.start()
            claimed = None
            for _ in range(100):
                claimed = queue.claim(actor="session:1", worker_id="worker:1")
                if claimed is not None:
                    break
                time.sleep(0.005)
            self.assertIsNotNone(claimed)
            queue.complete(
                claimed["delegation_id"], actor="session:1", worker_id="worker:1",
                result={"answer": 42},
            )
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual({"result": {"answer": 42}}, dict(captured[0].output))
            self.assertEqual(prepared.execution_ref, captured[0].provider_request_id)


if __name__ == "__main__":
    unittest.main()
