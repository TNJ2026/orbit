from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest

from orbit.store import (
    InvalidInputError,
    Store,
    UnknownAgentError,
    project_db_path,
)


class StoreTests(unittest.TestCase):
    def make_store(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        return Store(Path(self.tmp.name) / "messages.db")

    def register_pair(self, store):
        store.register_agent("a", "agent a")
        store.register_agent("b", "agent b")

    def test_send_message_requires_registered_sender_and_recipient(self):
        store = self.make_store()
        store.register_agent("a", "agent a")

        with self.assertRaises(UnknownAgentError):
            store.send_message("missing", "a", "hello")

        with self.assertRaises(UnknownAgentError):
            store.send_message("a", "missing", "hello")

    def test_fetch_leases_until_ack(self):
        store = self.make_store()
        self.register_pair(store)
        [message_id] = store.send_message("a", "b", "hello")

        first = store.fetch_unread("b", lease_seconds=60)
        self.assertEqual([message_id], [m["id"] for m in first])
        self.assertEqual(1, first[0]["delivery_count"])
        self.assertIn("lease_expires_at", first[0])
        self.assertIn("lease_token", first[0])

        self.assertEqual([], store.fetch_unread("b", lease_seconds=60))
        self.assertTrue(store.ack_message("b", message_id, first[0]["lease_token"]))
        self.assertEqual([], store.fetch_unread("b", lease_seconds=60))

    def test_ack_requires_current_lease_token(self):
        store = self.make_store()
        self.register_pair(store)
        [message_id] = store.send_message("a", "b", "hello")

        first = store.fetch_unread("b", lease_seconds=1)
        self.assertFalse(store.ack_message("b", message_id, "wrong-token"))

        time.sleep(1.1)
        second = store.fetch_unread("b", lease_seconds=60)
        self.assertNotEqual(first[0]["lease_token"], second[0]["lease_token"])
        self.assertFalse(store.ack_message("b", message_id, first[0]["lease_token"]))
        self.assertTrue(store.ack_message("b", message_id, second[0]["lease_token"]))

    def test_unacked_message_is_redelivered_after_lease_expiry(self):
        store = self.make_store()
        self.register_pair(store)
        [message_id] = store.send_message("a", "b", "hello")

        first = store.fetch_unread("b", lease_seconds=1)
        self.assertEqual(message_id, first[0]["id"])

        time.sleep(1.1)
        second = store.fetch_unread("b", lease_seconds=1)
        self.assertEqual(message_id, second[0]["id"])
        self.assertEqual(2, second[0]["delivery_count"])

    def test_get_thread_uses_ancestors_and_descendants(self):
        store = self.make_store()
        self.register_pair(store)
        [root] = store.send_message("a", "b", "root")
        [reply] = store.send_message("b", "a", "reply", reply_to=root)
        [follow_up] = store.send_message("a", "b", "follow up", reply_to=reply)

        thread = store.get_thread(reply)
        self.assertEqual([root, reply, follow_up], [m["id"] for m in thread])

    def test_list_messages_reports_ui_status_without_leasing(self):
        store = self.make_store()
        self.register_pair(store)
        [message_id] = store.send_message("a", "b", "hello")

        available = store.list_messages(agent="b", status="available")
        self.assertEqual([message_id], [m["id"] for m in available])
        self.assertEqual("available", available[0]["status"])

        [leased] = store.fetch_unread("b", lease_seconds=60)
        leased_messages = store.list_messages(agent="b", status="leased")
        self.assertEqual([message_id], [m["id"] for m in leased_messages])
        self.assertEqual("leased", leased_messages[0]["status"])

        store.ack_message("b", message_id, leased["lease_token"])
        read = store.list_messages(agent="b", status="read")
        self.assertEqual([message_id], [m["id"] for m in read])
        self.assertEqual("read", read[0]["status"])

    def test_task_metadata_can_be_filtered_and_updated(self):
        store = self.make_store()
        self.register_pair(store)
        [task_id] = store.send_message(
            "a",
            "b",
            "review auth changes",
            kind="task",
            title="Review auth flow",
            task_status="assigned",
        )
        store.send_message("a", "b", "plain message")

        tasks = store.list_messages(agent="b", kind="task")
        self.assertEqual([task_id], [m["id"] for m in tasks])
        self.assertEqual("Review auth flow", tasks[0]["title"])
        self.assertEqual("assigned", tasks[0]["task_status"])

        self.assertTrue(store.update_task_status(task_id, "accepted"))
        accepted = store.list_messages(agent="b", kind="task", task_status="accepted")
        self.assertEqual([task_id], [m["id"] for m in accepted])

    def test_task_message_creates_executable_task(self):
        store = self.make_store()
        self.register_pair(store)
        [message_id] = store.send_message(
            "a",
            "b",
            "review auth changes",
            kind="task",
            title="Review auth flow",
        )

        [task] = store.list_tasks()
        self.assertEqual(message_id, task["source_message_id"])
        self.assertEqual("Review auth flow", task["title"])
        self.assertEqual("review auth changes", task["content"])
        self.assertEqual("a", task["sender"])
        self.assertEqual("b", task["assignee"])
        self.assertEqual("b", task["recipient"])
        self.assertEqual("created", task["task_status"])
        self.assertEqual("implementer", task["role_required"])
        self.assertEqual("normal", task["importance"])
        self.assertEqual("medium", task["size"])
        self.assertEqual("medium", task["risk"])
        self.assertEqual([], task["required_capabilities"])
        self.assertTrue(task["exclusive_workspace"])

    def test_task_metadata_can_be_updated_for_assignment(self):
        store = self.make_store()
        self.register_pair(store)
        store.send_message("a", "b", "migrate schema", kind="task")
        [task] = store.list_tasks()

        updated = store.update_task_metadata(
            task["id"],
            role_required="implementer",
            importance="critical",
            size="large",
            risk="high",
            required_capabilities=["python", "sqlite"],
            exclusive_workspace=False,
        )

        self.assertEqual("critical", updated["importance"])
        self.assertEqual("large", updated["size"])
        self.assertEqual("high", updated["risk"])
        self.assertEqual(["python", "sqlite"], updated["required_capabilities"])
        self.assertFalse(updated["exclusive_workspace"])

    def test_task_metadata_rejects_off_list_values(self):
        store = self.make_store()
        self.register_pair(store)
        store.send_message("a", "b", "migrate schema", kind="task")
        [task] = store.list_tasks()

        for field, value in (
            ("importance", "urgent"),
            ("size", "huge"),
            ("risk", "extreme"),
        ):
            with self.assertRaisesRegex(InvalidInputError, f"invalid {field}"):
                store.update_task_metadata(task["id"], **{field: value})
        with self.assertRaisesRegex(InvalidInputError, "role_required"):
            store.update_task_metadata(task["id"], role_required="  ")
        # valid values still pass untouched
        updated = store.update_task_metadata(task["id"], importance="high")
        self.assertEqual("high", updated["importance"])

    def test_reap_stale_runs_frees_worker_capacity(self):
        store = self.make_store()
        self.register_pair(store)
        store.send_message("a", "b", "task", kind="task")
        [task] = store.list_tasks()
        run = store.create_task_run(task["id"], worker="claude-code")
        self.assertEqual({"claude-code": 1}, store.active_task_counts())

        reaped = store.reap_stale_runs()

        self.assertEqual(1, reaped)
        self.assertEqual({}, store.active_task_counts())
        self.assertEqual("orphaned", store.get_task_run(run["id"])["status"])
        self.assertEqual(0, store.reap_stale_runs())

    def test_task_engine_status_syncs_back_to_source_message(self):
        store = self.make_store()
        self.register_pair(store)
        [message_id] = store.send_message("a", "b", "run tests", kind="task")
        [task] = store.list_tasks()

        self.assertTrue(store.update_task_item_status(task["id"], "testing"))
        self.assertEqual("testing", store.list_tasks()[0]["task_status"])

    def test_goal_queries_return_only_goals_and_direct_children(self):
        store = self.make_store()
        self.register_pair(store)
        store.send_message("a", "b", "goal", kind="task", title="Goal")
        goal = store.list_tasks()[0]
        store.update_task_metadata(goal["id"], is_goal=True)
        store.create_step_card(
            goal["id"], "intake", "Intake", "do intake", "a", "b", "created"
        )
        store.send_message("a", "b", "unrelated", kind="task", title="Other")

        tasks = store.list_goals_with_children()

        self.assertEqual(
            {"Goal", "Intake"},
            {task["title"] for task in tasks},
        )

    def test_active_workflow_tasks_excludes_blocked_and_closed(self):
        store = self.make_store()
        self.register_pair(store)
        ids = []
        for status in ("in_progress", "blocked", "closed"):
            store.send_message("a", "b", status, kind="task", title=status)
            task = store.list_tasks()[0]
            store.set_task_workflow_state(
                task["id"], workflow_step="implement", task_status=status
            )
            ids.append(task["id"])

        tasks = store.list_active_workflow_tasks()

        self.assertEqual([ids[0]], [task["id"] for task in tasks])

    def test_message_task_status_syncs_to_task_engine(self):
        store = self.make_store()
        self.register_pair(store)
        [message_id] = store.send_message("a", "b", "fix lint", kind="task")

        self.assertTrue(store.update_task_status(message_id, "testing"))
        [task] = store.list_tasks(status="testing", assignee="b")
        self.assertEqual(message_id, task["source_message_id"])
        self.assertEqual("testing", task["task_status"])

    def test_task_runs_track_execution_attempts(self):
        store = self.make_store()
        self.register_pair(store)
        store.send_message("a", "b", "run test suite", kind="task")
        [task] = store.list_tasks()

        first = store.create_task_run(task["id"], "/tmp/task-1/run-001", "codex")
        second = store.create_task_run(task["id"], "/tmp/task-1/run-002", "codex")

        self.assertEqual(1, first["attempt"])
        self.assertEqual(2, second["attempt"])
        self.assertEqual("running", first["status"])
        self.assertEqual("/tmp/task-1/run-001", first["log_dir"])
        self.assertEqual([2, 1], [run["attempt"] for run in store.list_task_runs(task["id"])])

        finished = store.finish_task_run(first["id"], "failed", 1)
        self.assertEqual("failed", finished["status"])
        self.assertEqual(1, finished["exit_code"])
        self.assertIsNotNone(finished["finished_at"])

    def test_run_cancel_reason_persists_and_finish_clears_flag(self):
        store = self.make_store()
        self.register_pair(store)
        store.send_message("a", "b", "run test suite", kind="task")
        [task] = store.list_tasks()
        run = store.create_task_run(task["id"], worker="codex")

        self.assertTrue(store.request_run_kill(run["id"], "hub says stuck"))
        requested = store.get_task_run(run["id"])
        self.assertEqual(1, requested["cancel_requested"])
        self.assertEqual("hub says stuck", requested["cancel_reason"])
        self.assertTrue(store.run_cancel_requested(run["id"]))

        finished = store.finish_task_run(run["id"], "failed", 137)
        self.assertEqual(0, finished["cancel_requested"])
        self.assertEqual("hub says stuck", finished["cancel_reason"])
        self.assertFalse(store.run_cancel_requested(run["id"]))

    def test_step_card_display_id_tied_to_parent(self):
        store = self.make_store()
        self.register_pair(store)
        store.send_message("a", "b", "big task", kind="task")
        [parent] = store.list_tasks()
        c1 = store.create_step_card(parent["id"], "intake", "Intake", "x", "workflow", "b", "created")
        c2 = store.create_step_card(parent["id"], "implement", "Impl", "x", "workflow", "b", "in_progress")
        self.assertEqual(f"{parent['id']}.1", c1["display_id"])
        self.assertEqual(f"{parent['id']}.2", c2["display_id"])

    def test_task_run_log_dir_can_be_set_after_attempt_is_reserved(self):
        store = self.make_store()
        self.register_pair(store)
        store.send_message("a", "b", "collect logs", kind="task")
        [task] = store.list_tasks()

        run = store.create_task_run(task["id"], worker="tester")
        updated = store.update_task_run_log_dir(run["id"], "/tmp/task-1/run-001")

        self.assertEqual("/tmp/task-1/run-001", updated["log_dir"])
        self.assertEqual("tester", updated["worker"])

    def test_active_task_counts_tracks_running_workers(self):
        store = self.make_store()
        self.register_pair(store)
        store.send_message("a", "b", "run test suite", kind="task")
        [task] = store.list_tasks()
        running = store.create_task_run(task["id"], worker="codex")
        done = store.create_task_run(task["id"], worker="codex")
        store.finish_task_run(done["id"], "completed", 0)

        self.assertEqual({"codex": 1}, store.active_task_counts())

    def test_task_metadata_is_returned_from_inbox_and_thread(self):
        store = self.make_store()
        self.register_pair(store)
        [task_id] = store.send_message(
            "a",
            "b",
            "implement search",
            kind="task",
            title="Implement search",
        )

        [inbox_task] = store.fetch_unread("b", lease_seconds=60)
        self.assertEqual(task_id, inbox_task["id"])
        self.assertEqual("task", inbox_task["kind"])
        self.assertEqual("Implement search", inbox_task["title"])
        self.assertEqual("created", inbox_task["task_status"])

        thread = store.get_thread(task_id)
        self.assertEqual("task", thread[0]["kind"])
        self.assertEqual("created", thread[0]["task_status"])

    def test_existing_old_schema_database_migrates_before_index_creation(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "messages.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE agents (
                    name          TEXT PRIMARY KEY,
                    description   TEXT NOT NULL DEFAULT '',
                    registered_at TEXT NOT NULL,
                    last_seen     TEXT NOT NULL
                );
                CREATE TABLE messages (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender     TEXT NOT NULL,
                    recipient  TEXT NOT NULL,
                    content    TEXT NOT NULL,
                    reply_to   INTEGER,
                    created_at TEXT NOT NULL,
                    read_at    TEXT
                );
                CREATE INDEX idx_messages_unread
                    ON messages (recipient, read_at);
                CREATE INDEX idx_messages_reply_to
                    ON messages (reply_to);
                """
            )
            conn.close()

            store = Store(db_path)
            columns = {
                row["name"]
                for row in store._conn.execute("PRAGMA table_info(messages)").fetchall()
            }
            self.assertIn("leased_until", columns)
            self.assertIn("task_status", columns)
            store.close()

    def test_old_task_messages_are_backfilled_into_task_engine(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "messages.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE agents (
                    name          TEXT PRIMARY KEY,
                    description   TEXT NOT NULL DEFAULT '',
                    registered_at TEXT NOT NULL,
                    last_seen     TEXT NOT NULL
                );
                CREATE TABLE messages (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender     TEXT NOT NULL,
                    recipient  TEXT NOT NULL,
                    content    TEXT NOT NULL,
                    kind       TEXT NOT NULL DEFAULT 'message',
                    title      TEXT NOT NULL DEFAULT '',
                    task_status TEXT NOT NULL DEFAULT '',
                    reply_to   INTEGER,
                    created_at TEXT NOT NULL,
                    read_at    TEXT,
                    leased_until TEXT,
                    lease_owner TEXT,
                    lease_token TEXT,
                    delivery_count INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO agents (name, description, registered_at, last_seen)
                VALUES ('a', 'agent a', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
                       ('b', 'agent b', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
                INSERT INTO messages (sender, recipient, content, kind, title, task_status, created_at)
                VALUES ('a', 'b', 'legacy task', 'task', 'Legacy Task', 'assigned', '2026-01-01T00:00:00+00:00'),
                       ('a', 'b', 'plain message', 'message', '', '', '2026-01-01T00:00:00+00:00');
                """
            )
            conn.close()

            store = Store(db_path)
            tasks = store.list_tasks()

            self.assertEqual(1, len(tasks))
            self.assertEqual("Legacy Task", tasks[0]["title"])
            self.assertEqual("legacy task", tasks[0]["content"])
            self.assertEqual("assigned", tasks[0]["task_status"])
            store.close()

    def test_project_db_path_is_stable_and_project_scoped(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_a = root / "same-name"
            project_b = root / "nested" / "same-name"
            project_a.mkdir()
            project_b.mkdir(parents=True)
            base_dir = root / "dbs"

            first = project_db_path(project_a, base_dir=base_dir)
            second = project_db_path(project_a, base_dir=base_dir)
            other = project_db_path(project_b, base_dir=base_dir)

            self.assertEqual(first, second)
            self.assertNotEqual(first, other)
            self.assertEqual("messages.db", first.name)
            self.assertEqual(base_dir, first.parent.parent)

    def test_store_exposes_database_path_for_status_api(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "messages.db"
            store = Store(db_path)
            self.assertEqual(db_path, store.db_path)
            store.close()

    def test_invalid_task_status_is_rejected_not_cleaned(self):
        store = self.make_store()
        self.register_pair(store)
        # custom free-form labels (<=12 chars) are allowed — they come from
        # free-form workflow step statuses landing on task rows via dispatch
        [ok_id] = store.send_message(
            "a", "b", "x", kind="task", task_status="urgent"
        )
        self.assertEqual("urgent", store.get_message(ok_id)["task_status"])
        # over the cap -> rejected
        with self.assertRaises(InvalidInputError):
            store.send_message(
                "a", "b", "x", kind="task", task_status="a" * 13
            )
        [task_id] = store.send_message(
            "a", "b", "x", kind="task", task_status="assigned"
        )
        with self.assertRaises(InvalidInputError):
            store.update_task_status(task_id, "a" * 13)
        with self.assertRaises(InvalidInputError):
            store.update_task_status(task_id, "")
        # status untouched by the failed updates
        self.assertEqual("assigned", store.get_message(task_id)["task_status"])

    def test_invalid_kind_is_rejected(self):
        store = self.make_store()
        self.register_pair(store)
        with self.assertRaises(InvalidInputError):
            store.send_message("a", "b", "x", kind="todo")

    def test_reserved_and_empty_agent_names_are_rejected(self):
        store = self.make_store()
        with self.assertRaises(InvalidInputError):
            store.register_agent("*", "broadcast impostor")
        with self.assertRaises(InvalidInputError):
            store.register_agent("   ", "blank")
        # name is stripped before storage
        store.register_agent("  a  ", "agent a")
        self.assertEqual(["a"], store.agent_names())

    def test_project_db_path_resolves_project_root_from_subdirectory(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            sub = root / "src" / "pkg"
            sub.mkdir(parents=True)
            (root / ".git").mkdir()
            base_dir = Path(tmp) / "dbs"

            import os

            cwd = os.getcwd()
            try:
                os.chdir(sub)
                from_sub = project_db_path(base_dir=base_dir)
                os.chdir(root)
                from_root = project_db_path(base_dir=base_dir)
            finally:
                os.chdir(cwd)
            self.assertEqual(from_root, from_sub)


if __name__ == "__main__":
    unittest.main()
