"""Subflow nodes and cross-run lineage: a subflow node spawns a child task
that traverses its own named graph (workflow.json `subflows`), the parent node
waits for the child's terminal state, and workflow_runs records the
parent/child lineage (parent_run_id / parent_node_run_id / root_run_id) for
both subflow children and decompose subtasks."""

import json
import unittest
from tempfile import TemporaryDirectory
from unittest import mock

import orbit.server as server
from orbit.store import InvalidInputError
from orbit.workflow_config import workflow_config_for_task
from test_workflow_engine import ENTRY_DECOMPOSE_STEPS, EngineHarness

_spawn_patcher = None


def setUpModule():
    global _spawn_patcher
    _spawn_patcher = mock.patch.object(server, "_spawn_step_worker")
    _spawn_patcher.start()


def tearDownModule():
    _spawn_patcher.stop()


MAIN_STEPS = [
    {"id": "a", "name": "A", "agents": ["codex"], "required": True},
    {"id": "sub", "name": "Sub", "type": "subflow", "subflow": "inner"},
    {"id": "b", "name": "B", "agents": ["codex"], "required": True},
]
MAIN_EDGES = [
    {"from": "a", "to": "sub"},
    {"from": "sub", "to": "b"},
]
INNER_SUBFLOW = {
    "inner": {
        "steps": [
            {"id": "s1", "name": "S1", "agents": ["codex"]},
            {"id": "s2", "name": "S2", "agents": ["rev"]},
        ],
        "edges": [{"from": "s1", "to": "s2"}],
    }
}


def _subflow_harness(tmp, subflows=INNER_SUBFLOW):
    # Write the subflows first: the harness re-saves the main graph without a
    # subflows argument, which preserves the existing map (and a main graph
    # referencing 'inner' would not save at all before the map exists).
    server.write_workflow_config(MAIN_STEPS, tmp, MAIN_EDGES, subflows)
    return EngineHarness(tmp, steps=MAIN_STEPS, edges=MAIN_EDGES)


class SubflowConfigTests(unittest.TestCase):
    def test_subflows_are_normalized_and_survive_main_graph_saves(self):
        with TemporaryDirectory() as tmp:
            written = server.write_workflow_config(
                MAIN_STEPS, tmp, MAIN_EDGES, INNER_SUBFLOW
            )
            self.assertEqual({"inner"}, set(written["subflows"]))
            inner = written["subflows"]["inner"]
            self.assertEqual(["s1", "s2"], [s["id"] for s in inner["steps"]])
            self.assertEqual("subflow", written["steps"][1]["type"])
            self.assertEqual("inner", written["steps"][1]["subflow"])
            self.assertEqual(["success"], written["steps"][1]["ports"])
            # A canvas-style save without subflows must not drop them.
            server.write_workflow_config(MAIN_STEPS, tmp, MAIN_EDGES)
            cfg = server.read_workflow_config(tmp)
            self.assertEqual({"inner"}, set(cfg["subflows"]))

    def test_unknown_subflow_reference_is_rejected(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(InvalidInputError, "unknown subflow"):
                server.write_workflow_config(MAIN_STEPS, tmp, MAIN_EDGES, {})

    def test_subflow_node_requires_a_name(self):
        steps = [dict(s) for s in MAIN_STEPS]
        steps[1] = {"id": "sub", "name": "Sub", "type": "subflow"}
        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(InvalidInputError, "requires a 'subflow' name"):
                server.write_workflow_config(steps, tmp, MAIN_EDGES, INNER_SUBFLOW)

    def test_nested_subflow_node_is_rejected(self):
        nested = {
            "inner": {
                "steps": [
                    {"id": "s1", "name": "S1", "agents": ["codex"]},
                    {"id": "s2", "name": "S2", "type": "subflow", "subflow": "other"},
                ],
                "edges": [{"from": "s1", "to": "s2"}],
            }
        }
        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(InvalidInputError, "nested"):
                server.write_workflow_config(MAIN_STEPS, tmp, MAIN_EDGES, nested)

    def test_decompose_step_in_subflow_is_rejected(self):
        with_decompose = {
            "inner": {
                "steps": [
                    {"id": "s1", "name": "S1", "agents": ["codex"], "decompose": True},
                    {"id": "s2", "name": "S2", "agents": ["codex"]},
                ],
                "edges": [{"from": "s1", "to": "s2"}],
            }
        }
        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(InvalidInputError, "decompose"):
                server.write_workflow_config(
                    MAIN_STEPS, tmp, MAIN_EDGES, with_decompose
                )

    def test_subflow_graph_must_be_executable(self):
        # Two required steps with no connecting edge: the second required step
        # is unreachable from the subflow's entry.
        broken = {
            "inner": {
                "steps": [
                    {"id": "s1", "name": "S1", "agents": ["codex"], "required": True},
                    {"id": "s2", "name": "S2", "agents": ["codex"], "required": True},
                ],
                "edges": [],
            }
        }
        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(InvalidInputError, "not executable"):
                server.write_workflow_config(MAIN_STEPS, tmp, MAIN_EDGES, broken)

    def test_workflow_config_for_task_resolves_ref(self):
        with TemporaryDirectory() as tmp:
            server.write_workflow_config(MAIN_STEPS, tmp, MAIN_EDGES, INNER_SUBFLOW)
            main = workflow_config_for_task(tmp, {"workflow_ref": ""})
            self.assertEqual(["a", "sub", "b"], [s["id"] for s in main["steps"]])
            sub = workflow_config_for_task(tmp, {"workflow_ref": "inner"})
            self.assertEqual(["s1", "s2"], [s["id"] for s in sub["steps"]])
            self.assertEqual("inner", sub["subflow"])
            self.assertEqual({}, sub["subflows"])
            with self.assertRaisesRegex(InvalidInputError, "unknown workflow subflow"):
                workflow_config_for_task(tmp, {"workflow_ref": "missing"})

    def test_goal_preflight_validates_subflow_agents(self):
        no_agents = {
            "inner": {
                "steps": [{"id": "s1", "name": "S1"}],
                "edges": [],
            }
        }
        with TemporaryDirectory() as tmp:
            h = _subflow_harness(tmp, subflows=no_agents)
            with self.assertRaisesRegex(InvalidInputError, "sub/s1"):
                server._validate_goal_auto_runners(h.store, tmp, "t", "c")


class SubflowExecutionTests(unittest.TestCase):
    def _child_of(self, h, parent_id):
        children = [
            t for t in h.store.list_tasks_by_parent(parent_id)
            if t.get("workflow_ref") == "inner"
        ]
        self.assertEqual(1, len(children))
        return children[0]

    def test_full_flow_traverses_subflow_and_records_lineage(self):
        with TemporaryDirectory() as tmp:
            h = _subflow_harness(tmp)
            tid = h.create_task()
            h.start(tid)
            h.complete("codex", tid, "a", "done", "A out")

            # The subflow node spawned a child task routed by the inner graph.
            child = self._child_of(h, tid)
            self.assertEqual("inner", child["workflow_ref"])
            self.assertIsNone(child["source_message_id"])
            self.assertIn("s1", h.task(child["id"])["workflow_step"])

            h.complete("codex", child["id"], "s1", "done", "s1 out")
            h.complete(
                "rev", child["id"], "s2", "done",
                'WORKFLOW_RESULT: {"port":"success","output":{"x":1},'
                '"summary":"inner done","artifacts":[]}',
            )

            # Child terminal -> child closed, parent's subflow node completed
            # and the next main step dispatched.
            child = h.task(child["id"])
            self.assertEqual("closed", child["task_status"])
            transitions = h.store.list_task_transitions(tid)
            self.assertIn(
                ("sub", "b", "done"),
                [(t["from_step"], t["to_step"], t["outcome"]) for t in transitions],
            )
            h.complete("codex", tid, "b", "done", "B out")
            self.assertEqual("closed", h.task(tid)["task_status"])

            # Lineage: parent run roots the tree; the child run hangs off the
            # parent run and the parent's 'sub' node execution.
            parent_run = h.store.get_workflow_run_by_task(tid)
            child_run = h.store.get_workflow_run_by_task(child["id"])
            self.assertIsNone(parent_run["parent_run_id"])
            self.assertEqual(parent_run["id"], parent_run["root_run_id"])
            self.assertEqual(parent_run["id"], child_run["parent_run_id"])
            self.assertEqual(parent_run["id"], child_run["root_run_id"])
            sub_node = h.store.latest_node_run(parent_run["id"], "sub")
            self.assertEqual(sub_node["id"], child_run["parent_node_run_id"])
            self.assertEqual("succeeded", sub_node["status"])
            self.assertEqual("succeeded", parent_run["status"])
            self.assertEqual("succeeded", child_run["status"])

            lineage = h.store.list_workflow_run_lineage(parent_run["root_run_id"])
            self.assertEqual(
                [parent_run["id"], child_run["id"]],
                [run["id"] for run in lineage],
            )
            # The subflow's structured output reached the parent node result.
            [sub_result] = h.store.list_workflow_node_results(tid, "sub")
            self.assertEqual({"x": 1}, sub_result["output"])

    def test_blocked_child_blocks_parent_subflow_node(self):
        with TemporaryDirectory() as tmp:
            h = _subflow_harness(tmp)
            tid = h.create_task()
            h.start(tid)
            h.complete("codex", tid, "a", "done")
            child = self._child_of(h, tid)

            h.complete("codex", child["id"], "s1", "blocked", "stuck")

            self.assertEqual("blocked", h.task(child["id"])["task_status"])
            self.assertEqual("blocked", h.task(tid)["task_status"])
            transitions = h.store.list_task_transitions(tid)
            self.assertIn(
                ("sub", "sub", "blocked"),
                [(t["from_step"], t["to_step"], t["outcome"]) for t in transitions],
            )
            self.assertEqual(
                "blocked", h.store.get_workflow_run_by_task(child["id"])["status"]
            )

    def test_force_close_parent_cascades_to_subflow_child(self):
        with TemporaryDirectory() as tmp:
            h = _subflow_harness(tmp)
            tid = h.create_task()
            h.start(tid)
            h.complete("codex", tid, "a", "done")
            child = self._child_of(h, tid)

            server.force_close_goal(h.store, tmp, tid)

            self.assertEqual("closed", h.task(tid)["task_status"])
            self.assertEqual("closed", h.task(child["id"])["task_status"])
            self.assertEqual(
                "cancelled", h.store.get_workflow_run_by_task(tid)["status"]
            )
            self.assertEqual(
                "cancelled", h.store.get_workflow_run_by_task(child["id"])["status"]
            )
            # A straggler child runner finishing later must not reopen anything.
            report = h.complete("codex", child["id"], "s1", "done", "late")
            self.assertTrue(report.get("closed"))
            self.assertEqual("closed", h.task(tid)["task_status"])


class DecomposeLineageTests(unittest.TestCase):
    def test_decompose_subtask_runs_hang_off_the_goal_run(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(tmp, steps=ENTRY_DECOMPOSE_STEPS, bindings=[
                {"agent_name": "hub-agent", "assignment": "hub", "runner_command": "cat"},
                {"agent_name": "codex", "assignment": "implementer", "runner_command": "cat"},
                {"agent_name": "rev", "assignment": "reviewer", "runner_command": "cat"},
            ])
            goal_id = h.create_task(title="goal")
            h.store.update_task_metadata(goal_id, is_goal=True)
            h.start(goal_id)

            goal = h.task(goal_id)
            step = next(
                s for s in server.read_workflow_config(tmp)["steps"]
                if s["id"] == "intake"
            )
            server._complete_goal_intake_locked(
                h.store, tmp, goal, step, "hub-agent",
                json.dumps({
                    "tasks": [
                        {"title": "A", "content": "a"},
                        {"title": "B", "content": "b", "depends_on": [1]},
                    ]
                }),
            )

            goal_run = h.store.get_workflow_run_by_task(goal_id)
            intake_node = h.store.latest_node_run(goal_run["id"], "intake")
            subtasks = [
                t for t in h.store.list_tasks_by_parent(goal_id)
                if t.get("source_message_id") is not None
            ]
            self.assertEqual(2, len(subtasks))
            started = next(t for t in subtasks if t["title"] == "A")
            held = next(t for t in subtasks if t["title"] == "B")

            run_a = h.store.get_workflow_run_by_task(started["id"])
            self.assertEqual(goal_run["id"], run_a["parent_run_id"])
            self.assertEqual(intake_node["id"], run_a["parent_node_run_id"])
            self.assertEqual(goal_run["id"], run_a["root_run_id"])
            # Held subtasks have no run yet; releasing them records the same
            # lineage.
            self.assertIsNone(h.store.get_workflow_run_by_task(held["id"]))
            h.complete("hub-agent", started["id"], "intake", "done")
            h.complete("codex", started["id"], "implement", "done")
            h.complete("rev", started["id"], "review", "done")
            h.complete("hub-agent", started["id"], "accept", "done")
            run_b = h.store.get_workflow_run_by_task(held["id"])
            self.assertIsNotNone(run_b)
            self.assertEqual(goal_run["id"], run_b["parent_run_id"])
            self.assertEqual(intake_node["id"], run_b["parent_node_run_id"])
            self.assertEqual(goal_run["id"], run_b["root_run_id"])

            lineage = h.store.list_workflow_run_lineage(goal_run["id"])
            self.assertEqual(
                {goal_run["id"], run_a["id"], run_b["id"]},
                {run["id"] for run in lineage},
            )


if __name__ == "__main__":
    unittest.main()
