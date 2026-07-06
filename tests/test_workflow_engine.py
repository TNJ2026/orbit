"""Workflow constraint engine: dispatch, join, rework, blocking, skipping."""

import json
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import dev_loop.server as server
from dev_loop.store import InvalidInputError, Store

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
    {"id": "review", "name": "Review", "role_id": "reviewer", "task_status": "replied", "required": True},
    {"id": "accept", "name": "Accept", "role_id": "hub", "task_status": "accepted", "required": True},
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
            self.assertEqual("created", task["task_status"])
            self.assertEqual("intake", task["workflow_step"])

            done = h.complete("hub-agent", task_id, "intake", "done", "requirements clear")
            self.assertEqual([{"step": "implement", "assignee": "codex"}], done["dispatched"])
            task = h.task(task_id)
            self.assertEqual("codex", task["assignee"])
            self.assertEqual("in_progress", task["task_status"])

            # dispatch message carries the upstream result
            messages = h.inbox_senders("codex")
            self.assertTrue(any("requirements clear" in content for _, content in messages))

            h.complete("codex", task_id, "implement", "done", "diff ready")
            self.assertEqual("rev", h.task(task_id)["assignee"])

            # review rejects -> loop back to implement
            rework = h.complete("rev", task_id, "review", "rework", "tests missing")
            self.assertEqual([{"step": "implement", "assignee": "codex"}], rework["dispatched"])
            self.assertEqual("in_progress", h.task(task_id)["task_status"])

            h.complete("codex", task_id, "implement", "done", "tests added")
            h.complete("rev", task_id, "review", "done", "lgtm")
            self.assertEqual("accepted", h.task(task_id)["task_status"])

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
            {"id": "c", "name": "C", "role_id": "reviewer", "task_status": "replied", "required": True},
            {"id": "d", "name": "D", "role_id": "hub", "task_status": "accepted", "required": True},
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
            # Multiple active steps derive the visible task status from the
            # workflow configuration order instead of whichever dispatch ran last.
            self.assertEqual("in_progress", h.task(task_id)["task_status"])

            first = h.complete("codex", task_id, "b", "done")
            self.assertEqual([], first["dispatched"])
            self.assertTrue(any("waiting" in n for n in first["notices"]))

            second = h.complete("rev", task_id, "c", "done")
            self.assertEqual([{"step": "d", "assignee": "hub-agent"}], second["dispatched"])

    def test_optional_late_branch_does_not_redispatch_join_target(self):
        steps = [
            {"id": "a", "name": "A", "role_id": "hub", "task_status": "created", "required": True},
            {"id": "b", "name": "B", "role_id": "implementer", "task_status": "in_progress", "required": True},
            {"id": "c", "name": "C", "role_id": "tester", "task_status": "testing", "required": False},
            {"id": "d", "name": "D", "role_id": "reviewer", "task_status": "replied", "required": True},
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
            {"id": "test", "name": "Test", "role_id": "tester", "task_status": "testing", "required": False},
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
            {"id": "review", "name": "Review", "role_id": "reviewer", "task_status": "replied", "required": True},
            {"id": "optional_check", "name": "Optional Check", "role_id": "tester", "task_status": "testing", "required": False},
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
            {"id": "review", "name": "Review", "role_id": "reviewer", "task_status": "replied", "required": True},
            {"id": "optional_a", "name": "Optional A", "role_id": "tester", "task_status": "testing", "required": False},
            {"id": "optional_b", "name": "Optional B", "role_id": "tester", "task_status": "testing", "required": False},
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
            self.assertEqual("created", cards["intake"]["task_status"])
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
            self.assertEqual("blocked", h.task(goal_id)["task_status"])
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
            self.assertEqual("in_progress", cards["implement"]["task_status"])
            self.assertEqual("codex", cards["implement"]["assignee"])

            # blocked marks the card blocked; recovery closes it
            h.complete("codex", subtask["id"], "implement", "blocked", "need choice")
            self.assertEqual("blocked", self._cards(h, subtask["id"])["implement"]["task_status"])
            h.complete("hub-agent", subtask["id"], "implement", "done")
            cards = self._cards(h, subtask["id"])
            self.assertEqual("closed", cards["implement"]["task_status"])
            self.assertEqual("replied", cards["review"]["task_status"])

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

    def test_non_goal_tasks_get_no_step_cards(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp)
            task_id = h.create_task(title="plain")
            h.start(task_id)
            h.complete("hub-agent", task_id, "intake", "done")
            self.assertEqual({}, self._cards(h, task_id))


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
        cfg = __import__("dev_loop.server", fromlist=["server"]).read_workflow_config(h.root)
        return next(s for s in cfg["steps"] if s["id"] == "implement")

    def _member(self, h, name):
        import dev_loop.server as server
        members = server.read_team_config(h.root)["members"]
        return next(m for m in members if m["agent_name"] == name)

    def _run_event_types(self, run):
        path = Path(run["log_dir"]) / "events.jsonl"
        return [
            json.loads(line)["type"]
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

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

    def test_dispatch_spawns_worker_only_when_runner_command_exists(self):
        spawn = server._spawn_step_worker  # module-level mock
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, team=self._team_with_runner())
            task_id = h.create_task()
            spawn.reset_mock()
            h.start(task_id)  # intake -> hub-agent, no default runner
            self.assertEqual(0, spawn.call_count)
            h.complete("hub-agent", task_id, "intake", "done")  # -> codex, explicit runner
            self.assertEqual(1, spawn.call_count)
            self.assertEqual("implement", spawn.call_args.args[3]["id"])

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
            transitions = h.store.list_task_transitions(task_id)
            redispatched = [
                t["note"] for t in transitions
                if t["outcome"] == "dispatched" and t["to_step"] == "intake"
            ]
            self.assertIn("codex", redispatched)
            self.assertNotEqual("blocked", h.task(task_id)["task_status"])

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
            self.assertEqual("in_progress", h.task(goal_id)["task_status"])

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


if __name__ == "__main__":
    unittest.main()
