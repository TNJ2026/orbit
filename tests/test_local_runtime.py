import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from orbit.local_runtime import LocalRuntime
from orbit.project_lock import ProjectAlreadyRunningError, ProjectProcessLock
from orbit.runner import scheduler_tick
from orbit.store import Store
from orbit.workflow_config import read_workflow_config, write_workflow_config
from orbit.workflow_engine import start_workflow_task


class LocalRuntimeTests(unittest.TestCase):
    def test_embedded_job_is_event_driven_and_applied_inline(self):
        with TemporaryDirectory() as tmp:
            write_workflow_config(
                [
                    {
                        "id": "implement",
                        "name": "Implement",
                        "agents": ["codex"],
                        "command": "printf 'RESULT_SUMMARY: ok\\nWORKFLOW_OUTCOME: done\\n'",
                        "required": True,
                    }
                ],
                tmp,
                [],
            )
            store = Store(Path(tmp) / "runtime.db")
            store.register_agent("hub", "hub")
            store.register_agent("codex", "worker")
            store.send_message("hub", "hub", "work", kind="task", title="work")
            task = store.list_tasks()[0]
            start_workflow_task(store, tmp, "hub", int(task["id"]))
            errors = []
            runtime = LocalRuntime(
                store,
                tmp,
                run_worker=True,
                worker_concurrency=2,
                scheduler_tick=scheduler_tick,
                maintenance=[],
                hub_sweep=(3600, lambda *_: None),
                goal_verify=(3600, lambda *_: None),
                record_error=lambda name, exc: errors.append((name, exc)),
            )
            runtime.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if store.get_task(int(task["id"]))["task_status"] == "closed":
                    break
                time.sleep(0.02)
            self.assertEqual("closed", store.get_task(int(task["id"]))["task_status"])
            self.assertEqual("done", store.list_run_jobs()[0]["status"])
            self.assertEqual([], errors)
            runtime.stop()

    def test_project_lock_rejects_second_server(self):
        with TemporaryDirectory() as tmp:
            with ProjectProcessLock(Path(tmp)):
                with self.assertRaises(ProjectAlreadyRunningError):
                    with ProjectProcessLock(Path(tmp)):
                        pass


class LocalOptimizationTests(unittest.TestCase):
    def test_workflow_cache_returns_copies_and_detects_external_change(self):
        with TemporaryDirectory() as tmp:
            write_workflow_config(
                [{"id": "a", "name": "A", "agents": ["codex"]}], tmp, []
            )
            first = read_workflow_config(tmp)
            first["steps"][0]["name"] = "mutated"
            self.assertEqual("A", read_workflow_config(tmp)["steps"][0]["name"])
            path = Path(tmp) / ".orbit" / "workflow.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["steps"][0]["name"] = "External"
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual("External", read_workflow_config(tmp)["steps"][0]["name"])

    def test_transition_batch_groups_rows(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "batch.db")
            store.register_agent("hub", "hub")
            for title in ("a", "b"):
                store.send_message("hub", "hub", title, kind="task", title=title)
            tasks = store.list_tasks(status="all")
            for task in tasks:
                store.record_task_transition(
                    int(task["id"]), "", "implement", "hub", "dispatched"
                )
            grouped = store.list_task_transitions_for_tasks(
                [int(task["id"]) for task in tasks]
            )
            self.assertEqual({int(task["id"]) for task in tasks}, set(grouped))
            self.assertTrue(all(len(rows) == 1 for rows in grouped.values()))
            store.close()


if __name__ == "__main__":
    unittest.main()
