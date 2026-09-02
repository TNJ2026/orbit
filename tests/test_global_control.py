from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from orbit.global_control import (
    WorkflowTemplateError, WorkflowTemplateStorageError, WorkflowTemplateStore,
)


SOURCE = json.dumps({
    "dsl_version": "1.0",
    "metadata": {"id": "shared", "name": "Shared"},
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

    def test_deleting_a_template_takes_its_source_with_it(self) -> None:
        """`list` said it was gone while the file still held every byte.

        The create receipt stores the whole item, source included, so a deleted
        template stayed readable to anyone with the file — and replayable into
        a resurrection by whoever still held that idempotency key.
        """

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "templates.json"
            store = WorkflowTemplateStore(path)
            item = store.put(name="Shared", source=SOURCE, idempotency_key="create-1")
            self.assertIn("dsl_version", path.read_text(encoding="utf-8"))

            self.assertTrue(store.delete(
                item["template_id"], expected_version=1, idempotency_key="delete-1",
            ))

            self.assertEqual([], store.list())
            self.assertNotIn("dsl_version", path.read_text(encoding="utf-8"))
            # The delete stays idempotent: that receipt is about something the
            # catalog agrees is gone.
            self.assertTrue(store.delete(
                item["template_id"], expected_version=1, idempotency_key="delete-1",
            ))

    def test_retrying_the_create_of_a_deleted_template_does_not_revive_it(self) -> None:
        """A key with no receipt is a key that was never used.

        Dropping the create receipt outright freed the idempotency key, so the
        client that never saw the first response — a timeout, a dropped
        connection — retried its create and was handed a new template_id, which
        put the deletion straight back. The receipt has to stay; what it must
        not keep is the source.
        """

        with tempfile.TemporaryDirectory() as root:
            store = WorkflowTemplateStore(Path(root) / "templates.json")
            created = store.put(
                name="Shared", source=SOURCE, idempotency_key="create-1",
            )
            store.delete(
                created["template_id"], expected_version=1,
                idempotency_key="delete-1",
            )

            replayed = store.put(
                name="Shared", source=SOURCE, idempotency_key="create-1",
            )

            self.assertEqual(created["template_id"], replayed["template_id"])
            self.assertTrue(replayed["deleted"])
            self.assertNotIn("source", replayed)
            self.assertEqual([], store.list())
            # A different key is a different request, and may create again.
            fresh = store.put(name="Shared", source=SOURCE, idempotency_key="create-2")
            self.assertNotEqual(created["template_id"], fresh["template_id"])
            self.assertEqual(1, len(store.list()))

    def test_an_already_prefixed_id_is_refused_rather_than_stored(self) -> None:
        """A template nobody can instantiate is worse than a rejected write.

        `metadata.id` is a bare DSL identifier and the compiler is what puts
        `workflow:` in front of it; the schema pattern forbids the colon. A
        source that arrives already prefixed therefore cannot compile anywhere,
        so storing it only defers the refusal to whoever tries to use it.
        """

        source = json.dumps({
            "dsl_version": "1.0",
            "metadata": {"id": "workflow:already", "name": "Already"},
            "nodes": [], "edges": [],
        })
        with tempfile.TemporaryDirectory() as root:
            store = WorkflowTemplateStore(Path(root) / "templates.json")
            with self.assertRaisesRegex(WorkflowTemplateError, "metadata.id"):
                store.put(name="Already", source=source)
            self.assertEqual([], store.list())

    @unittest.skipIf(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        "root is not refused by file permissions",
    )
    def test_a_catalog_that_cannot_be_written_is_storage_news(self) -> None:
        """The adapters answer 503 to this; a raw OSError became a traceback.

        Same reading as a catalog that cannot be parsed: the request was fine,
        the durable state is not available, and nothing was overwritten.
        """

        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "global"
            store = WorkflowTemplateStore(home / "templates.json")
            store.put(name="Shared", source=SOURCE)
            os.chmod(home, 0o500)
            try:
                with self.assertRaises(WorkflowTemplateStorageError):
                    store.put(name="Second", source=SOURCE, idempotency_key="second")
            finally:
                os.chmod(home, 0o700)

    def test_reading_an_empty_catalog_leaves_the_disk_alone(self) -> None:
        """A read is a read. It used to create the directory and a lock file."""

        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "global"
            store = WorkflowTemplateStore(home / "templates.json")

            self.assertEqual([], store.list())
            with self.assertRaises(WorkflowTemplateError):
                store.get("template:nothing")

            self.assertFalse(home.exists(), f"a read created {home}")

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
                "metadata": {"id": f"item-{index}", "name": str(index)},
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


if __name__ == "__main__":
    unittest.main()
