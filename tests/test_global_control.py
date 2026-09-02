from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from orbit.global_control import (
    WorkflowTemplateError, WorkflowTemplateStore,
    import_legacy_workflow_library,
)


SOURCE = json.dumps({
    "dsl_version": "1.0",
    "metadata": {"id": "workflow:shared", "name": "Shared"},
    "nodes": [], "edges": [],
})


class WorkflowTemplateStoreTests(unittest.TestCase):
    def test_a_template_is_global_source_not_a_published_version(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "templates.json"
            store = WorkflowTemplateStore(path)
            item = store.put(name="Shared", source=SOURCE)

            self.assertEqual("workflow:shared", item["workflow_id"])
            self.assertTrue(item["template_id"].startswith("template:"))
            self.assertEqual([item], WorkflowTemplateStore(path).list())
            self.assertNotIn("workspace_id", item)

    def test_dsl_id_is_normalized_to_the_runtime_workflow_id(self) -> None:
        source = json.dumps({
            "dsl_version": "1.0",
            "metadata": {"id": "plain-id", "name": "Plain"},
            "nodes": [], "edges": [],
        })
        with tempfile.TemporaryDirectory() as root:
            item = WorkflowTemplateStore(
                Path(root) / "templates.json"
            ).put(name="Plain", source=source)
        self.assertEqual("workflow:plain-id", item["workflow_id"])

    def test_invalid_source_never_enters_the_global_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = WorkflowTemplateStore(Path(root) / "templates.json")
            with self.assertRaisesRegex(WorkflowTemplateError, "metadata.id"):
                store.put(name="broken", source="{}")
            self.assertEqual([], store.list())

    def test_corrupt_catalog_fails_closed_instead_of_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "templates.json"
            path.write_text("{broken", encoding="utf-8")
            store = WorkflowTemplateStore(path)
            with self.assertRaisesRegex(WorkflowTemplateError, "corrupt"):
                store.put(name="Shared", source=SOURCE)
            self.assertEqual("{broken", path.read_text(encoding="utf-8"))

    def test_two_store_instances_do_not_lose_concurrent_updates(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "templates.json"
            sources = [json.dumps({
                "dsl_version": "1.0",
                "metadata": {"id": f"workflow:item-{index}", "name": str(index)},
                "nodes": [], "edges": [],
            }) for index in range(12)]
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(
                    lambda pair: WorkflowTemplateStore(path).put(
                        name=str(pair[0]), source=pair[1],
                        idempotency_key=f"create-{pair[0]}",
                    ),
                    enumerate(sources),
                ))
            self.assertEqual(12, len(WorkflowTemplateStore(path).list()))

    def test_delete_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = WorkflowTemplateStore(Path(root) / "templates.json")
            item = store.put(name="Shared", source=SOURCE)
            self.assertTrue(store.delete(item["template_id"]))
            self.assertFalse(store.delete(item["template_id"]))

    def test_create_idempotency_replays_and_rejects_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = WorkflowTemplateStore(Path(root) / "templates.json")
            first = store.put(
                name="Shared", source=SOURCE, idempotency_key="create-1",
            )
            replay = store.put(
                name="Shared", source=SOURCE, idempotency_key="create-1",
            )
            self.assertEqual(first, replay)
            with self.assertRaisesRegex(WorkflowTemplateError, "already used"):
                store.put(
                    name="Different", source=SOURCE, idempotency_key="create-1",
                )

    def test_delete_checks_template_version(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = WorkflowTemplateStore(Path(root) / "templates.json")
            item = store.put(name="Shared", source=SOURCE)
            with self.assertRaisesRegex(WorkflowTemplateError, "version conflict"):
                store.delete(item["template_id"], expected_version=2)
            self.assertTrue(store.delete(item["template_id"], expected_version=1))

    def test_delete_idempotency_replays_the_first_result(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = WorkflowTemplateStore(Path(root) / "templates.json")
            item = store.put(name="Shared", source=SOURCE)
            self.assertTrue(store.delete(
                item["template_id"], expected_version=1,
                idempotency_key="delete-1",
            ))
            self.assertTrue(store.delete(
                item["template_id"], expected_version=1,
                idempotency_key="delete-1",
            ))

    def test_legacy_published_source_becomes_one_idempotent_template(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            library = Path(root) / "library.db"
            connection = sqlite3.connect(library)
            connection.executescript("""
                CREATE TABLE workflow_definitions(
                    workflow_id TEXT PRIMARY KEY, name TEXT
                );
                CREATE TABLE workflow_versions(
                    workflow_id TEXT, version INTEGER, definition_hash TEXT,
                    source_format TEXT, source_text TEXT
                );
                CREATE TABLE archived_workflows(workflow_id TEXT PRIMARY KEY);
            """)
            connection.execute(
                "INSERT INTO workflow_definitions VALUES (?, ?)",
                ("workflow:shared", "Shared"),
            )
            connection.execute(
                "INSERT INTO workflow_versions VALUES (?, ?, ?, ?, ?)",
                ("workflow:shared", 1, "sha256:legacy", "json", SOURCE),
            )
            connection.execute(
                "INSERT INTO archived_workflows VALUES (?)", ("workflow:shared",),
            )
            connection.commit()
            connection.close()
            store = WorkflowTemplateStore(Path(root) / "templates.json")

            self.assertEqual(1, import_legacy_workflow_library(library, store))
            self.assertEqual(0, import_legacy_workflow_library(library, store))
            self.assertEqual("workflow:shared", store.list()[0]["workflow_id"])


if __name__ == "__main__":
    unittest.main()
