"""Workflow constraint engine: dispatch, join, rework, blocking, skipping."""

import json
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import orbit.server as server
from orbit.store import InvalidInputError, Store

# Dispatches with runner commands spawn runner threads; in tests that would
# launch real CLIs (codex, hermes, ...) and race the manual complete() calls.
# Mock the spawn for the whole module; run_step_worker tests call it directly.
_spawn_patcher = None


def setUpModule():
    global _spawn_patcher
    _spawn_patcher = mock.patch.object(server, "_spawn_step_worker")
    _spawn_patcher.start()


def tearDownModule():
    _spawn_patcher.stop()


LINEAR_STEPS = [
    {"id": "intake", "name": "Intake", "role_id": "hub", "task_status": "created", "required": True},
    {"id": "implement", "name": "Implement", "role_id": "implementer", "task_status": "in_progress", "required": True},
    {"id": "review", "name": "Review", "role_id": "reviewer", "task_status": "in_progress", "required": True},
    {"id": "accept", "name": "Accept", "role_id": "hub", "task_status": "in_progress", "required": True},
]
LINEAR_EDGES = [
    {"from": "intake", "to": "implement"},
    {"from": "implement", "to": "review"},
    {"from": "review", "to": "accept"},
    {"from": "review", "to": "implement"},  # rework loop-back
]
TEAM = [
    {"agent_name": "hub-agent", "role_id": "hub"},
    {"agent_name": "codex", "role_id": "implementer"},
    {"agent_name": "rev", "role_id": "reviewer"},
]


class EngineHarness:
    def __init__(self, tmp, steps=None, edges=None, team=None):
        self.root = tmp
        server.write_workflow_config(steps or LINEAR_STEPS, tmp, edges or LINEAR_EDGES)
        server.write_team_config(team if team is not None else TEAM, tmp)
        self.store = Store(Path(tmp) / "test.db")
        for member in (team if team is not None else TEAM):
            self.store.register_agent(member["agent_name"], member["role_id"])

    def create_task(self, title="Ship feature"):
        ids = self.store.send_message(
            "hub-agent", "hub-agent", "build the thing", kind="task", title=title
        )
        task = self.store.list_tasks()[0]
        assert task["source_message_id"] == ids[0]
        return task["id"]

    def start(self, task_id, agent="hub-agent"):
        return server.start_workflow_task(self.store, self.root, agent, task_id)

    def complete(self, agent, task_id, step, outcome="done", result=""):
        return server.advance_workflow_task(
            self.store, self.root, agent, task_id, step, outcome, result
        )

    def state(self, task_id):
        return server.workflow_task_state(self.store, self.root, task_id)

    def task(self, task_id):
        return self.store.get_task(task_id)

    def inbox_senders(self, agent):
        return [
            (m["sender"], m["content"])
            for m in self.store.fetch_unread(agent, lease_seconds=1)
        ]


class WorkflowEngineTests(unittest.TestCase):
    def test_linear_flow_with_rework_and_close(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            task_id = h.create_task()

            started = h.start(task_id)
            self.assertEqual([{"step": "intake", "assignee": "hub-agent"}], started["dispatched"])
            task = h.task(task_id)
            self.assertEqual("hub-agent", task["assignee"])
            self.assertEqual("assigned", task["task_status"])
            self.assertEqual("intake", task["workflow_step"])

            done = h.complete("hub-agent", task_id, "intake", "done", "requirements clear")
            self.assertEqual([{"step": "implement", "assignee": "codex"}], done["dispatched"])
            task = h.task(task_id)
            self.assertEqual("codex", task["assignee"])
            self.assertEqual("assigned", task["task_status"])

            # dispatch message carries the upstream result
            messages = h.inbox_senders("codex")
            self.assertTrue(any("requirements clear" in content for _, content in messages))

            h.complete("codex", task_id, "implement", "done", "diff ready")
            self.assertEqual("rev", h.task(task_id)["assignee"])

            # review rejects -> loop back to implement
            rework = h.complete("rev", task_id, "review", "rework", "tests missing")
            self.assertEqual([{"step": "implement", "assignee": "codex"}], rework["dispatched"])
            self.assertEqual("assigned", h.task(task_id)["task_status"])

            h.complete("codex", task_id, "implement", "done", "tests added")
            h.complete("rev", task_id, "review", "done", "lgtm")
            self.assertEqual("assigned", h.task(task_id)["task_status"])

            closed = h.complete("hub-agent", task_id, "accept", "done", "shipped")
            self.assertTrue(closed["closed"])
            task = h.task(task_id)
            self.assertEqual("closed", task["task_status"])
            self.assertEqual("", task["workflow_step"])
            self.assertEqual([], h.state(task_id)["active_steps"])

    def test_merge_waits_for_all_required_branches(self):
        steps = [
            {"id": "a", "name": "A", "role_id": "hub", "task_status": "created", "required": True},
            {"id": "b", "name": "B", "role_id": "implementer", "task_status": "in_progress", "required": True},
            {"id": "c", "name": "C", "role_id": "reviewer", "task_status": "in_progress", "required": True},
            {"id": "d", "name": "D", "role_id": "hub", "task_status": "in_progress", "required": True},
        ]
        edges = [
            {"from": "a", "to": "b"},
            {"from": "a", "to": "c"},
            {"from": "b", "to": "d"},
            {"from": "c", "to": "d"},
        ]
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, steps=steps, edges=edges)
            task_id = h.create_task()
            h.start(task_id)

            fanout = h.complete("hub-agent", task_id, "a", "done")
            self.assertEqual(
                {"b", "c"}, {d["step"] for d in fanout["dispatched"]}
            )
            self.assertEqual({"b", "c"}, set(h.state(task_id)["active_steps"]))
            self.assertEqual("assigned", h.state(task_id)["status"])

            first = h.complete("codex", task_id, "b", "done")
            self.assertEqual([], first["dispatched"])
            self.assertTrue(any("waiting" in n for n in first["notices"]))

            second = h.complete("rev", task_id, "c", "done")
            self.assertEqual([{"step": "d", "assignee": "hub-agent"}], second["dispatched"])

    def test_visible_status_is_independent_of_workflow_step_config(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")
            self.assertEqual("assigned", h.state(task_id)["status"])

            steps = [dict(step) for step in LINEAR_STEPS]
            steps[1]["task_status"] = "in_progress"
            server.write_workflow_config(steps, tmp, LINEAR_EDGES)

            self.assertEqual("assigned", h.state(task_id)["status"])
            projected = server._project_workflow_task_status(
                h.store, tmp, h.task(task_id)
            )
            self.assertEqual("assigned", projected["task_status"])
            self.assertEqual("assigned", h.task(task_id)["task_status"])

    def test_blocked_status_overrides_active_step_projection(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")

            h.store.set_task_workflow_state(task_id, task_status="blocked")
            self.assertEqual("blocked", h.state(task_id)["status"])
            projected = server._project_workflow_task_status(
                h.store, tmp, h.task(task_id)
            )
            self.assertEqual("blocked", projected["task_status"])

    def test_manual_status_rejected_while_steps_active(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            task_id = h.create_task()
            h.start(task_id)  # intake active
            task = h.task(task_id)
            # a non-override manual status would be hidden by projection -> rejected
            reason = server._manual_status_rejection(h.store, task, "in_progress")
            self.assertIsNotNone(reason)
            self.assertIn("intake", reason)
            # overrides always stick
            for ok in ("blocked", "stalled", "closed"):
                self.assertIsNone(server._manual_status_rejection(h.store, task, ok))
            # no active steps (never started) -> anything goes
            idle_id = h.create_task(title="idle")
            self.assertIsNone(
                server._manual_status_rejection(h.store, h.task(idle_id), "in_progress")
            )

    def test_goals_summary_projects_subtask_status(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=[
                {"agent_name": "hub-agent", "role_id": "hub", "runner_command": "cat"},
                {"agent_name": "codex", "role_id": "implementer", "runner_command": "cat"},
                {"agent_name": "integrator", "role_id": "integrator", "runner_command": "cat"},
                {"agent_name": "rev", "role_id": "reviewer", "runner_command": "cat"},
            ])
            goal_id = h.create_task(title="goal")
            h.store.update_task_metadata(goal_id, is_goal=True)
            h.start(goal_id)
            step = next(
                s for s in server.read_workflow_config(tmp)["steps"]
                if s["id"] == "intake"
            )
            member = {
                **next(m for m in server.read_team_config(tmp)["members"]
                       if m["agent_name"] == "hub-agent"),
                "runner_command":
                    "printf '%s' '{\"tasks\":[{\"title\":\"A\",\"content\":\"a\"}]}'",
            }
            server.run_step_worker(h.store, tmp, goal_id, step, member)
            sub = next(
                t for t in h.store.list_tasks(status="all")
                if t.get("parent_task_id") == goal_id
                and t.get("source_message_id") is not None
            )
            # Advancing to another step changes workflow_step, while lifecycle
            # status remains assigned until a runner starts.
            h.complete("hub-agent", sub["id"], "intake", "done")
            steps = [dict(s) for s in LINEAR_STEPS]
            steps[1]["task_status"] = "in_progress"
            server.write_workflow_config(steps, tmp, LINEAR_EDGES)
            stored = h.task(sub["id"])["task_status"]
            self.assertEqual("assigned", stored)
            [projected] = server.goals_summary(h.store, tmp)[0]["subtasks"]
            self.assertEqual("assigned", projected["task_status"])
            # without a project_root the summary keeps the stored status
            [raw] = server.goals_summary(h.store)[0]["subtasks"]
            self.assertEqual(stored, raw["task_status"])

    def test_optional_late_branch_does_not_redispatch_join_target(self):
        steps = [
            {"id": "a", "name": "A", "role_id": "hub", "task_status": "created", "required": True},
            {"id": "b", "name": "B", "role_id": "implementer", "task_status": "in_progress", "required": True},
            {"id": "c", "name": "C", "role_id": "tester", "task_status": "in_progress", "required": False},
            {"id": "d", "name": "D", "role_id": "reviewer", "task_status": "in_progress", "required": True},
        ]
        edges = [
            {"from": "a", "to": "b"},
            {"from": "a", "to": "c"},
            {"from": "b", "to": "d"},
            {"from": "c", "to": "d"},
        ]
        team = TEAM + [{"agent_name": "test-agent", "role_id": "tester"}]
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, steps=steps, edges=edges, team=team)
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "a", "done")

            first = h.complete("codex", task_id, "b", "done")
            self.assertEqual([{"step": "d", "assignee": "rev"}], first["dispatched"])
            late = h.complete("test-agent", task_id, "c", "done")
            self.assertEqual([], late["dispatched"])

    def test_role_mismatch_is_rejected(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")
            with self.assertRaisesRegex(InvalidInputError, "not assigned"):
                h.complete("rev", task_id, "implement", "done")
            # hub may complete any step
            h.complete("hub-agent", task_id, "implement", "done")

    def test_unassigned_same_role_agent_cannot_complete_active_step(self):
        team = TEAM + [{"agent_name": "other-codex", "role_id": "implementer"}]
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=team)
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")

            with self.assertRaisesRegex(InvalidInputError, "not assigned"):
                h.complete("other-codex", task_id, "implement", "done")

    def test_inactive_step_completion_is_rejected(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            task_id = h.create_task()

            with self.assertRaisesRegex(InvalidInputError, "not active"):
                h.complete("codex", task_id, "implement", "done")

            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")
            h.complete("codex", task_id, "implement", "done", "diff ready")
            with self.assertRaisesRegex(InvalidInputError, "not active"):
                h.complete("codex", task_id, "implement", "done", "duplicate")

    def test_blocked_pauses_task_and_notifies_hub(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")

            blocked = h.complete(
                "codex", task_id, "implement", "blocked", "need choice: A or B"
            )
            self.assertEqual("blocked", h.task(task_id)["task_status"])
            self.assertIn("notified hub-agent", blocked["notices"][0])
            messages = h.inbox_senders("hub-agent")
            self.assertTrue(
                any("need choice: A or B" in content for _, content in messages)
            )

    def test_optional_step_without_member_is_skipped(self):
        steps = LINEAR_STEPS[:2] + [
            {"id": "test", "name": "Test", "role_id": "tester", "task_status": "in_progress", "required": False},
        ] + LINEAR_STEPS[2:]
        edges = [
            {"from": "intake", "to": "implement"},
            {"from": "implement", "to": "test"},
            {"from": "test", "to": "review"},
            {"from": "review", "to": "accept"},
        ]
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, steps=steps, edges=edges)  # team has no tester
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")

            result = h.complete("codex", task_id, "implement", "done")
            # tester step skipped, review dispatched directly
            self.assertEqual([{"step": "review", "assignee": "rev"}], result["dispatched"])
            self.assertTrue(any("skipped" in n for n in result["notices"]))

    def test_required_step_without_member_blocks(self):
        team = [m for m in TEAM if m["role_id"] != "reviewer"]
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=team)
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")

            result = h.complete("codex", task_id, "implement", "done")
            self.assertEqual([], result["dispatched"])
            self.assertEqual("blocked", h.task(task_id)["task_status"])
            messages = h.inbox_senders("hub-agent")
            self.assertTrue(any("no" in c and "reviewer" in c for _, c in messages))

    def test_start_twice_is_rejected(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            task_id = h.create_task()
            h.start(task_id)
            with self.assertRaisesRegex(InvalidInputError, "already in the workflow"):
                h.start(task_id)

    def test_required_cycle_disconnected_from_main_entry_is_rejected(self):
        # review <-> optional_check form a cycle with no forward inbound edge.
        # Back-edge classification makes review a second entry point, so the
        # graph is executable: both components dispatch at start and the task
        # can run to completion. (Stripping back edges always yields a DAG, so
        # a non-empty workflow always has an entry and a terminal — the
        # executability pre-check only guards degenerate configs.)
        steps = [
            {"id": "intake", "name": "Intake", "role_id": "hub", "task_status": "created", "required": True},
            {"id": "implement", "name": "Implement", "role_id": "implementer", "task_status": "in_progress", "required": True},
            {"id": "review", "name": "Review", "role_id": "reviewer", "task_status": "in_progress", "required": True},
            {"id": "optional_check", "name": "Optional Check", "role_id": "tester", "task_status": "in_progress", "required": False},
        ]
        edges = [
            {"from": "intake", "to": "implement"},
            {"from": "review", "to": "optional_check"},
            {"from": "optional_check", "to": "review"},
        ]
        team = TEAM + [{"agent_name": "test-agent", "role_id": "tester"}]
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, steps=steps, edges=edges, team=team)
            task_id = h.create_task()
            with self.assertRaisesRegex(InvalidInputError, "required steps unreachable"):
                h.start(task_id)

    def test_start_allows_warning_for_dangling_optional_step(self):
        steps = [
            {"id": "intake", "name": "Intake", "role_id": "hub", "task_status": "created", "required": True},
            {"id": "implement", "name": "Implement", "role_id": "implementer", "task_status": "in_progress", "required": True},
            {"id": "review", "name": "Review", "role_id": "reviewer", "task_status": "in_progress", "required": True},
            {"id": "optional_a", "name": "Optional A", "role_id": "tester", "task_status": "in_progress", "required": False},
            {"id": "optional_b", "name": "Optional B", "role_id": "tester", "task_status": "in_progress", "required": False},
        ]
        edges = [
            {"from": "intake", "to": "implement"},
            {"from": "implement", "to": "review"},
            {"from": "optional_a", "to": "optional_b"},
            {"from": "optional_b", "to": "optional_a"},
        ]
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, steps=steps, edges=edges)
            cfg = server.read_workflow_config(tmp)
            self.assertTrue(any("unreachable" in w for w in cfg["warnings"]))
            task_id = h.create_task()

            started = h.start(task_id)
            self.assertEqual(
                [{"step": "intake", "assignee": "hub-agent"}],
                started["dispatched"],
            )

    def test_ui_actor_resolves_to_enabled_hub_member(self):
        with TemporaryDirectory() as tmp:
            EngineHarness(tmp)
            self.assertEqual("hub-agent", server._workflow_api_actor("", tmp))
            self.assertEqual("hub-agent", server._workflow_api_actor("ui", tmp))
            self.assertEqual("codex", server._workflow_api_actor("codex", tmp))

    def test_ui_actor_without_hub_member_is_rejected(self):
        team = [m for m in TEAM if m["role_id"] != "hub"] + [
            {"agent_name": "hub-agent", "role_id": "hub", "enabled": False},
        ]
        with TemporaryDirectory() as tmp:
            EngineHarness(tmp, team=team)
            with self.assertRaisesRegex(InvalidInputError, "no enabled hub member"):
                server._workflow_api_actor("", tmp)

    def test_workflow_config_locked_while_task_active(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            task_id = h.create_task()
            h.start(task_id)

            reason = server.workflow_locked_reason(h.store)
            self.assertIsNotNone(reason)
            self.assertIn(f"#{task_id}", reason)

            h.complete("hub-agent", task_id, "intake", "blocked", "waiting")
            self.assertIsNone(server.workflow_locked_reason(h.store))

    def test_only_one_goal_can_run_at_a_time(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            first = h.create_task(title="first goal")
            second = h.create_task(title="second goal")
            h.store.update_task_metadata(first, is_goal=True)
            h.store.update_task_metadata(second, is_goal=True)

            h.start(first)
            with self.assertRaisesRegex(InvalidInputError, "already active"):
                h.start(second)

            server.force_close_goal(h.store, tmp, first)
            started = h.start(second)
            self.assertEqual(
                [{"step": "intake", "assignee": "hub-agent"}],
                started["dispatched"],
            )

    def test_rework_loop_caps_and_blocks(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            tid = h.create_task()
            h.start(tid)
            h.complete("hub-agent", tid, "intake", "done")
            # MAX_REWORK_ROUNDS rework rounds are allowed
            for _ in range(server.MAX_REWORK_ROUNDS):
                h.complete("codex", tid, "implement", "done")
                rw = h.complete("rev", tid, "review", "rework", "again")
                self.assertEqual([{"step": "implement", "assignee": "codex"}], rw["dispatched"])
            # the next rework is capped: block instead of looping
            h.complete("codex", tid, "implement", "done")
            capped = h.complete("rev", tid, "review", "rework", "still broken")
            self.assertEqual("blocked", capped["outcome"])
            self.assertTrue(capped.get("rework_limited"))
            self.assertEqual([], capped["dispatched"])
            self.assertEqual("blocked", h.task(tid)["task_status"])

    def test_rework_budget_is_per_originating_step(self):
        # review->implement and test->implement both loop back to implement, but
        # each step keeps its own rework budget: a tester rework must not drain
        # the reviewer's, so the reviewer can still loop back afterwards.
        steps = [
            {"id": "intake", "name": "Intake", "role_id": "hub", "task_status": "created", "required": True},
            {"id": "implement", "name": "Implement", "role_id": "implementer", "task_status": "in_progress", "required": True},
            {"id": "review", "name": "Review", "role_id": "reviewer", "task_status": "in_progress", "required": True},
            {"id": "test", "name": "Test", "role_id": "tester", "task_status": "in_progress", "required": True},
            {"id": "accept", "name": "Accept", "role_id": "hub", "task_status": "in_progress", "required": True},
        ]
        edges = [
            {"from": "intake", "to": "implement"},
            {"from": "implement", "to": "review"},
            {"from": "review", "to": "test"},
            {"from": "test", "to": "accept"},
            {"from": "review", "to": "implement"},  # rework loop-back
            {"from": "test", "to": "implement"},     # rework loop-back
        ]
        team = [
            {"agent_name": "hub-agent", "role_id": "hub"},
            {"agent_name": "codex", "role_id": "implementer"},
            {"agent_name": "rev", "role_id": "reviewer"},
            {"agent_name": "qa", "role_id": "tester"},
        ]
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, steps=steps, edges=edges, team=team)
            tid = h.create_task()
            h.start(tid)
            h.complete("hub-agent", tid, "intake", "done")
            # reviewer loop-back #1 (review's budget: 1/2)
            h.complete("codex", tid, "implement", "done")
            rw1 = h.complete("rev", tid, "review", "rework", "r1")
            self.assertEqual([{"step": "implement", "assignee": "codex"}], rw1["dispatched"])
            # pass review, advance to test; tester loop-back (a different step's budget)
            h.complete("codex", tid, "implement", "done")
            h.complete("rev", tid, "review", "done")
            tw = h.complete("qa", tid, "test", "rework", "t1")
            self.assertEqual([{"step": "implement", "assignee": "codex"}], tw["dispatched"])
            # reviewer loop-back #2: still within review's own budget, so it loops
            # back instead of blocking (old shared counter would block here).
            h.complete("codex", tid, "implement", "done")
            rw2 = h.complete("rev", tid, "review", "rework", "r2")
            self.assertEqual("rework", rw2["outcome"])
            self.assertEqual([{"step": "implement", "assignee": "codex"}], rw2["dispatched"])
            # review has now spent 2 of its own budget (unaffected by the tester
            # rework); use up the rest, each still loops back.
            for _ in range(server.MAX_REWORK_ROUNDS - 2):
                h.complete("codex", tid, "implement", "done")
                more = h.complete("rev", tid, "review", "rework", "again")
                self.assertEqual("rework", more["outcome"])
            # one past review's own cap -> block
            h.complete("codex", tid, "implement", "done")
            capped = h.complete("rev", tid, "review", "rework", "final")
            self.assertEqual("blocked", capped["outcome"])
            self.assertTrue(capped.get("rework_limited"))

    def test_blocked_reason_from_latest_transition(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            tid = h.create_task()
            h.start(tid)
            h.complete("hub-agent", tid, "intake", "done")
            h.complete("codex", tid, "implement", "blocked", "disk full, cannot build")
            task = h.task(tid)
            self.assertEqual("blocked", task["task_status"])
            self.assertEqual(
                "disk full, cannot build",
                server._task_blocked_reason(h.store, task),
            )

    def test_blocked_reason_none_when_not_blocked(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            tid = h.create_task()
            self.assertIsNone(server._task_blocked_reason(h.store, h.task(tid)))

    def test_reopen_loop_into_entry_is_executable(self):
        # accept -> intake is a legitimate reopen loop; entry/terminal checks
        # must classify it as a loop-back, not "no entry step".
        edges = LINEAR_EDGES + [{"from": "accept", "to": "intake"}]
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, edges=edges)
            task_id = h.create_task()
            started = h.start(task_id)
            self.assertEqual(
                [{"step": "intake", "assignee": "hub-agent"}],
                started["dispatched"],
            )
            h.complete("hub-agent", task_id, "intake", "done")
            h.complete("codex", task_id, "implement", "done")
            h.complete("rev", task_id, "review", "done")
            closed = h.complete("hub-agent", task_id, "accept", "done")
            self.assertTrue(closed.get("closed"))


class StepTimeoutTests(unittest.TestCase):
    def _timed_steps(self, timeout=30):
        steps = [dict(s) for s in LINEAR_STEPS]
        for s in steps:
            if s["id"] == "implement":
                s["timeout_minutes"] = timeout
        return steps

    def _start_at_implement(self, h, task_id):
        h.start(task_id)
        h.complete("hub-agent", task_id, "intake", "done")

    def test_timed_out_step_is_reassigned_to_other_member(self):
        from datetime import datetime, timedelta, timezone

        team = TEAM + [{"agent_name": "other-codex", "role_id": "implementer"}]
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, steps=self._timed_steps(), team=team)
            task_id = h.create_task()
            self._start_at_implement(h, task_id)

            later = datetime.now(timezone.utc) + timedelta(minutes=31)
            actions = server.check_workflow_step_timeouts(h.store, tmp, now=later)

            self.assertEqual(1, len(actions))
            self.assertEqual("reassigned", actions[0]["action"])
            self.assertEqual("codex", actions[0]["from"])
            self.assertEqual("other-codex", actions[0]["to"])
            # old assignee lost the step, new one can complete it
            with self.assertRaisesRegex(InvalidInputError, "not assigned"):
                h.complete("codex", task_id, "implement", "done")
            result = h.complete("other-codex", task_id, "implement", "done")
            self.assertEqual([{"step": "review", "assignee": "rev"}], result["dispatched"])
            # hub heard about the reassignment
            self.assertTrue(
                any("timed out" in c for _, c in h.inbox_senders("hub-agent"))
            )

    def test_timeout_without_alternative_notifies_hub_once(self):
        from datetime import datetime, timedelta, timezone

        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, steps=self._timed_steps())
            task_id = h.create_task()
            self._start_at_implement(h, task_id)

            later = datetime.now(timezone.utc) + timedelta(minutes=31)
            first = server.check_workflow_step_timeouts(h.store, tmp, now=later)
            second = server.check_workflow_step_timeouts(h.store, tmp, now=later)

            self.assertEqual(1, len(first))
            self.assertEqual("notified_hub", first[0]["action"])
            self.assertEqual([], second)  # one alert per dispatch
            # step still active for the original assignee
            self.assertIn("implement", h.state(task_id)["active_steps"])
            h.complete("codex", task_id, "implement", "done")

    def test_step_within_timeout_is_untouched(self):
        from datetime import datetime, timedelta, timezone

        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, steps=self._timed_steps())
            task_id = h.create_task()
            self._start_at_implement(h, task_id)

            soon = datetime.now(timezone.utc) + timedelta(minutes=5)
            self.assertEqual([], server.check_workflow_step_timeouts(h.store, tmp, now=soon))

    def test_step_without_timeout_never_fires(self):
        from datetime import datetime, timedelta, timezone

        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)  # no timeout_minutes anywhere
            task_id = h.create_task()
            self._start_at_implement(h, task_id)

            much_later = datetime.now(timezone.utc) + timedelta(days=7)
            self.assertEqual(
                [], server.check_workflow_step_timeouts(h.store, tmp, now=much_later)
            )


class GoalTests(unittest.TestCase):
    def test_goal_intake_card_settled_even_if_subtask_dispatch_fails(self):
        with TemporaryDirectory() as tmp:
            # A runner-ready team clears the pre-dispatch validation gate, so the
            # mocked dispatch crash below exercises the settle-before-dispatch path.
            h = EngineHarness(tmp, team=[
                {"agent_name": "hub-agent", "role_id": "hub", "runner_command": "cat"},
                {"agent_name": "codex", "role_id": "implementer", "runner_command": "cat"},
                {"agent_name": "integrator", "role_id": "integrator", "runner_command": "cat"},
                {"agent_name": "rev", "role_id": "reviewer", "runner_command": "cat"},
            ])
            goal_id = h.create_task(title="goal")
            h.store.update_task_metadata(goal_id, is_goal=True)
            h.start(goal_id)  # dispatch intake -> materialize the goal's card
            card = h.store.find_open_step_card(goal_id, "intake")
            self.assertIsNotNone(card)
            step = next(
                s for s in server.read_workflow_config(tmp)["steps"] if s["id"] == "intake"
            )
            result = '{"tasks":[{"title":"t","content":"c","acceptance":"a"}]}'
            with mock.patch.object(
                server, "_start_goal_business_subtasks", side_effect=RuntimeError("boom")
            ):
                with self.assertRaises(RuntimeError):
                    server._complete_goal_intake_locked(
                        h.store, tmp, h.task(goal_id), step, "hub-agent", result
                    )
            # the intake card is settled before subtasks dispatch, so it is not
            # left stuck in_progress when dispatch blows up
            self.assertEqual("closed", h.task(card["id"])["task_status"])

    def test_goal_intake_revalidates_team_before_dispatch(self):
        with TemporaryDirectory() as tmp:
            runners = [
                {"agent_name": "hub-agent", "role_id": "hub", "runner_command": "cat"},
                {"agent_name": "codex", "role_id": "implementer", "runner_command": "cat"},
                {"agent_name": "rev", "role_id": "reviewer", "runner_command": "cat"},
            ]
            h = EngineHarness(tmp, team=runners)
            goal_id = h.create_task(title="goal")
            h.store.update_task_metadata(goal_id, is_goal=True)
            h.start(goal_id)
            step = next(
                s for s in server.read_workflow_config(tmp)["steps"] if s["id"] == "intake"
            )
            # Team loses its implementer after the goal started but before intake
            # finished: dispatching the business subtasks now would only strand
            # them blocked, so completing intake must refuse instead.
            server.write_team_config(
                [m for m in runners if m["role_id"] != "implementer"], tmp
            )
            result = '{"tasks":[{"title":"t","content":"c"}]}'
            with self.assertRaisesRegex(InvalidInputError, "implementer"):
                server._complete_goal_intake_locked(
                    h.store, tmp, h.task(goal_id), step, "hub-agent", result
                )
            # intake stays open (not settled) so hub can retry after fixing team,
            # and no business subtask was dispatched
            self.assertIsNotNone(h.store.find_open_step_card(goal_id, "intake"))
            business = [
                t for t in h.store.list_tasks(status="all")
                if t.get("parent_task_id") == goal_id
                and t.get("source_message_id") is not None
            ]
            self.assertEqual([], business)

    def test_subtask_links_to_goal_and_summary_aggregates(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            goal_id = h.create_task(title="Big goal")
            goal = h.task(goal_id)
            h.store.update_task_metadata(goal_id, is_goal=True)

            # hub splits: subtasks reply to the goal's source message
            for n in (1, 2):
                h.store.send_message(
                    "hub-agent", "hub-agent", f"part {n}", kind="task",
                    title=f"Sub {n}", reply_to=goal["source_message_id"],
                )
            subs = [t for t in h.store.list_tasks() if t.get("parent_task_id") == goal_id]
            self.assertEqual(2, len(subs))

            h.store.update_task_item_status(subs[0]["id"], "closed")
            h.store.update_task_item_status(subs[1]["id"], "blocked")

            [summary] = server.goals_summary(h.store)
            self.assertEqual(goal_id, summary["id"])
            self.assertTrue(summary["is_goal"])
            self.assertEqual(2, summary["subtask_total"])
            self.assertEqual(1, summary["subtask_closed"])
            self.assertEqual(1, summary["subtask_blocked"])
            self.assertEqual(
                {"Sub 1", "Sub 2"}, {s["title"] for s in summary["subtasks"]}
            )

    def test_non_goal_tasks_absent_from_summary(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            h.create_task(title="plain task")
            self.assertEqual([], server.goals_summary(h.store))


class StepCardTests(unittest.TestCase):
    def _goal(self, h):
        goal_id = h.create_task(title="Ship goal")
        h.store.update_task_metadata(goal_id, is_goal=True)
        return goal_id

    def _cards(self, h, parent_id):
        return {
            t["workflow_step"]: t
            for t in h.store.list_tasks()
            if t.get("parent_task_id") == parent_id
            and t.get("source_message_id") is None
        }

    def _team_with_runners(self):
        return [
            {"agent_name": "hub-agent", "role_id": "hub", "runner_command": "cat"},
            {"agent_name": "codex", "role_id": "implementer", "runner_command": "cat"},
            {"agent_name": "integrator", "role_id": "integrator", "runner_command": "cat"},
            {"agent_name": "rev", "role_id": "reviewer", "runner_command": "cat"},
        ]

    def _step(self, h, step_id):
        cfg = server.read_workflow_config(h.root)
        return next(s for s in cfg["steps"] if s["id"] == step_id)

    def _member(self, h, name):
        return next(
            m for m in server.read_team_config(h.root)["members"]
            if m["agent_name"] == name
        )

    def test_goal_intake_creates_business_tasks_and_step_cards(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._team_with_runners())
            goal_id = self._goal(h)
            h.start(goal_id)

            cards = self._cards(h, goal_id)
            self.assertEqual(["intake"], list(cards))
            self.assertEqual("hub-agent", cards["intake"]["assignee"])
            self.assertEqual("assigned", cards["intake"]["task_status"])
            self.assertIn("Intake", cards["intake"]["title"])

            report = server.run_step_worker(
                h.store,
                tmp,
                goal_id,
                self._step(h, "intake"),
                {
                    **self._member(h, "hub-agent"),
                    "runner_command": (
                        "printf '%s' "
                        "'{\"tasks\":[{\"title\":\"API\",\"content\":\"Build API\",\"acceptance\":\"tests\"},"
                        "{\"title\":\"UI\",\"content\":\"Build UI\"}]}'"
                    ),
                },
            )
            self.assertEqual("done", report["outcome"])
            self.assertEqual(2, len(report["created_subtasks"]))

            business = [
                t for t in h.store.list_tasks(status="all")
                if t.get("parent_task_id") == goal_id
                and t.get("source_message_id") is not None
            ]
            self.assertEqual({"API", "UI"}, {t["title"] for t in business})
            for subtask in business:
                cards = self._cards(h, subtask["id"])
                self.assertEqual(["intake"], list(cards))
                self.assertEqual("hub-agent", cards["intake"]["assignee"])

    def test_goal_runner_preflight_rejects_missing_command(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)  # hub-agent has no runner command/default
            with self.assertRaisesRegex(InvalidInputError, "runner commands"):
                server._validate_goal_auto_runners(h.store, tmp, "Goal", "Build it")

    def test_goal_preflight_rejects_missing_required_role(self):
        with TemporaryDirectory() as tmp:
            # reviewer role has no enabled member -> team is not sound
            team = [m for m in self._team_with_runners() if m["role_id"] != "reviewer"]
            h = EngineHarness(tmp, team=team)
            with self.assertRaisesRegex(InvalidInputError, "missing required roles"):
                server._validate_goal_auto_runners(h.store, tmp, "Goal", "Build it")

    def test_goal_intake_invalid_json_blocks_goal(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._team_with_runners())
            goal_id = self._goal(h)
            h.start(goal_id)

            report = server.run_step_worker(
                h.store,
                tmp,
                goal_id,
                self._step(h, "intake"),
                {**self._member(h, "hub-agent"), "runner_command": "echo not-json"},
            )

            self.assertEqual("blocked", report["outcome"])
            self.assertEqual("stalled", h.task(goal_id)["task_status"])  # goal vocabulary
            self.assertEqual("blocked", self._cards(h, goal_id)["intake"]["task_status"])

    def test_business_task_step_cards_settle_and_goal_waits_for_acceptance(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._team_with_runners())
            goal_id = self._goal(h)
            goal = h.task(goal_id)
            [message_id] = h.store.send_message(
                "hub-agent",
                "hub-agent",
                "Build API",
                reply_to=goal["source_message_id"],
                kind="task",
                title="API",
            )
            subtask = next(
                t for t in h.store.list_tasks(status="all")
                if t["source_message_id"] == message_id
            )
            h.start(subtask["id"])

            h.complete("hub-agent", subtask["id"], "intake", "done")
            cards = self._cards(h, subtask["id"])
            self.assertEqual("closed", cards["intake"]["task_status"])
            self.assertEqual("assigned", cards["implement"]["task_status"])
            self.assertEqual("codex", cards["implement"]["assignee"])

            # blocked marks the card blocked; recovery closes it
            h.complete("codex", subtask["id"], "implement", "blocked", "need choice")
            self.assertEqual("blocked", self._cards(h, subtask["id"])["implement"]["task_status"])
            h.complete("hub-agent", subtask["id"], "implement", "done")
            cards = self._cards(h, subtask["id"])
            self.assertEqual("closed", cards["implement"]["task_status"])
            self.assertEqual("assigned", cards["review"]["task_status"])

            # rework closes the review card and opens a fresh implement card
            h.complete("rev", subtask["id"], "review", "rework", "tests missing")
            cards = [
                t for t in h.store.list_tasks()
                if t.get("parent_task_id") == subtask["id"] and t["workflow_step"] == "implement"
            ]
            self.assertEqual(2, len(cards))
            open_cards = [c for c in cards if c["task_status"] != "closed"]
            self.assertEqual(1, len(open_cards))

            h.complete("codex", subtask["id"], "implement", "done")
            h.complete("rev", subtask["id"], "review", "done")
            h.complete("hub-agent", subtask["id"], "accept", "done")
            leftovers = [
                t for t in h.store.list_tasks()
                if t.get("parent_task_id") == subtask["id"] and t["task_status"] != "closed"
            ]
            self.assertEqual([], leftovers)
            self.assertEqual("accepted", h.task(goal_id)["task_status"])

    def test_blocked_reason_for_step_card_uses_parent_transition(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._team_with_runners())
            goal = h.task(self._goal(h))
            [mid] = h.store.send_message(
                "hub-agent", "hub-agent", "Build API",
                reply_to=goal["source_message_id"], kind="task", title="API",
            )
            subtask = next(
                t for t in h.store.list_tasks(status="all")
                if t["source_message_id"] == mid
            )
            h.start(subtask["id"])
            h.complete("hub-agent", subtask["id"], "intake", "done")
            h.complete("codex", subtask["id"], "implement", "blocked", "API spec missing")
            card = self._cards(h, subtask["id"])["implement"]
            self.assertEqual("blocked", card["task_status"])
            # the card carries no transitions of its own — the reason is on the parent
            self.assertIsNone(h.store.get_task(card["id"]).get("source_message_id"))
            self.assertEqual(
                "API spec missing",
                server._task_blocked_reason(h.store, card),
            )

    def test_non_goal_tasks_get_no_step_cards(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            task_id = h.create_task(title="plain")
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")
            self.assertEqual({}, self._cards(h, task_id))

    def test_parse_depends_on_to_zero_based_indices(self):
        tasks = server._parse_goal_subtasks(
            '{"tasks":[{"title":"A","content":"a"},'
            '{"title":"B","content":"b","depends_on":[1]}]}'
        )
        self.assertEqual([], tasks[0]["deps"])
        self.assertEqual([0], tasks[1]["deps"])

    def test_parse_rejects_invalid_depends_on(self):
        cases = [
            '{"tasks":[{"title":"A","content":"a","depends_on":[2]}]}',        # out of range
            '{"tasks":[{"title":"A","content":"a","depends_on":[1]}]}',        # self
            '{"tasks":[{"title":"A","content":"a","depends_on":[2]},'
            '{"title":"B","content":"b","depends_on":[1]}]}',                  # cycle
            '{"tasks":[{"title":"A","content":"a","depends_on":"x"}]}',        # not a list
        ]
        for bad in cases:
            with self.assertRaises(server.InvalidInputError):
                server._parse_goal_subtasks(bad)

    def test_dependent_subtask_is_held_then_released_on_prereq_close(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._team_with_runners())
            goal_id = self._goal(h)
            h.start(goal_id)
            report = server.run_step_worker(
                h.store, tmp, goal_id, self._step(h, "intake"),
                {
                    **self._member(h, "hub-agent"),
                    "runner_command": (
                        "printf '%s' "
                        "'{\"tasks\":[{\"title\":\"A\",\"content\":\"a\"},"
                        "{\"title\":\"B\",\"content\":\"b\",\"depends_on\":[1]}]}'"
                    ),
                },
            )
            self.assertEqual("done", report["outcome"])
            business = {
                t["title"]: t for t in h.store.list_tasks(status="all")
                if t.get("parent_task_id") == goal_id
                and t.get("source_message_id") is not None
            }
            a, b = business["A"], business["B"]
            # A dispatched into the workflow; B held (created, no step) on A.
            self.assertTrue(h.task(a["id"])["workflow_step"])
            self.assertEqual("created", h.task(b["id"])["task_status"])
            self.assertFalse(h.task(b["id"])["workflow_step"])
            self.assertEqual([a["id"]], h.task(b["id"])["depends_on"])
            # Close A -> roll-up releases B.
            h.store.set_task_workflow_state(a["id"], task_status="closed")
            server._recompute_parent_goal_status(h.store, h.task(a["id"]), tmp)
            self.assertTrue(h.task(b["id"])["workflow_step"])


class TeamLockTests(unittest.TestCase):
    def test_team_locked_while_workflow_tasks_run(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            self.assertIsNone(server.team_locked_reason(h.store))

            task_id = h.create_task()
            h.start(task_id)
            reason = server.team_locked_reason(h.store)
            self.assertIn(f"#{task_id}", reason)

            # blocked task releases the lock — fixing the team is the way out
            h.complete("hub-agent", task_id, "intake", "blocked", "need input")
            self.assertIsNone(server.team_locked_reason(h.store))

            # resuming (hub completes the active step) locks again
            h.complete("hub-agent", task_id, "intake", "done")
            self.assertIsNotNone(server.team_locked_reason(h.store))

            # run to completion -> closed -> unlocked
            h.complete("codex", task_id, "implement", "done")
            h.complete("rev", task_id, "review", "done")
            h.complete("hub-agent", task_id, "accept", "done")
            self.assertIsNone(server.team_locked_reason(h.store))


class AutoRunnerTests(unittest.TestCase):
    def _team_with_runner(self, command="cat"):
        team = [dict(m) for m in TEAM]
        for m in team:
            if m["agent_name"] == "codex":
                m["runner_command"] = command
        return team

    def _implement_step(self, h):
        cfg = __import__("orbit.server", fromlist=["server"]).read_workflow_config(h.root)
        return next(s for s in cfg["steps"] if s["id"] == "implement")

    def _member(self, h, name):
        import orbit.server as server
        members = server.read_team_config(h.root)["members"]
        return next(m for m in members if m["agent_name"] == name)

    def _run_event_types(self, run):
        path = Path(run["log_dir"]) / "events.jsonl"
        return [
            json.loads(line)["type"]
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def _review_step(self, h):
        cfg = __import__("orbit.server", fromlist=["server"]).read_workflow_config(h.root)
        return next(s for s in cfg["steps"] if s["id"] == "review")

    def test_reviewer_runner_rework_verdict_loops_back(self):
        with TemporaryDirectory() as tmp:
            team = [dict(m) for m in TEAM]
            for m in team:
                m["max_concurrent_tasks"] = 0
                if m["agent_name"] == "rev":
                    m["runner_command"] = "printf 'looks off\\nWORKFLOW_OUTCOME: rework\\n'"
            h = EngineHarness(tmp, team=team)
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")
            h.complete("codex", task_id, "implement", "done")
            self.assertEqual("rev", h.task(task_id)["assignee"])

            # review runner exits 0 but votes rework -> loop back to implement
            report = server.run_step_worker(
                h.store, tmp, task_id, self._review_step(h), self._member(h, "rev")
            )
            self.assertEqual(
                [{"step": "implement", "assignee": "codex"}], report["dispatched"]
            )

    def test_runner_blocked_verdict_blocks_despite_exit0(self):
        with TemporaryDirectory() as tmp:
            team = [dict(m) for m in TEAM]
            for m in team:
                m["runner_command"] = "printf 'ran but broke\\nWORKFLOW_OUTCOME: blocked\\n'"
            h = EngineHarness(tmp, team=team)
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")

            # implement runner exits 0 but self-reports failure
            report = server.run_step_worker(
                h.store, tmp, task_id, self._implement_step(h), self._member(h, "codex")
            )
            self.assertEqual("blocked", h.task(task_id)["task_status"])
            self.assertEqual([], report["dispatched"])
            self.assertEqual("failed", h.store.list_task_runs(task_id)[0]["status"])

    def test_runner_not_wedged_by_child_holding_pipe(self):
        # A backgrounded child inherits the runner's stdout/stderr and holds them
        # open after the command exits, so the reader's os.read never sees EOF.
        # The runner must still finish promptly (bounded join), not hang forever.
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._team_with_runner("sleep 5 & printf ok"))
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")
            start = time.monotonic()
            with mock.patch.object(server, "RUNNER_STREAM_DRAIN_SECONDS", 0.2):
                report = server.run_step_worker(
                    h.store, tmp, task_id, self._implement_step(h),
                    self._member(h, "codex"),
                )
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 3.0)  # did not wait out the 5s child
            # exit 0 with output -> done -> advanced to review
            self.assertEqual([{"step": "review", "assignee": "rev"}], report["dispatched"])

    def test_descendant_pids_walks_process_tree_and_kill_reaps_it(self):
        import subprocess as sp
        # sh backgrounds one sleep and foregrounds another: both are descendants.
        proc = sp.Popen(["sh", "-c", "sleep 30 & sleep 30"], start_new_session=True)
        try:
            time.sleep(0.4)
            pids = server._descendant_pids(proc.pid)
            self.assertTrue(pids, "descendant walk found no children")
        finally:
            server._kill_process_group(proc)
            proc.wait(timeout=5)

    def test_reviewer_runner_default_verdict_advances(self):
        with TemporaryDirectory() as tmp:
            team = [dict(m) for m in TEAM]
            for m in team:
                m["max_concurrent_tasks"] = 0
                if m["agent_name"] == "rev":
                    m["runner_command"] = "echo lgtm"  # no verdict line -> done
            h = EngineHarness(tmp, team=team)
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")
            h.complete("codex", task_id, "implement", "done")

            report = server.run_step_worker(
                h.store, tmp, task_id, self._review_step(h), self._member(h, "rev")
            )
            self.assertEqual([{"step": "accept", "assignee": "hub-agent"}], report["dispatched"])

    def test_worker_success_advances_step_with_stdout_result(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._team_with_runner("echo done: docs/x.md"))
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")

            report = server.run_step_worker(
                h.store, tmp, task_id, self._implement_step(h),
                self._member(h, "codex"),
            )

            self.assertEqual([{"step": "review", "assignee": "rev"}], report["dispatched"])
            runs = h.store.list_task_runs(task_id)
            self.assertEqual("succeeded", runs[0]["status"])
            self.assertEqual(
                ["run_created", "runner_started", "runner_finished"],
                self._run_event_types(runs[0]),
            )
            # review dispatch message carries the runner's stdout as upstream result
            self.assertTrue(
                any("done: docs/x.md" in c for _, c in h.inbox_senders("rev"))
            )

    def test_worker_streams_stdout_while_running(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(
                tmp,
                team=self._team_with_runner("printf first; sleep 0.6; printf second"),
            )
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")
            report = {}

            def target():
                report.update(server.run_step_worker(
                    h.store, tmp, task_id, self._implement_step(h),
                    self._member(h, "codex"),
                ))

            thread = threading.Thread(target=target)
            thread.start()
            stdout_path = None
            deadline = time.time() + 2
            while time.time() < deadline:
                runs = h.store.list_task_runs(task_id)
                if runs:
                    stdout_path = Path(runs[0]["log_dir"]) / "stdout.log"
                    if stdout_path.exists() and "first" in stdout_path.read_text(encoding="utf-8"):
                        break
                time.sleep(0.05)
            else:
                self.fail("runner stdout was not written before process exit")

            self.assertTrue(thread.is_alive())
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertEqual([{"step": "review", "assignee": "rev"}], report["dispatched"])
            self.assertEqual("firstsecond", stdout_path.read_text(encoding="utf-8"))

    def test_worker_timeout_is_not_blocked_by_unread_stdin(self):
        with TemporaryDirectory() as tmp:
            command = f'"{sys.executable}" -c "import time; time.sleep(5)"'
            h = EngineHarness(tmp, team=self._team_with_runner(command))
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")

            with mock.patch.object(
                server, "_build_step_prompt", return_value="x" * 1_000_000
            ):
                started = time.time()
                report = server.run_step_worker(
                    h.store,
                    tmp,
                    task_id,
                    self._implement_step(h),
                    self._member(h, "codex"),
                    timeout_seconds=0.2,
                )

            self.assertLess(time.time() - started, 2)
            self.assertEqual("blocked", report["outcome"])
            self.assertIn("timed out", report["runner_result"])
            self.assertEqual("timeout", h.store.list_task_runs(task_id)[0]["status"])

    def test_worker_failure_blocks_and_notifies_hub(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._team_with_runner("echo boom >&2; exit 3"))
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")

            server.run_step_worker(
                h.store, tmp, task_id, self._implement_step(h),
                self._member(h, "codex"),
            )

            self.assertEqual("blocked", h.task(task_id)["task_status"])
            self.assertEqual("failed", h.store.list_task_runs(task_id)[0]["status"])
            self.assertTrue(
                any("runner exited 3" in c for _, c in h.inbox_senders("hub-agent"))
            )

    def test_worker_timeout_blocks(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._team_with_runner("sleep 5"))
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")

            server.run_step_worker(
                h.store, tmp, task_id, self._implement_step(h),
                self._member(h, "codex"), timeout_seconds=0.2,
            )

            self.assertEqual("blocked", h.task(task_id)["task_status"])
            run = h.store.list_task_runs(task_id)[0]
            self.assertEqual("timeout", run["status"])
            self.assertEqual(
                ["run_created", "runner_started", "runner_timeout", "runner_finished"],
                self._run_event_types(run),
            )

    def test_dispatch_queues_runner_job_only_when_runner_command_exists(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._team_with_runner())
            task_id = h.create_task()
            h.start(task_id)  # intake -> hub-agent, no default runner
            self.assertEqual([], h.store.list_run_jobs(status="all"))
            h.complete("hub-agent", task_id, "intake", "done")  # -> codex, explicit runner
            jobs = h.store.list_run_jobs(status="all")
            self.assertEqual(1, len(jobs))
            self.assertEqual("pending", jobs[0]["status"])
            self.assertEqual("implement", jobs[0]["step"])
            self.assertEqual("codex", jobs[0]["assignee"])

    def test_runner_claims_and_reports_then_scheduler_advances(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._team_with_runner("echo done"))
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")

            # Runner only executes + reports; it does not advance.
            result = server.run_queued_job(h.store, tmp, runner_name="runner-1")
            self.assertIsNotNone(result)
            self.assertEqual("finished", result["status"])
            self.assertEqual("done", result["outcome"])
            job = h.store.list_run_jobs(status="finished")[0]
            self.assertEqual("done", job["outcome"])
            runs = h.store.list_task_runs(task_id)
            self.assertEqual("succeeded", runs[0]["status"])

            # The scheduler applies the outcome and advances the workflow.
            processed = server.scheduler_tick(h.store, tmp)
            self.assertEqual(1, len(processed))
            self.assertEqual(
                [{"step": "review", "assignee": "rev"}],
                processed[0]["report"]["dispatched"],
            )
            # The finished job is now marked done (advanced).
            self.assertEqual("done", h.store.get_run_job(job["id"])["status"])

    def test_empty_output_is_blocked_not_done(self):
        with TemporaryDirectory() as tmp:
            # `true` exits 0 with no output — a silent runner (like agy producing
            # nothing) must not count as done.
            h = EngineHarness(tmp, team=self._team_with_runner("true"))
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")  # queues implement
            result = server.run_queued_job(h.store, tmp, runner_name="runner-1")
            self.assertEqual("finished", result["status"])
            self.assertEqual("blocked", result["outcome"])

    def test_goal_step_run_recorded_on_step_card(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._team_with_runner("echo built"))
            goal_id = h.create_task(title="Goal")
            h.store.update_task_metadata(goal_id, is_goal=True)
            goal = h.task(goal_id)
            [message_id] = h.store.send_message(
                "hub-agent",
                "hub-agent",
                "Build API",
                reply_to=goal["source_message_id"],
                kind="task",
                title="API",
            )
            subtask = next(
                t for t in h.store.list_tasks(status="all")
                if t["source_message_id"] == message_id
            )
            h.start(subtask["id"])
            h.complete("hub-agent", subtask["id"], "intake", "done")

            card = h.store.find_open_step_card(subtask["id"], "implement")
            server.run_step_worker(
                h.store, tmp, subtask["id"], self._implement_step(h),
                self._member(h, "codex"),
            )

            self.assertEqual([], h.store.list_task_runs(subtask["id"]))
            runs = h.store.list_task_runs(card["id"])
            self.assertEqual(1, len(runs))
            self.assertEqual("succeeded", runs[0]["status"])
            self.assertEqual("codex", runs[0]["worker"])

    def test_cli_runner_command_defaults(self):
        cmd = server._runner_command_for({"agent_name": "antigravity"})
        self.assertEqual('agy --dangerously-skip-permissions --print "$(cat)"', cmd)

        cmd = server._runner_command_for({"agent_name": "hermes"})
        self.assertEqual('hermes --yolo -z "$(cat)"', cmd)
        cmd = server._runner_command_for({"agent_name": "opencode"})
        self.assertEqual('opencode run --auto "$(cat)"', cmd)
        cmd = server._runner_command_for({"agent_name": "hermes-manager"})
        self.assertEqual('hermes --profile manager --yolo -z "$(cat)"', cmd)
        # explicit runner_command still wins
        cmd = server._runner_command_for(
            {"agent_name": "hermes-manager", "runner_command": "hermes chat"}
        )
        self.assertEqual("hermes chat", cmd)

    def test_missing_runner_command_blocks(self):
        from unittest import mock

        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._team_with_runner(""))
            task_id = h.create_task()
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")

            # no member override and no per-tool default -> blocked
            with mock.patch.dict(server._DEFAULT_RUNNER_COMMANDS, {}, clear=True):
                server.run_step_worker(
                    h.store, tmp, task_id, self._implement_step(h),
                    self._member(h, "codex"),
                )
            self.assertEqual("blocked", h.task(task_id)["task_status"])
            self.assertTrue(
                any("no runner command" in c for _, c in h.inbox_senders("hub-agent"))
            )


class RerunTests(unittest.TestCase):
    def _team(self):
        team = [dict(m) for m in TEAM]
        for m in team:
            m["runner_command"] = "cat"
        return team

    def test_rerun_blocked_step_dispatches_to_chosen_agent(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._team())
            task_id = h.create_task()
            server.start_workflow_task(h.store, tmp, "hub-agent", task_id)
            server.advance_workflow_task(
                h.store, tmp, "hub-agent", task_id, "intake", "blocked", "stuck"
            )
            self.assertEqual("blocked", h.task(task_id)["task_status"])

            result = server.rerun_workflow_step(h.store, tmp, task_id, "codex")

            self.assertEqual("intake", result["step"])
            self.assertEqual("codex", result["assignee"])
            self.assertTrue(result["reran"])
            job = h.store.get_run_job(result["queued_job_id"])
            self.assertEqual("pending", job["status"])
            self.assertEqual("intake", job["step"])
            self.assertEqual("codex", job["assignee"])
            transitions = h.store.list_task_transitions(task_id)
            redispatched = [
                t["note"] for t in transitions
                if t["outcome"] == "dispatched" and t["to_step"] == "intake"
            ]
            self.assertIn("codex", redispatched)
            self.assertNotEqual("blocked", h.task(task_id)["task_status"])

    def test_rerun_on_step_card_redirects_to_parent(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._team())
            tid = h.create_task()
            server.start_workflow_task(h.store, tmp, "hub-agent", tid)
            server.advance_workflow_task(
                h.store, tmp, "hub-agent", tid, "intake", "blocked", "stuck"
            )
            # a step card: child of the workflow task, its own step, no transitions
            card = h.create_task(title="Intake · card")
            h.store._conn.execute(
                "UPDATE tasks SET parent_task_id = ?, workflow_step = 'intake' WHERE id = ?",
                (tid, card),
            )
            h.store._conn.commit()

            result = server.rerun_workflow_step(h.store, tmp, card, "codex")
            self.assertEqual(tid, result["task_id"])  # redirected to parent
            self.assertEqual("intake", result["step"])
            self.assertEqual("codex", result["assignee"])

    def test_rerun_refuses_when_a_run_is_in_progress(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._team())
            task_id = h.create_task()
            server.start_workflow_task(h.store, tmp, "hub-agent", task_id)
            # A runner is still in flight for this task's step.
            h.store.create_task_run(task_id, worker="hub-agent", status="running")

            with self.assertRaises(InvalidInputError) as ctx:
                server.rerun_workflow_step(h.store, tmp, task_id, "codex")
            self.assertIn("already in progress", str(ctx.exception))

    def test_rerun_ignores_stale_running_attempt_when_latest_failed(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._team())
            task_id = h.create_task()
            server.start_workflow_task(h.store, tmp, "hub-agent", task_id)
            server.advance_workflow_task(
                h.store, tmp, "hub-agent", task_id, "intake", "blocked", "stuck"
            )
            stale = h.store.create_task_run(task_id, worker="hub-agent", status="running")
            failed = h.store.create_task_run(task_id, worker="hub-agent", status="running")
            h.store.finish_task_run(failed["id"], "failed", 1)

            result = server.rerun_workflow_step(h.store, tmp, task_id, "codex")

            self.assertEqual("intake", result["step"])
            self.assertEqual("codex", result["assignee"])
            self.assertEqual("running", h.store.get_task_run(stale["id"])["status"])
            self.assertEqual("pending", h.store.get_run_job(result["queued_job_id"])["status"])

    def test_rerun_unknown_agent_without_runner_is_rejected(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._team())
            task_id = h.create_task()
            server.start_workflow_task(h.store, tmp, "hub-agent", task_id)
            server.advance_workflow_task(
                h.store, tmp, "hub-agent", task_id, "intake", "blocked", "stuck"
            )
            with mock.patch.dict(server._DEFAULT_RUNNER_COMMANDS, {}, clear=True):
                with self.assertRaises(InvalidInputError) as ctx:
                    server.rerun_workflow_step(h.store, tmp, task_id, "nobody")
            self.assertIn("no runner command", str(ctx.exception))


class MarkTaskRunningTests(unittest.TestCase):
    def _set_status(self, store, task_id, status):
        store.set_task_workflow_state(task_id, task_status=status)

    def test_run_start_marks_card_subtask_and_goal_in_progress(self):
        # A runner records its run on the step card, so marking must start from
        # the card (run_task_id) and roll up card -> subtask -> goal.
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            goal_id = h.create_task(title="goal")
            sub_id = h.create_task(title="sub")
            card_id = h.create_task(title="Intake · card")
            h.store._conn.execute(
                "UPDATE tasks SET is_goal = 1 WHERE id = ?", (goal_id,)
            )
            h.store._conn.execute(
                "UPDATE tasks SET parent_task_id = ? WHERE id = ?", (goal_id, sub_id)
            )
            h.store._conn.execute(
                "UPDATE tasks SET parent_task_id = ? WHERE id = ?", (sub_id, card_id)
            )
            h.store._conn.commit()
            self._set_status(h.store, goal_id, "created")
            self._set_status(h.store, sub_id, "assigned")
            self._set_status(h.store, card_id, "created")

            server._mark_task_running(h.store, card_id)

            self.assertEqual("in_progress", h.task(card_id)["task_status"])
            self.assertEqual("in_progress", h.task(sub_id)["task_status"])
            self.assertEqual("running", h.task(goal_id)["task_status"])  # goal vocabulary

    def test_running_card_remains_in_progress(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            tid = h.create_task(title="active")
            self._set_status(h.store, tid, "in_progress")
            server._mark_task_running(h.store, tid)
            self.assertEqual("in_progress", h.task(tid)["task_status"])

    def test_terminal_parent_is_not_reopened(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            goal_id = h.create_task(title="goal")
            sub_id = h.create_task(title="sub")
            h.store._conn.execute(
                "UPDATE tasks SET is_goal = 1 WHERE id = ?", (goal_id,)
            )
            h.store._conn.execute(
                "UPDATE tasks SET parent_task_id = ? WHERE id = ?", (goal_id, sub_id)
            )
            h.store._conn.commit()
            self._set_status(h.store, goal_id, "accepted")
            self._set_status(h.store, sub_id, "blocked")

            server._mark_task_running(h.store, sub_id)

            self.assertEqual("in_progress", h.task(sub_id)["task_status"])
            self.assertEqual("accepted", h.task(goal_id)["task_status"])


class GoalRollupTests(unittest.TestCase):
    def _mk(self, h, title, status, parent=None, is_goal=False):
        tid = h.create_task(title=title)
        if is_goal:
            h.store._conn.execute("UPDATE tasks SET is_goal = 1 WHERE id = ?", (tid,))
        if parent:
            h.store._conn.execute(
                "UPDATE tasks SET parent_task_id = ? WHERE id = ?", (parent, tid)
            )
        h.store._conn.commit()
        h.store.set_task_workflow_state(tid, task_status=status)
        return tid

    def test_all_subtasks_closed_accepts_goal(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            goal = self._mk(h, "goal", "in_progress", is_goal=True)
            self._mk(h, "s1", "closed", parent=goal)
            s2 = self._mk(h, "s2", "closed", parent=goal)
            server._recompute_parent_goal_status(h.store, h.task(s2), h.root)
            self.assertEqual("accepted", h.task(goal)["task_status"])

    def test_any_blocked_subtask_stalls_goal(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            goal = self._mk(h, "goal", "in_progress", is_goal=True)
            self._mk(h, "s1", "closed", parent=goal)
            s2 = self._mk(h, "s2", "blocked", parent=goal)
            server._recompute_parent_goal_status(h.store, h.task(s2), h.root)
            self.assertEqual("stalled", h.task(goal)["task_status"])

    def test_mixed_subtasks_keep_goal_in_progress(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            goal = self._mk(h, "goal", "stalled", is_goal=True)
            self._mk(h, "s1", "closed", parent=goal)
            s2 = self._mk(h, "s2", "in_progress", parent=goal)
            server._recompute_parent_goal_status(h.store, h.task(s2), h.root)
            self.assertEqual("running", h.task(goal)["task_status"])

    def test_explicitly_closed_goal_is_left_alone(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            goal = self._mk(h, "goal", "closed", is_goal=True)
            s1 = self._mk(h, "s1", "blocked", parent=goal)
            server._recompute_parent_goal_status(h.store, h.task(s1), h.root)
            self.assertEqual("closed", h.task(goal)["task_status"])


class UnlimitedConcurrencyTests(unittest.TestCase):
    def _member(self, max_concurrent):
        return {
            "agent_name": "solo",
            "role_id": "hub",
            "enabled": True,
            "expertise_level": 3,
            "max_concurrent_tasks": max_concurrent,
            "capabilities": [],
        }

    def test_unlimited_member_never_excluded_on_load(self):
        ranked = server.rank_assignment_candidates(
            {"role_required": "hub"}, [self._member(0)], {"solo": 5}, role_id="hub"
        )
        self.assertIsNotNone(ranked.get("selected"))
        self.assertEqual("solo", ranked["selected"]["agent_name"])

    def test_positive_cap_still_excludes_when_full(self):
        ranked = server.rank_assignment_candidates(
            {"role_required": "hub"}, [self._member(1)], {"solo": 1}, role_id="hub"
        )
        self.assertIsNone(ranked.get("selected"))


class RunJobLeaseTests(unittest.TestCase):
    def _job(self, h):
        tid = h.create_task()
        return h.store.create_run_job(tid, "implement", "codex", "echo x", "")

    def test_renew_keeps_lease_and_blocks_reclaim(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            job = self._job(h)
            claimed = h.store.claim_next_run_job("runner-1", lease_seconds=300)
            self.assertEqual(job["id"], claimed["id"])
            # a fresh lease keeps another runner from claiming it
            self.assertIsNone(h.store.claim_next_run_job("runner-2", lease_seconds=300))
            # the heartbeat renews the lease (still owned by runner-1)
            self.assertTrue(h.store.renew_run_job(job["id"], "runner-1", 300))
            self.assertIsNone(h.store.claim_next_run_job("runner-2", lease_seconds=300))

    def test_expired_lease_is_reclaimed_by_another_runner(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            job = self._job(h)
            h.store.claim_next_run_job("runner-1", lease_seconds=300)
            # no heartbeat -> the lease expires
            h.store._conn.execute(
                "UPDATE run_jobs SET leased_until = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", job["id"]),
            )
            h.store._conn.commit()
            stolen = h.store.claim_next_run_job("runner-2", lease_seconds=300)
            self.assertEqual(job["id"], stolen["id"])
            self.assertEqual("runner-2", stolen["claimed_by"])
            # the old owner can no longer renew what it lost
            self.assertFalse(h.store.renew_run_job(job["id"], "runner-1", 300))

    def test_lost_lease_owner_cannot_finish_job(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            job = self._job(h)
            h.store.claim_next_run_job("runner-1", lease_seconds=300)
            h.store._conn.execute(
                "UPDATE run_jobs SET leased_until = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", job["id"]),
            )
            h.store._conn.commit()
            h.store.claim_next_run_job("runner-2", lease_seconds=300)

            stale = h.store.finish_run_job(
                job["id"],
                "finished",
                outcome="done",
                result="stale",
                runner_name="runner-1",
                current_status="running",
            )
            self.assertIsNone(stale)
            current = h.store.get_run_job(job["id"])
            self.assertEqual("running", current["status"])
            self.assertEqual("runner-2", current["claimed_by"])

    def test_finished_job_claim_is_single_owner(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            job = self._job(h)
            h.store.claim_next_run_job("runner-1", lease_seconds=300)
            h.store.finish_run_job(
                job["id"],
                "finished",
                outcome="done",
                result="ok",
                runner_name="runner-1",
                current_status="running",
            )

            claimed = h.store.claim_finished_run_job("scheduler-1")
            self.assertEqual(job["id"], claimed["id"])
            self.assertEqual("applying", claimed["status"])
            self.assertEqual("scheduler-1", claimed["applied_by"])
            # the runner that executed the job stays visible on claimed_by
            self.assertEqual("runner-1", claimed["claimed_by"])
            self.assertIsNone(h.store.claim_finished_run_job("scheduler-2"))
            # a different scheduler cannot finish what scheduler-1 is applying
            self.assertIsNone(h.store.finish_run_job(
                job["id"], "done",
                applied_by="scheduler-2", current_status="applying",
            ))
            done = h.store.finish_run_job(
                job["id"],
                "done",
                applied_by="scheduler-1",
                current_status="applying",
            )
            self.assertEqual("done", done["status"])
            self.assertEqual("runner-1", done["claimed_by"])


class HubInspectTests(unittest.TestCase):
    def _hub_team(self, hub_command):
        team = [dict(m) for m in TEAM]
        for m in team:
            if m["role_id"] == "hub":
                m["runner_command"] = hub_command
        return team

    def _cands(self):
        return [
            {"run_id": 11, "task_id": 1, "title": "a", "step": "implement",
             "assignee": "codex", "elapsed": 400, "output": ""},
            {"run_id": 22, "task_id": 2, "title": "b", "step": "review",
             "assignee": "codex", "elapsed": 400, "output": ""},
        ]

    def test_batch_parses_per_run_decisions(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(
                tmp, team=self._hub_team("printf 'DECISION 1: KILL\\nDECISION 2: CONTINUE\\n'"))
            d = server._hub_inspect_batch(h.store, tmp, self._cands())
            self.assertEqual("kill", d[11])
            self.assertEqual("continue", d[22])

    def test_batch_unclear_defaults_continue(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._hub_team("echo idk"))
            self.assertEqual({11: "continue", 22: "continue"},
                             server._hub_inspect_batch(h.store, tmp, self._cands()))

    def test_batch_no_hub_command_defaults_continue(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)  # default team's hub has no runner command
            self.assertEqual({11: "continue", 22: "continue"},
                             server._hub_inspect_batch(h.store, tmp, self._cands()))

    def test_sweep_flags_stuck_run_instead_of_killing_pid(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._hub_team("echo 'DECISION 1: KILL'"))
            tid = h.create_task()
            run = h.store.create_task_run(
                tid, worker="codex", command="x", workflow_step="implement")
            run_dir = Path(tmp) / "rundir"
            run_dir.mkdir()
            (run_dir / "stdout.log").write_text("", encoding="utf-8")  # silent
            h.store._conn.execute(
                "UPDATE task_runs SET started_at=?, log_dir=?, pid=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", str(run_dir), 999999999, run["id"]),
            )
            h.store._conn.commit()
            server._HUB_SWEEP_STATE.clear()
            flagged = server.hub_inspect_sweep(h.store, tmp)  # silent > 10m -> hub KILL
            server._HUB_SWEEP_STATE.clear()
            self.assertIn(run["id"], flagged)
            # the sweep only signals; it does not kill a pid itself
            self.assertTrue(h.store.run_cancel_requested(run["id"]))

    def test_sweep_kill_blocks_the_workflow_task(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._hub_team("echo 'DECISION 1: KILL'"))
            tid = h.create_task()
            h.start(tid)
            h.complete("hub-agent", tid, "intake", "done")  # implement now active (codex)
            self.assertNotEqual("blocked", h.task(tid)["task_status"])
            run = h.store.create_task_run(
                tid, worker="codex", command="x", workflow_step="implement")
            run_dir = Path(tmp) / "rundir"
            run_dir.mkdir()
            (run_dir / "stdout.log").write_text("", encoding="utf-8")  # silent
            h.store._conn.execute(
                "UPDATE task_runs SET started_at=?, log_dir=?, pid=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", str(run_dir), 999999999, run["id"]),
            )
            h.store._conn.commit()
            server._HUB_SWEEP_STATE.clear()
            flagged = server.hub_inspect_sweep(h.store, tmp)  # silent > 10m -> hub KILL
            server._HUB_SWEEP_STATE.clear()
            self.assertIn(run["id"], flagged)
            self.assertTrue(h.store.run_cancel_requested(run["id"]))
            # the change: hub's KILL blocks the workflow task immediately instead
            # of leaving it in_progress waiting on a possibly-wedged runner.
            self.assertEqual("blocked", h.task(tid)["task_status"])

    def test_sweep_skips_already_condemned_run(self):
        # A run already flagged for kill isn't re-inspected: no second hub call,
        # and it is not returned as freshly flagged.
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._hub_team("echo 'DECISION 1: KILL'"))
            tid = h.create_task()
            run = h.store.create_task_run(
                tid, worker="codex", command="x", workflow_step="implement")
            h.store.request_run_kill(run["id"], "prior sweep")
            h.store._conn.execute(
                "UPDATE task_runs SET started_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", run["id"]),
            )
            h.store._conn.commit()
            server._HUB_SWEEP_STATE.clear()
            server.hub_inspect_sweep(h.store, tmp)
            flagged = server.hub_inspect_sweep(h.store, tmp)
            server._HUB_SWEEP_STATE.clear()
            self.assertEqual([], flagged)

    def test_run_cancel_flag_roundtrip(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            tid = h.create_task()
            run = h.store.create_task_run(tid, worker="codex")
            self.assertFalse(h.store.run_cancel_requested(run["id"]))
            self.assertTrue(h.store.request_run_kill(run["id"]))
            self.assertTrue(h.store.run_cancel_requested(run["id"]))

    def test_sweep_skips_run_that_is_producing_output(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._hub_team("echo 'DECISION 1: KILL'"))
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            (run_dir / "stdout.log").write_text("start", encoding="utf-8")
            old = "2000-01-01T00:00:00+00:00"  # far past the soft timeout
            server._HUB_SWEEP_STATE.clear()
            # a fake running run with a pid that is not ours to kill
            def fake_running():
                return [{"id": 99, "task_id": 1, "worker": "codex",
                         "pid": 999999999, "log_dir": str(run_dir), "started_at": old}]
            h.store.list_running_task_runs = fake_running  # type: ignore
            # first sweep just records the size (no delta yet)
            self.assertEqual([], server.hub_inspect_sweep(h.store, tmp))
            # output grew -> still producing -> skipped, not killed
            (run_dir / "stdout.log").write_text("start more", encoding="utf-8")
            self.assertEqual([], server.hub_inspect_sweep(h.store, tmp))
            server._HUB_SWEEP_STATE.clear()

    def test_sweep_waits_for_ten_minutes_without_output(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._hub_team("echo 'DECISION 1: KILL'"))
            tid = h.create_task()
            run = h.store.create_task_run(
                tid, worker="codex", command="x", workflow_step="implement")
            run_dir = Path(tmp) / "rundir"
            run_dir.mkdir()
            (run_dir / "stdout.log").write_text("", encoding="utf-8")
            recent_start = (
                datetime.now(timezone.utc) - timedelta(seconds=60)
            ).isoformat(timespec="seconds")
            h.store._conn.execute(
                "UPDATE task_runs SET started_at=?, log_dir=?, pid=? WHERE id=?",
                (recent_start, str(run_dir), 999999999, run["id"]),
            )
            h.store._conn.commit()
            server._HUB_SWEEP_STATE.clear()
            self.assertEqual([], server.hub_inspect_sweep(h.store, tmp))
            server._HUB_SWEEP_STATE.clear()


class RunnerScopeTests(unittest.TestCase):
    def test_steps_for_roles_resolves(self):
        with TemporaryDirectory() as tmp:
            EngineHarness(tmp)  # writes the default workflow
            self.assertEqual(["implement"], server._steps_for_roles(tmp, ["implementer"]))
            self.assertEqual({"intake", "accept"}, set(server._steps_for_roles(tmp, ["hub"])))
            self.assertIsNone(server._steps_for_roles(tmp, []))

    def test_claim_filters_by_step(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            t1 = h.create_task()
            h.store.create_run_job(t1, "implement", "codex", "echo x", "")
            review = h.store.create_run_job(t1, "review", "rev", "echo y", "")
            # a runner scoped to the review step claims only that job
            claimed = h.store.claim_next_run_job("r", steps=["review"])
            self.assertEqual(review["id"], claimed["id"])
            self.assertEqual("review", claimed["step"])


class ForceCloseTests(unittest.TestCase):
    def test_force_close_closes_tree_and_orphans_runs(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            goal = h.create_task(title="goal")
            sub = h.create_task(title="sub")
            h.store._conn.execute("UPDATE tasks SET is_goal = 1 WHERE id = ?", (goal,))
            h.store._conn.execute("UPDATE tasks SET parent_task_id = ? WHERE id = ?", (goal, sub))
            h.store._conn.commit()
            h.store.set_task_workflow_state(goal, task_status="in_progress")
            h.store.set_task_workflow_state(sub, task_status="in_progress")
            h.store.create_task_run(sub, worker="gemini", status="running")  # no pid

            result = server.force_close_goal(h.store, tmp, goal)

            self.assertEqual("closed", h.task(goal)["task_status"])
            self.assertEqual("closed", h.task(sub)["task_status"])
            self.assertEqual("orphaned", h.store.list_task_runs(sub)[0]["status"])
            self.assertEqual(0, result["killed_runners"])  # no pid recorded -> nothing killed
            self.assertGreaterEqual(result["closed_tasks"], 2)

    def test_advance_on_terminal_task_is_noop(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            tid = h.create_task()
            h.start(tid)
            h.complete("hub-agent", tid, "intake", "done")  # at implement
            h.store.set_task_workflow_state(tid, task_status="closed")
            # a straggler runner finishing after force-close must not re-open it
            res = server.advance_workflow_task(h.store, tmp, "codex", tid, "implement", "done")
            self.assertEqual([], res["dispatched"])
            self.assertEqual("closed", h.task(tid)["task_status"])


class ActiveStepLedgerTests(unittest.TestCase):
    def _tx(self, h, tid, from_step, to_step, outcome, note=""):
        h.store.record_task_transition(tid, from_step, to_step, "codex", outcome, note)

    def test_killed_runner_extra_dispatch_does_not_phantom_activate(self):
        # A dispatch whose runner was killed (no finishing transition) followed
        # by a re-dispatch that DID finish must not leave the step active.
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            tid = h.create_task()
            self._tx(h, tid, "", "implement", "dispatched", "codex")  # killed run
            self._tx(h, tid, "", "implement", "dispatched", "codex")  # re-dispatch
            self._tx(h, tid, "implement", "review", "done")           # finished
            trans = h.store.list_task_transitions(tid)
            self.assertNotIn("implement", server._active_steps(trans))
            self.assertNotIn("implement", server._active_step_assignees(trans))

    def test_latest_dispatch_without_finish_stays_active(self):
        # A genuinely in-flight (or killed-and-not-yet-recovered) step whose
        # latest dispatch has no finish after it is still active.
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            tid = h.create_task()
            self._tx(h, tid, "implement", "review", "done")   # earlier finish
            self._tx(h, tid, "", "implement", "dispatched", "codex")  # re-dispatched, not finished
            trans = h.store.list_task_transitions(tid)
            self.assertIn("implement", server._active_steps(trans))
            self.assertEqual("codex", server._active_step_assignees(trans)["implement"])


class TaskHealthCheckTests(unittest.TestCase):
    def test_dead_runner_step_alerts_and_dedupes(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            tid = h.create_task()
            h.start(tid)  # intake active, dispatched to hub-agent
            run = h.store.create_task_run(tid, worker="hub-agent", status="running")
            h.store._conn.execute(
                "UPDATE task_runs SET status = 'orphaned' WHERE id = ?", (run["id"],)
            )
            h.store._conn.commit()

            alerts = server.check_task_health(h.store, tmp)
            self.assertEqual(1, len(alerts))
            self.assertEqual("dead runner", alerts[0]["problem"])
            self.assertEqual("intake", alerts[0]["step"])
            self.assertTrue(any(str(tid) in c for _, c in h.inbox_senders("hub-agent")))
            # same unchanged problem is not re-alerted
            self.assertEqual([], server.check_task_health(h.store, tmp))

    def test_orphaned_with_undispatched_next_step_is_blocked(self):
        # intake done -> implement should have been dispatched but was not
        # (advance interrupted). Recover to blocked at implement, visible/rerunnable.
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            tid = h.create_task()
            h.store.record_task_transition(tid, "", "intake", "hub-agent", "dispatched", "hub-agent")
            h.store.record_task_transition(tid, "intake", "implement", "hub-agent", "done", "")
            h.store.set_task_workflow_state(tid, task_status="in_progress")

            alerts = server.check_task_health(h.store, tmp)
            self.assertEqual(1, len(alerts))
            self.assertEqual("orphaned -> blocked", alerts[0]["problem"])
            self.assertEqual("implement", alerts[0]["step"])
            self.assertEqual("blocked", h.task(tid)["task_status"])
            self.assertEqual("implement", h.task(tid)["workflow_step"])
            # now blocked -> no longer matches, not re-processed
            self.assertEqual([], server.check_task_health(h.store, tmp))

    def test_orphaned_after_terminal_done_is_closed(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            tid = h.create_task()
            h.store.record_task_transition(tid, "", "accept", "hub-agent", "dispatched", "hub-agent")
            h.store.record_task_transition(tid, "accept", "", "hub-agent", "done", "")
            h.store.set_task_workflow_state(tid, task_status="in_progress")

            alerts = server.check_task_health(h.store, tmp)
            self.assertEqual("orphaned -> closed", alerts[0]["problem"])
            self.assertEqual("closed", h.task(tid)["task_status"])

    def test_undispatched_rework_alerts_and_dedupes(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            tid = h.create_task()
            h.start(tid)
            h.complete("hub-agent", tid, "intake", "done")
            h.complete("codex", tid, "implement", "done")

            h.store.record_task_transition(
                tid, "review", "implement", "rev", "rework", "tests missing"
            )

            alerts = server.check_task_health(h.store, tmp)
            self.assertEqual(1, len(alerts))
            self.assertEqual("undispatched rework", alerts[0]["problem"])
            self.assertEqual("implement", alerts[0]["step"])
            self.assertEqual([], server.check_task_health(h.store, tmp))

    def test_stale_pending_dispatch_action_alerts_and_dedupes(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            tid = h.create_task()
            action = h.store.create_workflow_action(
                tid, "dispatch_step", step="implement", assignee="codex"
            )
            h.store._conn.execute(
                "UPDATE workflow_actions SET created_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", action["id"]),
            )
            h.store._conn.commit()

            alerts = server.check_task_health(h.store, tmp)
            self.assertEqual(1, len(alerts))
            self.assertEqual("pending workflow action", alerts[0]["problem"])
            self.assertEqual("implement", alerts[0]["step"])
            self.assertEqual(
                "alerted", h.store.get_workflow_action(action["id"])["status"]
            )
            self.assertEqual([], server.check_task_health(h.store, tmp))

    def test_normal_dispatch_completes_workflow_action(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            tid = h.create_task()
            h.start(tid)

            actions = h.store.list_workflow_actions(status="all")
            self.assertEqual(1, len(actions))
            self.assertEqual("dispatch_step", actions[0]["action_type"])
            self.assertEqual("intake", actions[0]["step"])
            self.assertEqual("done", actions[0]["status"])

    def test_no_alert_when_dead_run_predates_redispatch(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            tid = h.create_task()
            h.start(tid)
            run = h.store.create_task_run(tid, worker="hub-agent", status="running")
            h.store._conn.execute(
                "UPDATE task_runs SET status = 'orphaned', "
                "started_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (run["id"],),
            )
            h.store._conn.commit()
            # a fresh dispatch supersedes the dead run; its runner has not
            # recorded a run yet -> watchdog must hold off, not false-alert
            h.store.record_task_transition(
                tid, "", "intake", "hub-agent", "dispatched", "hub-agent"
            )
            self.assertEqual([], server.check_task_health(h.store, tmp))

    def test_healthy_running_step_no_alert(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            tid = h.create_task()
            h.start(tid)
            h.store.create_task_run(tid, worker="hub-agent", status="running")
            self.assertEqual([], server.check_task_health(h.store, tmp))

    def test_blocked_task_ignored_after_runner_failure(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            tid = h.create_task()
            h.start(tid)
            run = h.store.create_task_run(tid, worker="hub-agent", status="running")
            h.store.finish_task_run(run["id"], "failed", 1)
            server.advance_workflow_task(
                h.store, tmp, "hub-agent", tid, "intake", "blocked", "runner failed"
            )
            self.assertEqual([], server.check_task_health(h.store, tmp))

    def test_closed_task_ignored(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            tid = h.create_task()
            h.start(tid)
            run = h.store.create_task_run(tid, worker="hub-agent", status="running")
            h.store._conn.execute(
                "UPDATE task_runs SET status = 'orphaned' WHERE id = ?", (run["id"],)
            )
            h.store.set_task_workflow_state(tid, task_status="closed")
            h.store._conn.commit()
            self.assertEqual([], server.check_task_health(h.store, tmp))


class TokenStatsTests(unittest.TestCase):
    def test_parse_run_tokens_native_and_sentinel(self):
        self.assertEqual(114751, server._parse_run_tokens("", "tokens used\n114,751"))
        self.assertEqual(3200, server._parse_run_tokens("TOKENS_USED: 3,200", ""))
        # an accurate native count wins over the self-reported sentinel
        self.assertEqual(
            114751, server._parse_run_tokens("TOKENS_USED: 5", "tokens used\n114,751")
        )
        self.assertIsNone(server._parse_run_tokens("no usage here", "nothing"))

    def test_step_prompt_asks_for_tokens(self):
        with TemporaryDirectory() as tmp:
            EngineHarness(tmp)
            step = {"id": "implement", "name": "Implement",
                    "role_id": "implementer", "task_status": "in_progress",
                    "required": True}
            prompt = server._build_step_prompt(
                tmp, {"id": 1, "title": "t", "content": ""}, step, ""
            )
            self.assertIn("TOKENS_USED", prompt)

    def test_finish_task_run_persists_tokens(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            tid = h.create_task()
            run = h.store.create_task_run(tid, worker="codex", status="running")
            h.store.finish_task_run(run["id"], "succeeded", 0, 4200)
            self.assertEqual(4200, h.store.list_task_runs(tid)[0]["tokens"])

    def test_goal_tokens_total_sums_subtree(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            g = h.create_task(title="goal")
            s = h.create_task(title="sub")
            c = h.create_task(title="card")
            h.store._conn.execute("UPDATE tasks SET is_goal = 1 WHERE id = ?", (g,))
            h.store._conn.execute("UPDATE tasks SET parent_task_id = ? WHERE id = ?", (g, s))
            h.store._conn.execute("UPDATE tasks SET parent_task_id = ? WHERE id = ?", (s, c))
            h.store._conn.commit()
            r1 = h.store.create_task_run(s, worker="codex", status="running")
            h.store.finish_task_run(r1["id"], "succeeded", 0, 1000)
            r2 = h.store.create_task_run(c, worker="gemini", status="running")
            h.store.finish_task_run(r2["id"], "succeeded", 0, 250)
            goals = {x["id"]: x for x in server.goals_summary(h.store)}
            self.assertEqual(1250, goals[g]["tokens_total"])


class DecomposeStepTests(unittest.TestCase):
    """A `decompose`-flagged step splits a goal after goal-level design steps;
    subtasks then begin at that step's successors, not the workflow entry."""

    STEPS = [
        {"id": "intake", "name": "Intake", "role_id": "hub", "required": True},
        {"id": "design", "name": "Design", "role_id": "architect", "required": True},
        {"id": "plan", "name": "Plan", "role_id": "hub", "decompose": True},
        {"id": "implement", "name": "Implement", "role_id": "implementer", "required": True},
        {"id": "review", "name": "Review", "role_id": "reviewer", "required": True},
        {"id": "accept", "name": "Accept", "role_id": "hub", "required": True},
    ]
    EDGES = [
        {"from": "intake", "to": "design"},
        {"from": "design", "to": "plan"},
        {"from": "plan", "to": "implement"},
        {"from": "implement", "to": "review"},
        {"from": "review", "to": "accept"},
    ]
    TEAM = [
        {"agent_name": "hub-agent", "role_id": "hub", "runner_command": "cat"},
        {"agent_name": "arch", "role_id": "architect", "runner_command": "cat"},
        {"agent_name": "codex", "role_id": "implementer", "runner_command": "cat"},
        {"agent_name": "rev", "role_id": "reviewer", "runner_command": "cat"},
    ]

    def test_flag_normalizes_locks_required_and_forces_isolate_off(self):
        norm = server._normalize_workflow_step(
            {"id": "plan", "name": "Plan", "role_id": "hub",
             "decompose": True, "required": False, "isolate": True}, 0
        )
        self.assertTrue(norm["decompose"])
        self.assertTrue(norm["required"])          # locked by the decompose flag
        self.assertTrue(norm["required_locked"])
        self.assertFalse(norm["isolate"])          # runs at goal level, never isolated

    def test_decompose_step_id_prefers_flag_else_entry(self):
        with TemporaryDirectory() as tmp:
            server.write_workflow_config(self.STEPS, tmp, self.EDGES)
            cfg = server.read_workflow_config(tmp)
            back = server._workflow_graph(cfg)
            self.assertEqual("plan", server._root_goal_decompose_step_id(cfg, back))
        with TemporaryDirectory() as tmp:  # no flag -> entry step (back-compat)
            server.write_workflow_config(LINEAR_STEPS, tmp, LINEAR_EDGES)
            cfg = server.read_workflow_config(tmp)
            back = server._workflow_graph(cfg)
            self.assertEqual("intake", server._root_goal_decompose_step_id(cfg, back))

    def test_subtasks_start_at_decompose_successor(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, steps=self.STEPS, edges=self.EDGES, team=self.TEAM)
            goal_id = h.create_task(title="Big goal")
            h.store.update_task_metadata(goal_id, is_goal=True)
            h.start(goal_id)  # dispatches the goal's own intake
            plan_step = next(
                s for s in server.read_workflow_config(tmp)["steps"] if s["id"] == "plan"
            )
            report = server._complete_goal_intake_locked(
                h.store, tmp, h.store.get_task(goal_id), plan_step, "hub-agent",
                '{"tasks":[{"title":"A","content":"do A"},{"title":"B","content":"do B"}]}',
            )
            self.assertEqual(2, len(report["created_subtasks"]))
            # Each subtask begins at implement (plan's successor) — not intake/design.
            for item in report["started"]:
                self.assertEqual(
                    [{"step": "implement", "assignee": "codex"}], item["dispatched"]
                )
            # The goal itself has left the step flow.
            self.assertEqual("", h.task(goal_id)["workflow_step"])

    def test_held_subtask_released_after_decompose_keeps_upstream_result(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, steps=self.STEPS, edges=self.EDGES, team=self.TEAM)
            goal_id = h.create_task(title="Big goal")
            h.store.update_task_metadata(goal_id, is_goal=True)
            h.start(goal_id)
            plan_step = next(
                s for s in server.read_workflow_config(tmp)["steps"] if s["id"] == "plan"
            )
            upstream = (
                '{"tasks":[{"title":"A","content":"do A"},'
                '{"title":"B","content":"do B","depends_on":[1]}],'
                '"design":"shared architecture"}'
            )
            server._complete_goal_intake_locked(
                h.store, tmp, h.store.get_task(goal_id), plan_step, "hub-agent",
                upstream,
            )
            business = {
                t["title"]: t for t in h.store.list_tasks(status="all")
                if t.get("parent_task_id") == goal_id
                and t.get("source_message_id") is not None
            }
            a, b = business["A"], business["B"]
            self.assertTrue(h.task(a["id"])["workflow_step"])
            self.assertFalse(h.task(b["id"])["workflow_step"])

            h.store.set_task_workflow_state(a["id"], task_status="closed")
            server._recompute_parent_goal_status(h.store, h.task(a["id"]), tmp)

            b = h.task(b["id"])
            self.assertEqual("implement", b["workflow_step"])
            transitions = h.store.list_task_transitions(b["id"])
            self.assertTrue(
                any(t["from_step"] == "plan" and t["note"] == upstream for t in transitions)
            )
            jobs = [
                job for job in h.store.list_run_jobs("all")
                if job["task_id"] == b["id"] and job["step"] == "implement"
            ]
            self.assertEqual(upstream, jobs[-1]["upstream_result"])

    def test_decompose_warnings(self):
        two = [dict(s, decompose=True) for s in self.STEPS if s["id"] in ("plan", "design")]
        rest = [s for s in self.STEPS if s["id"] not in ("plan", "design")]
        warns = server._workflow_graph_warnings(two + rest, self.EDGES)
        self.assertTrue(any("multiple decompose steps" in w for w in warns), warns)
        # decompose step with no outgoing edge
        warns2 = server._workflow_graph_warnings(
            self.STEPS, [e for e in self.EDGES if e["from"] != "plan"]
        )
        self.assertTrue(any("no outgoing step" in w for w in warns2), warns2)


if __name__ == "__main__":
    unittest.main()
