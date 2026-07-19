from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from orbit.workflow.catalogs import HandlerManifest, InMemoryHandlerCatalog, InMemorySchemaCatalog
from orbit.workflow.dsl import compile_source
from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.handlers import ResourceProfile
from orbit.workflow.persistence import PublishConflictError, SQLiteWorkflowVersionStore
from orbit.workflow.persistence.database import connect_workflow_database
from tests.test_workflow_dsl import VALID_DSL


class WorkflowVersionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "workflow.db"
        self.store = SQLiteWorkflowVersionStore(self.db_path)
        self.schemas = InMemorySchemaCatalog(
            {"example://request/1.0": {"type": "object"}}
        )
        self.handlers = InMemoryHandlerCatalog(
            [
                HandlerManifest(
                    "collect", "1.2.0", ("action",), {},
                    {"request": "example://request/1.0"},
                    {"type": "object", "additionalProperties": False},
                    ExecutionSafety.REPLAY_SAFE,
                    ResourceProfile(0, 0, 0, 60, 0, "free"),
                    "example://request/1.0",
                )
            ]
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def compile(self, *, name: str = "Approval flow"):
        value = json.loads(json.dumps(VALID_DSL))
        value["metadata"]["name"] = name
        return compile_source(
            json.dumps(value), self.handlers, self.schemas, source_format="json"
        )

    def publish(self, compiled, expected: int):
        return self.store.publish(
            compiled,
            expected_latest_version=expected,
            source_format="json",
            source_text="{}",
            actor="test",
        )

    def test_publish_is_immutable_idempotent_and_round_trips_ir(self) -> None:
        compiled = self.compile()
        first = self.publish(compiled, 0)
        duplicate = self.publish(compiled, 999)
        self.assertEqual(1, first.version.value)
        self.assertEqual(first, duplicate)
        loaded = self.store.get(first.workflow_id, 1)
        self.assertEqual(compiled.ir, loaded.ir)
        self.assertEqual(compiled.definition_hash, loaded.definition_hash)
        self.assertEqual(1, self.store.latest_version(first.workflow_id))

    def test_expected_version_conflict_and_next_version(self) -> None:
        first = self.publish(self.compile(), 0)
        changed = self.compile(name="Approval flow v2")
        with self.assertRaises(PublishConflictError) as raised:
            self.publish(changed, 0)
        self.assertEqual(1, raised.exception.actual)
        second = self.publish(changed, 1)
        self.assertEqual(2, second.version.value)
        self.assertEqual(first.workflow_id, second.workflow_id)
        with connect_workflow_database(self.db_path) as connection:
            display_name = connection.execute(
                "SELECT name FROM workflow_definitions WHERE workflow_id = ?",
                (first.workflow_id,),
            ).fetchone()[0]
        self.assertEqual("Approval flow v2", display_name)

    def test_database_triggers_reject_update_and_delete(self) -> None:
        record = self.publish(self.compile(), 0)
        connection = connect_workflow_database(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE workflow_versions SET created_by = 'tampered' WHERE workflow_id = ?",
                    (record.workflow_id,),
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM workflow_versions WHERE workflow_id = ?",
                    (record.workflow_id,),
                )
        finally:
            connection.close()

    def test_concurrent_publish_allocates_only_one_next_version(self) -> None:
        self.publish(self.compile(), 0)
        candidates = [self.compile(name="Candidate A"), self.compile(name="Candidate B")]

        def attempt(compiled):
            try:
                return self.publish(compiled, 1).version.value
            except PublishConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, candidates))
        self.assertEqual([2, "conflict"], sorted(results, key=str))
        self.assertEqual(2, self.store.latest_version("workflow:approval_flow"))

    def test_migration_ledger_is_idempotent(self) -> None:
        SQLiteWorkflowVersionStore(self.db_path)
        with connect_workflow_database(self.db_path) as connection:
            versions = connection.execute(
                "SELECT version FROM workflow_schema_migrations ORDER BY version"
            ).fetchall()
            self.assertEqual(list(range(1, 13)), [row[0] for row in versions])


if __name__ == "__main__":
    unittest.main()
