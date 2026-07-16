"""Record layer for the general workflow runtime: workflow_runs / node_runs /
workflow_tokens are dual-written next to the existing tasks/task_transitions
tables (docs/general-workflow-design.md §10.1/§10.3/§10.4) without changing
any routing decision or any existing API field."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import orbit.server as server
from orbit.store import InvalidInputError, Store
from test_workflow_engine import BINDINGS, EngineHarness, LINEAR_EDGES, LINEAR_STEPS

_spawn_patcher = None


def setUpModule():
    global _spawn_patcher
    _spawn_patcher = mock.patch.object(server, "_spawn_step_worker")
    _spawn_patcher.start()


def tearDownModule():
    _spawn_patcher.stop()


class WorkflowRunRecordTests(unittest.TestCase):
    def _run_linear_flow(self, h, task_id):
        h.complete("hub-agent", task_id, "intake", "done", "requirements clear")
        h.complete("codex", task_id, "implement", "done", "diff ready")
        h.complete("rev", task_id, "review", "done", "lgtm")
        h.complete("hub-agent", task_id, "accept", "done", "shipped")

    def test_linear_flow_records_run_node_runs_and_token_chain(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            task_id = h.create_task()
            h.start(task_id)

            run = h.store.get_workflow_run_by_task(task_id)
            self.assertIsNotNone(run)
            self.assertEqual("running", run["status"])
            # Definition snapshot is captured at start (§4.1).
            snapshot_steps = {s["id"] for s in run["definition_snapshot"]["steps"]}
            self.assertEqual({"intake", "implement", "review", "accept"}, snapshot_steps)

            self._run_linear_flow(h, task_id)

            run = h.store.get_workflow_run_by_task(task_id)
            self.assertEqual("succeeded", run["status"])
            self.assertIsNotNone(run["completed_at"])

            # node_runs correspond 1:1 (count and order) with the engine's
            # dispatch transitions — the old tables stay the source of truth.
            dispatches = [
                t["to_step"] for t in h.store.list_task_transitions(task_id)
                if t["outcome"] == "dispatched"
            ]
            node_runs = h.store.list_node_runs(workflow_run_id=run["id"])
            self.assertEqual(dispatches, [n["step"] for n in node_runs])
            self.assertEqual(
                ["intake", "implement", "review", "accept"],
                [n["step"] for n in node_runs],
            )
            self.assertEqual({"succeeded"}, {n["status"] for n in node_runs})
            self.assertEqual([1, 1, 1, 1], [n["attempt"] for n in node_runs])
            self.assertEqual(
                ["hub-agent", "codex", "rev", "hub-agent"],
                [n["executor"] for n in node_runs],
            )
            self.assertTrue(all(n["completed_at"] for n in node_runs))

            # Complete token chain: entry token -> one token per traversed
            # edge, every one consumed by its downstream dispatch.
            tokens = h.store.list_workflow_tokens(run["id"])
            self.assertEqual(
                [("", "intake"), ("intake", "implement"),
                 ("implement", "review"), ("review", "accept")],
                [(t["from_step"], t["to_step"]) for t in tokens],
            )
            self.assertEqual({"consumed"}, {t["status"] for t in tokens})
            self.assertTrue(all(t["consumed_at"] for t in tokens))
            # Emitted tokens are attached to the node run that produced them.
            by_step = {n["step"]: n["id"] for n in node_runs}
            self.assertEqual(by_step["intake"], tokens[1]["node_run_id"])
            self.assertEqual(by_step["review"], tokens[3]["node_run_id"])

    def test_rework_creates_second_attempt_and_rework_token(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")
            h.complete("codex", task_id, "implement", "done", "diff ready")
            h.complete("rev", task_id, "review", "rework", "tests missing")

            run = h.store.get_workflow_run_by_task(task_id)
            implement_runs = h.store.list_node_runs(
                workflow_run_id=run["id"], step="implement"
            )
            self.assertEqual([1, 2], [n["attempt"] for n in implement_runs])
            self.assertEqual("succeeded", implement_runs[0]["status"])
            self.assertEqual("running", implement_runs[1]["status"])
            review_runs = h.store.list_node_runs(
                workflow_run_id=run["id"], step="review"
            )
            # Business result rides on the port; execution itself succeeded.
            self.assertEqual("succeeded", review_runs[0]["status"])
            self.assertEqual("rework", review_runs[0]["port"])
            rework_tokens = [
                t for t in h.store.list_workflow_tokens(run["id"])
                if t["from_step"] == "review" and t["to_step"] == "implement"
            ]
            self.assertEqual(1, len(rework_tokens))
            self.assertEqual("rework", rework_tokens[0]["port"])
            self.assertEqual("consumed", rework_tokens[0]["status"])

    def test_blocked_step_converges_node_and_run_to_blocked(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")
            h.complete("codex", task_id, "implement", "blocked", "cannot proceed")

            run = h.store.get_workflow_run_by_task(task_id)
            self.assertEqual("blocked", run["status"])
            self.assertIsNone(run["completed_at"])
            [node] = h.store.list_node_runs(
                workflow_run_id=run["id"], step="implement"
            )
            self.assertEqual("blocked", node["status"])
            # Recovery: re-running the step supersedes nothing (the blocked
            # attempt is settled) and records attempt 2.
            server.rerun_workflow_step(h.store, tmp, task_id, "codex", step="implement")
            attempts = h.store.list_node_runs(
                workflow_run_id=run["id"], step="implement"
            )
            self.assertEqual([1, 2], [n["attempt"] for n in attempts])
            self.assertEqual("running", attempts[1]["status"])
            self.assertEqual(
                "running", h.store.get_workflow_run_by_task(task_id)["status"]
            )

    def test_approval_marks_node_waiting_then_succeeded(self):
        steps = [dict(s) for s in LINEAR_STEPS]
        steps[2]["approval_required"] = True  # review
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, steps=steps)
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")
            h.complete("codex", task_id, "implement", "done")
            h.complete("rev", task_id, "review", "done", "lgtm")

            run = h.store.get_workflow_run_by_task(task_id)
            [review] = h.store.list_node_runs(workflow_run_id=run["id"], step="review")
            self.assertEqual("waiting", review["status"])
            self.assertIsNone(review["completed_at"])

            h.complete(server.HUB_NOTIFY_AGENT, task_id, "review", "done", "approved")
            [review] = h.store.list_node_runs(workflow_run_id=run["id"], step="review")
            self.assertEqual("succeeded", review["status"])
            self.assertIsNotNone(review["completed_at"])

    def test_node_settlement_and_token_emission_are_one_transaction(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            task_id = h.create_task()
            h.start(task_id)
            # Fail the token INSERT mid-mirror: the SAVEPOINT must roll the
            # whole mirror back (node run left unsettled, no token) while the
            # primary transition write commits untouched.
            with mock.patch.object(
                Store, "_insert_workflow_token_locked", side_effect=RuntimeError("boom")
            ):
                result = h.complete("hub-agent", task_id, "intake", "done")
            self.assertEqual([{"step": "implement", "assignee": "codex"}],
                             result["dispatched"])
            transitions = h.store.list_task_transitions(task_id)
            self.assertIn(
                ("intake", "implement", "done"),
                [(t["from_step"], t["to_step"], t["outcome"]) for t in transitions],
            )
            run = h.store.get_workflow_run_by_task(task_id)
            [intake] = h.store.list_node_runs(workflow_run_id=run["id"], step="intake")
            self.assertIsNone(intake["completed_at"])  # rolled back with the token
            self.assertEqual(
                [],
                [t for t in h.store.list_workflow_tokens(run["id"])
                 if t["from_step"] == "intake"],
            )

    def test_mirror_failure_never_breaks_the_engine(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            task_id = h.create_task()
            with mock.patch.object(
                Store, "_mirror_transition_locked", side_effect=RuntimeError("boom")
            ):
                h.start(task_id)
                self._run_linear_flow(h, task_id)
            task = h.task(task_id)
            self.assertEqual("closed", task["task_status"])
            run = h.store.get_workflow_run_by_task(task_id)
            self.assertEqual("succeeded", run["status"])  # status mirror still ran
            self.assertEqual([], h.store.list_node_runs(workflow_run_id=run["id"]))

    def test_run_creation_failure_never_breaks_start(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            task_id = h.create_task()
            with mock.patch.object(
                Store, "create_workflow_run", side_effect=RuntimeError("boom")
            ):
                started = h.start(task_id)
            self.assertTrue(started["started"])
            self.assertIsNone(h.store.get_workflow_run_by_task(task_id))
            self._run_linear_flow(h, task_id)
            self.assertEqual("closed", h.task(task_id)["task_status"])

    def test_goals_summary_appends_workflow_run_without_touching_v1_fields(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            goal_id = h.create_task(title="goal")
            h.store.update_task_metadata(goal_id, is_goal=True)

            before = server.goals_summary(h.store, tmp)[0]
            h.start(goal_id)
            after = server.goals_summary(h.store, tmp)[0]

            # v1 projection fields are unchanged; workflow_run is append-only.
            v1_fields = {
                "subtask_total", "subtask_closed", "subtask_blocked",
                "tokens_total", "budget_exceeded", "budget_overage",
                "steps", "subtasks",
            }
            self.assertTrue(v1_fields <= set(before) <= set(after))
            self.assertIsNone(before["workflow_run"])
            run = h.store.get_workflow_run_by_task(goal_id)
            self.assertEqual(
                {"id": run["id"], "status": "running"}, after["workflow_run"]
            )

            self._run_linear_flow(h, goal_id)
            final = server.goals_summary(h.store, tmp)[0]
            self.assertEqual("accepted", final["task_status"])
            self.assertEqual("succeeded", final["workflow_run"]["status"])

    def test_force_close_cancels_run_open_nodes_and_pending_tokens(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")

            server.force_close_goal(h.store, tmp, task_id)
            run = h.store.get_workflow_run_by_task(task_id)
            self.assertEqual("cancelled", run["status"])
            self.assertIsNotNone(run["completed_at"])
            open_nodes = [
                n for n in h.store.list_node_runs(workflow_run_id=run["id"])
                if n["completed_at"] is None
            ]
            self.assertEqual([], open_nodes)
            self.assertEqual(
                [], h.store.list_workflow_tokens(run["id"], status="pending")
            )

    def test_foreach_items_get_their_own_node_runs(self):
        steps = [
            {"id": "plan", "name": "Plan", "agents": ["codex"]},
            {
                "id": "process", "name": "Process", "type": "foreach",
                "agents": ["codex", "gemini"],
                "items": "$.input.items",
                "item_key": "$.id",
                "max_concurrency": 2,
            },
            {"id": "publish", "name": "Publish", "agents": ["hub-agent"]},
        ]
        edges = [
            {"from": "plan", "to": "process", "mapping": {"items": "$.output.items"}},
            {"from": "process", "to": "publish"},
        ]
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, steps=steps, edges=edges)
            tid = h.create_task()
            h.start(tid)
            h.complete(
                "codex", tid, "plan", "done",
                'WORKFLOW_RESULT: {"output":{"items":[{"id":"a"},{"id":"b"}]}}',
            )
            [group] = h.store.list_workflow_item_groups(tid, "process")
            scopes = {s["scope_key"]: s for s in group["scopes"]}
            run = h.store.get_workflow_run_by_task(tid)

            item_nodes = [
                n for n in h.store.list_node_runs(workflow_run_id=run["id"], step="process")
                if n["item_scope_id"] is not None
            ]
            self.assertEqual(
                {scopes["a"]["id"], scopes["b"]["id"]},
                {n["item_scope_id"] for n in item_nodes},
            )
            self.assertTrue(all(n["run_job_id"] for n in item_nodes))
            self.assertEqual({"running"}, {n["status"] for n in item_nodes})

            from orbit.workflow_engine import apply_foreach_item_outcome
            apply_foreach_item_outcome(
                h.store, tmp, scopes["a"]["id"], "codex", "done",
                'WORKFLOW_RESULT: {"output":{"value":"A"}}',
            )
            apply_foreach_item_outcome(
                h.store, tmp, scopes["b"]["id"], "gemini", "done",
                'WORKFLOW_RESULT: {"output":{"value":"B"}}',
            )
            nodes = h.store.list_node_runs(workflow_run_id=run["id"], step="process")
            settled_items = [n for n in nodes if n["item_scope_id"] is not None]
            self.assertEqual({"succeeded"}, {n["status"] for n in settled_items})
            # The foreach step itself also completed and routed to publish.
            [step_node] = [n for n in nodes if n["item_scope_id"] is None]
            self.assertEqual("succeeded", step_node["status"])
            self.assertEqual(
                1,
                len([
                    t for t in h.store.list_workflow_tokens(run["id"])
                    if (t["from_step"], t["to_step"]) == ("process", "publish")
                ]),
            )

    def test_store_finish_node_run_emits_tokens_atomically(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.db")
            store.register_agent("hub-agent", "hub")
            ids = store.send_message(
                "hub-agent", "hub-agent", "content", kind="task", title="t"
            )
            task = store.get_task_by_source_message(ids[0])
            run = store.create_workflow_run(
                task["id"], {"steps": []}, entry_steps=["a"]
            )
            # create_workflow_run is idempotent per task.
            again = store.create_workflow_run(task["id"], {"steps": [1]})
            self.assertEqual(run["id"], again["id"])
            [entry] = store.list_workflow_tokens(run["id"])
            self.assertEqual(("", "a", "pending"),
                             (entry["from_step"], entry["to_step"], entry["status"]))

            node = store.record_node_run(run["id"], "a", executor="codex")
            self.assertEqual(1, node["attempt"])
            [entry] = store.list_workflow_tokens(run["id"])
            self.assertEqual("consumed", entry["status"])  # consumed on record

            with self.assertRaises(InvalidInputError):
                store.finish_node_run(node["id"], "not-a-status")
            settled = store.finish_node_run(
                node["id"], "succeeded", port="success", summary="ok",
                tokens=[{"from_step": "a", "to_step": "b", "port": "success"}],
            )
            self.assertEqual("succeeded", settled["status"])
            tokens = store.list_workflow_tokens(run["id"], status="pending")
            self.assertEqual(
                [("a", "b", node["id"])],
                [(t["from_step"], t["to_step"], t["node_run_id"]) for t in tokens],
            )
            self.assertEqual(1, store.consume_workflow_tokens(run["id"], "b"))
            store.close()


class RunVariablesTests(unittest.TestCase):
    """Goal token budget mirrored into workflow run `variables` (design §11).
    tasks.token_budget stays the authoritative source the engine guard reads;
    variables are a migration-period record layer with zero routing role."""

    def _goal(self, h, budget=500):
        goal_id = h.create_task(title="budgeted goal")
        h.store.update_task_metadata(goal_id, is_goal=True, token_budget=budget)
        return goal_id

    def _variables(self, h, task_id):
        return h.store.get_workflow_run_by_task(task_id)["variables"]

    def test_goal_start_records_budget_in_run_variables(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            goal_id = self._goal(h, 500)
            h.start(goal_id)
            self.assertEqual({"token_budget": 500}, self._variables(h, goal_id))

    def test_non_goal_run_variables_stay_empty(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            task_id = h.create_task()
            h.start(task_id)
            self.assertEqual({}, self._variables(h, task_id))

    def test_budget_update_syncs_run_variables(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            goal_id = self._goal(h, 500)
            h.start(goal_id)
            h.store.update_task_metadata(goal_id, token_budget=800)
            self.assertEqual(800, self._variables(h, goal_id)["token_budget"])
            # tasks.token_budget stays the authoritative source.
            self.assertEqual(800, h.store.get_task(goal_id)["token_budget"])

    def test_update_variables_merges_and_none_deletes(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            task_id = h.create_task()
            run = h.store.create_workflow_run(task_id, {})
            h.store.update_workflow_run_variables(run["id"], a=1, b="x")
            h.store.update_workflow_run_variables(run["id"], b=None, c=2)
            self.assertEqual({"a": 1, "c": 2}, self._variables(h, task_id))
            self.assertIsNone(h.store.update_workflow_run_variables(999_999, a=1))

    def test_budget_freeze_mirrors_usage_snapshot(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            goal_id = self._goal(h, 500)
            h.start(goal_id)
            run = h.store.create_task_run(
                goal_id, worker="hub-agent", status="running"
            )
            h.store.finish_task_run(run["id"], "succeeded", 0, 600)
            # Advancing dispatches the next step; the budget guard freezes the
            # goal and mirrors the usage snapshot into the run's variables.
            h.complete("hub-agent", goal_id, "intake", "done")
            variables = self._variables(h, goal_id)
            self.assertEqual(500, variables["token_budget"])
            self.assertEqual(600, variables["tokens_total"])
            self.assertTrue(variables["budget_frozen_at"])

    def test_budget_resume_syncs_budget_and_clears_frozen_marker(self):
        with TemporaryDirectory() as tmp:
            bindings = [{**m, "runner_command": "cat"} for m in BINDINGS]
            h = EngineHarness(tmp, bindings=bindings)
            goal_id = h.create_task(title="budgeted goal")
            task_id = h.create_task(title="frozen subtask")
            h.store.update_task_metadata(goal_id, is_goal=True, token_budget=500)
            h.store._conn.execute(
                "UPDATE tasks SET parent_task_id = ?, status = 'blocked', "
                "workflow_step = 'implement' WHERE id = ?",
                (goal_id, task_id),
            )
            h.store.set_task_workflow_state(goal_id, task_status="stalled")
            h.store.record_task_transition(
                task_id, "", "implement", "workflow", "blocked",
                "goal token budget exceeded; dispatch frozen",
            )
            run = h.store.create_task_run(task_id, worker="codex", status="running")
            h.store.finish_task_run(run["id"], "succeeded", 0, 600)
            wf_run = h.store.create_workflow_run(goal_id, {})
            h.store.update_workflow_run_variables(
                wf_run["id"], token_budget=500, tokens_total=600,
                budget_frozen_at="2026-01-01T00:00:00+00:00",
            )

            server.resume_goal_after_budget_increase(h.store, tmp, goal_id, 1_000)

            variables = self._variables(h, goal_id)
            self.assertEqual(1_000, variables["token_budget"])
            self.assertEqual(600, variables["tokens_total"])
            self.assertNotIn("budget_frozen_at", variables)


if __name__ == "__main__":
    unittest.main()
