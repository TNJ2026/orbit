"""§10.1 transactions, idempotency and crash recovery: stable idempotency
keys on node_runs, handler idempotency declarations, the non-idempotent
crash window (needs_confirmation, no automatic replay) and the operator
confirmation protocol (treat-as-succeeded / re-execute)."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import orbit.server as server
from orbit import recovery
from orbit.node_handlers import NODE_HANDLERS, workflow_node_schema
from orbit.store import InvalidInputError, Store
from test_workflow_engine import BINDINGS, EngineHarness

_spawn_patcher = None


def setUpModule():
    global _spawn_patcher
    _spawn_patcher = mock.patch.object(server, "_spawn_step_worker")
    _spawn_patcher.start()


def tearDownModule():
    _spawn_patcher.stop()


def _harness(tmp):
    # Runner commands so dispatch queues real run jobs (spawn is mocked).
    return EngineHarness(tmp, bindings=[{**m, "runner_command": "cat"} for m in BINDINGS])


def _crash_active_step(h, tid, claim=True):
    """Simulate a runner dying mid-execution of the currently active step:
    the run job was claimed (execution started) and the task_run died."""
    if claim:
        job = h.store.claim_next_run_job("runner-1")
        assert job is not None
    run = h.store.create_task_run(tid, worker="hub-agent", status="running")
    h.store._conn.execute(
        "UPDATE task_runs SET status = 'orphaned' WHERE id = ?", (run["id"],)
    )
    h.store._conn.commit()
    return run


class HandlerIdempotencyDeclarationTests(unittest.TestCase):
    def test_builtin_handlers_declare_idempotency(self):
        expected_none = {"agent", "command", "legacy.decompose", "git.merge"}
        for name, handler in NODE_HANDLERS.items():
            self.assertEqual(
                "none" if name in expected_none else "guaranteed",
                handler.idempotency,
                name,
            )

    def test_schema_exposes_idempotency(self):
        handlers = {h["id"]: h for h in workflow_node_schema()["handlers"]}
        self.assertEqual("none", handlers["agent"]["idempotency"])
        self.assertEqual("none", handlers["command"]["idempotency"])
        self.assertEqual("guaranteed", handlers["join"]["idempotency"])
        self.assertEqual("guaranteed", handlers["human"]["idempotency"])


class IdempotencyKeyTests(unittest.TestCase):
    def _store_with_run(self, tmp):
        store = Store(Path(tmp) / "test.db")
        store.register_agent("hub-agent", "hub")
        store.send_message("hub-agent", "hub-agent", "body", kind="task", title="t")
        task_id = store.list_tasks()[0]["id"]
        run = store.create_workflow_run(task_id, {}, entry_steps=["s"])
        return store, task_id, run

    def test_first_attempt_generates_key_and_retry_reuses_it(self):
        with TemporaryDirectory() as tmp:
            store, _task_id, run = self._store_with_run(tmp)
            first = store.record_node_run(run["id"], "s")
            key = f"{run['id']}:s:0:1"
            self.assertEqual(1, first["attempt"])
            self.assertEqual(key, first["idempotency_key"])

            # Execution failure -> retry: attempt increments, key is REUSED
            # (same business activation, §10.1).
            store.finish_node_run(first["id"], "failed", summary="runner died")
            retry = store.record_node_run(run["id"], "s", consume_tokens=False)
            self.assertEqual(2, retry["attempt"])
            self.assertEqual(key, retry["idempotency_key"])

            # Cancelled attempts (supersede / operator retry) also reuse.
            store.finish_node_run(retry["id"], "cancelled")
            again = store.record_node_run(run["id"], "s", consume_tokens=False)
            self.assertEqual(3, again["attempt"])
            self.assertEqual(key, again["idempotency_key"])

    def test_new_business_cycle_gets_a_fresh_key(self):
        with TemporaryDirectory() as tmp:
            store, _task_id, run = self._store_with_run(tmp)
            first = store.record_node_run(run["id"], "s")
            store.finish_node_run(first["id"], "succeeded", port="rework")
            # The step completed its business outcome and was re-entered
            # (rework loop): new activation cycle, new key.
            second = store.record_node_run(run["id"], "s", consume_tokens=False)
            self.assertEqual(2, second["attempt"])
            self.assertEqual(f"{run['id']}:s:0:2", second["idempotency_key"])

    def test_item_scope_is_part_of_the_key(self):
        with TemporaryDirectory() as tmp:
            store, task_id, run = self._store_with_run(tmp)
            group = store.create_workflow_item_group(task_id, "s", [{"title": "a"}])
            scope = group["scopes"][0]
            node = store.record_node_run(
                run["id"], "s", item_scope_id=scope["id"], consume_tokens=False
            )
            self.assertEqual(
                f"{run['id']}:s:{scope['id']}:1", node["idempotency_key"]
            )

    def test_engine_rework_loop_rotates_the_key(self):
        with TemporaryDirectory() as tmp:
            h = _harness(tmp)
            tid = h.create_task()
            h.start(tid)
            h.complete("hub-agent", tid, "intake", "done", "scope")
            h.complete("codex", tid, "implement", "done", "diff")
            h.complete("rev", tid, "review", "rework", "tests missing")
            run = h.store.get_workflow_run_by_task(tid)
            impl_runs = h.store.list_node_runs(
                workflow_run_id=run["id"], step="implement"
            )
            self.assertEqual(2, len(impl_runs))
            self.assertEqual(
                f"{run['id']}:implement:0:1", impl_runs[0]["idempotency_key"]
            )
            # Second activation (rework re-entry) is a new business cycle.
            self.assertEqual(2, impl_runs[1]["attempt"])
            self.assertEqual(
                f"{run['id']}:implement:0:2", impl_runs[1]["idempotency_key"]
            )


class NonIdempotentCrashWindowTests(unittest.TestCase):
    def test_dead_runner_on_claimed_none_step_needs_confirmation(self):
        with TemporaryDirectory() as tmp:
            h = _harness(tmp)
            tid = h.create_task()
            h.start(tid)  # intake active (handler=agent -> idempotency none)
            _crash_active_step(h, tid, claim=True)

            alerts = server.check_task_health(h.store, tmp)

            self.assertEqual(1, len(alerts))
            self.assertEqual("non-idempotent crash window", alerts[0]["problem"])
            self.assertEqual("needs_confirmation", alerts[0]["action"])
            # Node run flagged, still open for confirmation.
            run = h.store.get_workflow_run_by_task(tid)
            node = h.store.list_node_runs(workflow_run_id=run["id"], step="intake")[-1]
            self.assertEqual("needs_confirmation", node["status"])
            self.assertIsNone(node["completed_at"])
            # Task blocked through the existing mechanism.
            self.assertEqual("blocked", h.task(tid)["task_status"])
            trans = h.store.list_task_transitions(tid)
            self.assertEqual("blocked", trans[-1]["outcome"])
            self.assertIn("non-idempotent step crashed", trans[-1]["note"])
            # NOT automatically re-queued: the only run job is the crashed one.
            jobs = [j for j in h.store.list_run_jobs("all") if j["step"] == "intake"]
            self.assertEqual(1, len(jobs))
            self.assertNotEqual("pending", jobs[0]["status"])
            # Deduped: a second sweep changes nothing.
            self.assertEqual([], server.check_task_health(h.store, tmp))

    def test_unclaimed_job_keeps_existing_auto_recovery(self):
        # No claim = execution never started, so replay is the first execution
        # and the pre-existing auto-recovery path stays in charge.
        with TemporaryDirectory() as tmp:
            h = _harness(tmp)
            tid = h.create_task()
            h.start(tid)
            _crash_active_step(h, tid, claim=False)

            alerts = server.check_task_health(h.store, tmp)
            self.assertEqual([], alerts)
            self.assertNotEqual("blocked", h.task(tid)["task_status"])
            run = h.store.get_workflow_run_by_task(tid)
            nodes = h.store.list_node_runs(workflow_run_id=run["id"], step="intake")
            self.assertNotIn("needs_confirmation", {n["status"] for n in nodes})

    def test_guaranteed_handler_is_not_gated(self):
        with TemporaryDirectory() as tmp:
            h = _harness(tmp)
            tid = h.create_task()
            h.start(tid)
            h.store.claim_next_run_job("runner-1")
            crash = recovery._handle_non_idempotent_crash(
                h.store, tmp, h.task(tid),
                {"id": "gate", "type": "join", "handler": "join"}, "gate",
                callbacks=mock.Mock(),
            )
            self.assertIsNone(crash)


class ConfirmNodeRunTests(unittest.TestCase):
    def _crashed_task(self, h):
        tid = h.create_task()
        h.start(tid)
        _crash_active_step(h, tid, claim=True)
        alerts = server.check_task_health(h.store, h.root)
        assert alerts and alerts[0]["action"] == "needs_confirmation"
        return tid

    def test_confirm_succeeded_advances_to_next_step(self):
        with TemporaryDirectory() as tmp:
            h = _harness(tmp)
            tid = self._crashed_task(h)

            result = server.confirm_node_run(
                h.store, tmp, tid, "intake", "succeeded"
            )

            self.assertEqual("succeeded", result["disposition"])
            run = h.store.get_workflow_run_by_task(tid)
            node = h.store.get_node_run(result["node_run_id"])
            self.assertEqual("succeeded", node["status"])
            self.assertIsNotNone(node["completed_at"])
            # Advanced along the default port: implement is now dispatched.
            self.assertEqual(
                [{"step": "implement", "assignee": "codex"}],
                result["advance"]["dispatched"],
            )
            trans = h.store.list_task_transitions(tid)
            self.assertIn(
                ("intake", "implement", "done"),
                {(t["from_step"], t["to_step"], t["outcome"]) for t in trans},
            )
            self.assertNotEqual("blocked", h.task(tid)["task_status"])
            impl = h.store.list_node_runs(workflow_run_id=run["id"], step="implement")
            self.assertEqual(1, len(impl))

    def test_confirm_retry_reuses_key_with_new_attempt(self):
        with TemporaryDirectory() as tmp:
            h = _harness(tmp)
            tid = self._crashed_task(h)
            run = h.store.get_workflow_run_by_task(tid)
            flagged = h.store.list_node_runs(
                workflow_run_id=run["id"], step="intake"
            )[-1]

            result = server.confirm_node_run(h.store, tmp, tid, "intake", "retry")

            self.assertEqual("retry", result["disposition"])
            self.assertTrue(result["reran"])
            # The unresolved attempt is cancelled, never silently replayed.
            self.assertEqual(
                "cancelled", h.store.get_node_run(flagged["id"])["status"]
            )
            nodes = h.store.list_node_runs(workflow_run_id=run["id"], step="intake")
            fresh = nodes[-1]
            self.assertEqual(flagged["attempt"] + 1, fresh["attempt"])
            self.assertEqual(flagged["idempotency_key"], fresh["idempotency_key"])
            self.assertEqual("running", fresh["status"])
            # A new run job was queued for the step.
            jobs = [j for j in h.store.list_run_jobs("all") if j["step"] == "intake"]
            self.assertEqual("pending", jobs[0]["status"])

    def test_confirm_rejects_bad_input(self):
        with TemporaryDirectory() as tmp:
            h = _harness(tmp)
            tid = h.create_task()
            h.start(tid)
            with self.assertRaises(InvalidInputError):  # unknown task
                server.confirm_node_run(h.store, tmp, 9999, "intake", "succeeded")
            with self.assertRaises(InvalidInputError):  # bad disposition
                server.confirm_node_run(h.store, tmp, tid, "intake", "maybe")
            with self.assertRaises(InvalidInputError):  # nothing awaiting
                server.confirm_node_run(h.store, tmp, tid, "intake", "succeeded")
            with self.assertRaises(InvalidInputError):  # unknown step
                server.confirm_node_run(h.store, tmp, tid, "nope", "retry")


if __name__ == "__main__":
    unittest.main()
