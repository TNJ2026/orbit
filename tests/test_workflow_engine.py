"""Workflow constraint engine: dispatch, join, rework, blocking, skipping."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import dev_loop.server as server
from dev_loop.store import InvalidInputError, Store


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

    def test_cycle_component_becomes_second_entry_and_runs(self):
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
            started = h.start(task_id)
            self.assertEqual(
                {"intake", "review"}, {d["step"] for d in started["dispatched"]}
            )

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


if __name__ == "__main__":
    unittest.main()
