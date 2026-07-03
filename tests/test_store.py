from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest

from dev_loop.store import (
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
        with self.assertRaises(InvalidInputError):
            store.send_message("a", "b", "x", kind="task", task_status="urgent")
        [task_id] = store.send_message(
            "a", "b", "x", kind="task", task_status="assigned"
        )
        with self.assertRaises(InvalidInputError):
            store.update_task_status(task_id, "done")
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
