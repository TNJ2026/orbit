from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import pickle
import tempfile
from threading import Thread
import time
import unittest

from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.definitions import IRHandlerRef, IRNode
from orbit.workflow.domain.handlers import CancelDisposition, RecoveryDisposition
from orbit.workflow.langgraph_runtime.harness_subagent import (
    APP_DELEGATE_MANIFEST, AppDelegationHandler, DelegationQueue,
    HARNESS_SUBAGENT_MANIFEST, HarnessSubagentHandler,
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

    def test_current_app_queue_needs_no_unattended_execution_lease(self) -> None:
        queue = DelegationQueue(
            Path(self.temp.name) / "app-delegations.db",
            require_execution_lease=False,
        )
        queue.enqueue("app:1", actor="session:app", request={
            "input": {"task": "review"},
            "config": {"provider": "current-app"},
        })
        claimed = queue.claim(actor="session:app", worker_id="codex-task:1")
        self.assertEqual("leased", claimed["status"])
        completed = queue.complete(
            "app:1", actor="session:app", worker_id="codex-task:1",
            result={"answer": "done"},
        )
        self.assertEqual("succeeded", completed["status"])

    def test_unclaimed_app_work_cannot_execute_after_its_deadline(self) -> None:
        queue = DelegationQueue(
            Path(self.temp.name) / "app-timeout.db",
            require_execution_lease=False,
        )
        queue.enqueue("app:late", actor="session:app", request={
            "input": {"task": "late"}, "config": {"provider": "current-app"},
        })
        expired = queue.timeout("app:late", actor="session:app")
        self.assertEqual("failed", expired["status"])
        self.assertIsNone(queue.claim(actor="session:app", worker_id="late-worker"))

    def test_current_app_can_rediscover_only_its_open_delegations(self) -> None:
        queue = DelegationQueue(
            Path(self.temp.name) / "app-resume.db",
            require_execution_lease=False,
        )
        queue.enqueue("app:done", actor="session:app", request={"task": 3})
        queue.claim(actor="session:app", worker_id="worker:done")
        queue.complete(
            "app:done", actor="session:app", worker_id="worker:done",
            result={"answer": "done"},
        )
        queue.enqueue("app:queued", actor="session:app", request={"task": 1})
        queue.enqueue("app:other", actor="session:other", request={"task": 2})
        open_items = queue.list(actor="session:app")
        self.assertEqual(["app:queued"], [item["delegation_id"] for item in open_items])
        all_items = queue.list(
            actor="session:app", statuses=("queued", "succeeded"),
        )
        self.assertEqual(
            ["app:done", "app:queued"],
            [item["delegation_id"] for item in all_items],
        )
        self.assertTrue(all("created_at" in item for item in all_items))

    def test_app_delegate_manifest_and_handler_are_host_neutral(self) -> None:
        queue = DelegationQueue(
            Path(self.temp.name) / "app-handler.db",
            require_execution_lease=False,
        )
        handler = AppDelegationHandler(queue, poll_seconds=0.001)
        valid = handler.validate(APP_DELEGATE_MANIFEST, {
            "target": "run_initiator", "effects": "read",
        })
        self.assertEqual((), valid.issues)
        background = handler.validate(APP_DELEGATE_MANIFEST, {
            "target": "background_pool", "pool": "coding", "effects": "read",
        })
        self.assertEqual((), background.issues)
        invalid = handler.validate(APP_DELEGATE_MANIFEST, {
            "target": "codex-app", "effects": "write", "isolation_mode": "shared",
        })
        self.assertEqual(2, len(invalid.issues))
        self.assertEqual("app.delegate", APP_DELEGATE_MANIFEST.name)

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

    def test_background_pool_is_claimed_only_by_machine_worker(self) -> None:
        queue = DelegationQueue(
            Path(self.temp.name) / "background.db", require_execution_lease=False,
        )
        queue.enqueue("app:interactive", actor="session:1", request={
            "input": {"task": "interactive"},
            "config": {"target": "run_initiator"},
        })
        queue.enqueue("app:background", actor="session:2", request={
            "input": {"task": "background"},
            "config": {"target": "background_pool", "pool": "coding"},
        })

        interactive = queue.claim(actor="session:1", worker_id="app-worker")
        self.assertEqual("app:interactive", interactive["delegation_id"])
        self.assertIsNone(queue.claim(actor="session:2", worker_id="app-worker"))
        self.assertIsNone(queue.claim_background(
            worker_id="machine-worker", pools=("default",),
        ))
        background = queue.claim_background(
            worker_id="machine-worker", pools=("coding",),
        )
        self.assertEqual("app:background", background["delegation_id"])
        self.assertEqual("session:2", background["actor"])
        renewed = queue.renew_background(
            "app:background", worker_id="machine-worker",
        )
        self.assertEqual("leased", renewed["status"])
        completed = queue.complete_background(
            "app:background", worker_id="machine-worker", result={"ok": True},
        )
        self.assertEqual("succeeded", completed["status"])

    def test_successful_delegation_result_must_match_the_object_port(self) -> None:
        self.queue.enqueue("delegation:object", actor="session:1", request={
            "input": {"task": 1}, "config": {"provider": "codex"},
        })
        self.queue.claim(actor="session:1", worker_id="worker:1")
        with self.assertRaisesRegex(ValueError, "result must be an object"):
            self.queue.complete(
                "delegation:object", actor="session:1", worker_id="worker:1",
                result="not an object",
            )
        self.assertEqual(
            "leased",
            self.queue.get("delegation:object", actor="session:1")["status"],
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
        self.assertIn("list_delegations", names)
        self.assertIn("configure_execution_lease", names)
        resumable = dispatch({
            "jsonrpc": "2.0", "id": 11, "method": "tools/call",
            "params": {"name": "list_delegations", "arguments": {}},
        }, "session:1")
        self.assertEqual(
            "delegation:1",
            resumable["result"]["structuredContent"]["delegations"][0]["delegation_id"],
        )
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

    def test_confirmed_app_result_becomes_recoverable_without_reexecution(self) -> None:
        queue = DelegationQueue(
            Path(self.temp.name) / "app-reconcile.db",
            require_execution_lease=False,
        )
        queue.enqueue("app_delegation:unknown", actor="session:app", request={
            "input": {"task": "write"}, "config": {"provider": "current-app"},
        })
        queue.claim(actor="session:app", worker_id="worker:lost")
        with queue._connect() as db:
            db.execute(
                "UPDATE harness_delegations SET lease_expires_at=?"
                " WHERE delegation_id=?",
                ("2000-01-01T00:00:00.000000Z", "app_delegation:unknown"),
            )
            db.commit()
        self.assertEqual(
            "unknown",
            queue.get("app_delegation:unknown", actor="session:app")["status"],
        )
        queue.reconcile(
            "app_delegation:unknown", actor="session:app",
            outcome="confirmed_succeeded", note="verified output",
            idempotency_key="reconcile:app", result={"answer": "done"},
        )
        item = queue.get("app_delegation:unknown", actor="session:app")
        self.assertEqual("succeeded", item["status"])
        self.assertEqual({"answer": "done"}, item["result"])
        recovered = AppDelegationHandler(queue).recover(
            "app_delegation:unknown", object(),
        )
        self.assertEqual(RecoveryDisposition.FOUND, recovered.disposition)


class HarnessSubagentHandlerTests(unittest.TestCase):
    def test_app_handler_is_safe_to_send_to_a_spawned_worker(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            handler = AppDelegationHandler(DelegationQueue(
                Path(directory) / "app-pickle.db", require_execution_lease=False,
            ))
            restored = pickle.loads(pickle.dumps(handler))
            self.assertFalse(restored.queue.require_execution_lease)

    def test_app_handler_hands_work_to_the_current_actor(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            queue = DelegationQueue(
                Path(directory) / "app.db", require_execution_lease=False,
            )
            handler = AppDelegationHandler(queue, poll_seconds=0.005)
            request = SimpleNamespace(
                attempt_id="langgraph_attempt:run:node:1",
                idempotency_key="app-attempt:stable", actor="session:workbuddy",
                input={"task": {"prompt": "review it"}},
                config={"target": "run_initiator", "max_wall_seconds": 5},
                deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            prepared = handler.prepare(request, SimpleNamespace(request=request))
            captured = []
            thread = Thread(target=lambda: captured.append(
                handler.execute(prepared, object()),
            ))
            thread.start()
            claimed = None
            for _ in range(100):
                claimed = queue.claim(
                    actor="session:workbuddy", worker_id="workbuddy-task:1",
                )
                if claimed is not None:
                    break
                time.sleep(0.005)
            self.assertIsNotNone(claimed)
            self.assertEqual("current-app", claimed["request"]["config"]["provider"])
            self.assertEqual(
                {"name": "orbit-app-delegation", "version": "1"},
                claimed["request"]["protocol"],
            )
            self.assertEqual(
                "langgraph_attempt:run:node:1",
                claimed["request"]["execution"]["attempt_id"],
            )
            self.assertIsNone(queue.claim(
                actor="session:other", worker_id="other-task:1",
            ))
            queue.complete(
                claimed["delegation_id"], actor="session:workbuddy",
                worker_id="workbuddy-task:1", result={"answer": "done"},
            )
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual({"result": {"answer": "done"}}, dict(captured[0].output))

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
