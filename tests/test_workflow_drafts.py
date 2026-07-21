"""Draft lifecycle: CAS, one-active, validation gating, publish semantics.

Everything runs against the real migration, the real compiler and the real
version store — the only fake anywhere is the clock.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest

from orbit.workflow.application.workflow_draft_service import (
    DraftAlreadyActiveError, DraftNotFoundError, DraftNotValidatedError,
    DraftSourceTooLargeError, DraftVersionConflictError, MAX_SOURCE_BYTES,
    SourceUnavailableError, WorkflowDraftApplicationService,
    WorkflowVersionConflictError,
)
from orbit.workflow.application.workflows import (
    WorkflowCatalogs, WorkflowDefinitionService,
)
from orbit.workflow.catalogs import (
    HandlerManifest, InMemoryHandlerCatalog, InMemorySchemaCatalog,
)
from orbit.workflow.catalogs.extensions import InMemoryExtensionRegistry
from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.handlers import ResourceProfile
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.migrations import migrate_workflow_database
from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore


NOW = datetime(2026, 7, 20, 9, tzinfo=timezone.utc)

MANIFEST = HandlerManifest(
    "transform", "1.0.0", ("action",),
    {"value": "example://integer/1.0"}, {"value": "example://integer/1.0"},
    {"type": "object"}, ExecutionSafety.REPLAY_SAFE,
    ResourceProfile(100_000, 100_000, 0, 300, 0, "builtin"),
    "schema://object/1.0", (), (), True, True,
)


def dsl(workflow_id: str = "draftable", name: str = "Draftable") -> dict:
    return {
        "dsl_version": "1.2",
        "metadata": {"id": workflow_id, "name": name},
        "nodes": [
            {
                "id": "work", "kind": "action",
                "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
                "outputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
                "handler": {"name": "transform", "version": "1.0.0"},
            },
            {
                "id": "done", "kind": "terminal",
                "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
            },
        ],
        "edges": [{
            "id": "flow", "from": {"node": "work", "port": "value"},
            "to": {"node": "done", "port": "value"},
        }],
        "entry": ["work"], "terminals": ["done"],
    }


class DraftTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "drafts.db"
        with connect_workflow_database(self.path) as connection:
            migrate_workflow_database(connection)
        catalogs = WorkflowCatalogs(
            InMemoryHandlerCatalog([MANIFEST]),
            InMemorySchemaCatalog({
                "example://integer/1.0": {"type": "integer"},
                "schema://object/1.0": {"type": "object"},
            }),
            InMemoryExtensionRegistry(),
        )
        self.store = SQLiteWorkflowVersionStore(self.path)
        self.definitions = WorkflowDefinitionService(catalogs, self.store)
        self.definitions.publish_workflow(
            json.dumps(dsl()), source_name="<test>", source_format="json",
            expected_latest_version=0, actor="author",
        )
        self.service = WorkflowDraftApplicationService(self.path, self.definitions)

    def draft(self, actor: str = "author", base: int | None = None):
        return self.service.create_or_resume(
            "workflow:draftable", base_version=base, actor=actor, now=NOW,
        )


class DraftLifecycleTests(DraftTestCase):
    def test_create_seeds_from_the_published_source_and_resume_returns_it(self) -> None:
        first = self.draft()
        self.assertEqual("active", first.status)
        self.assertEqual(1, first.base_version)
        self.assertEqual("dirty", first.validation_status)
        self.assertIn("Draftable", first.source_text)
        again = self.draft()
        self.assertEqual(first.draft_id, again.draft_id)

    def test_a_different_base_collides_with_the_active_draft(self) -> None:
        modified = dsl(name="Draftable v2")
        source = json.dumps(modified)
        draft = self.draft()
        saved = self.service.save(
            EntityId.parse(draft.draft_id), source,
            expected_revision=draft.revision, actor="author", now=NOW,
        )
        validated = self.service.validate(
            EntityId.parse(draft.draft_id),
            expected_revision=saved.revision, actor="author", now=NOW,
        )
        self.service.publish(
            EntityId.parse(draft.draft_id),
            expected_revision=validated.revision, actor="author", now=NOW,
        )
        # v2 exists; a new draft from v1 while another is active must surface
        # the existing one, never rebase it.
        fresh = self.draft(base=2)
        self.assertEqual("active", fresh.status)
        with self.assertRaises(DraftAlreadyActiveError) as caught:
            self.draft(base=1)
        self.assertEqual(fresh.draft_id, caught.exception.draft["draft_id"])

    def test_save_is_cas_and_resets_validation(self) -> None:
        draft = self.draft()
        saved = self.service.save(
            EntityId.parse(draft.draft_id), "{\"broken\": true}",
            expected_revision=draft.revision, actor="author", now=NOW,
        )
        self.assertEqual(draft.revision + 1, saved.revision)
        self.assertEqual("dirty", saved.validation_status)
        with self.assertRaises(DraftVersionConflictError):
            self.service.save(
                EntityId.parse(draft.draft_id), "{}",
                expected_revision=draft.revision, actor="author", now=NOW,
            )

    def test_source_cap_is_enforced_before_any_write(self) -> None:
        draft = self.draft()
        with self.assertRaises(DraftSourceTooLargeError):
            self.service.save(
                EntityId.parse(draft.draft_id), "x" * (MAX_SOURCE_BYTES + 1),
                expected_revision=draft.revision, actor="author", now=NOW,
            )

    def test_source_at_cap_survives_service_reconstruction(self) -> None:
        draft = self.draft()
        source = " " * MAX_SOURCE_BYTES
        saved = self.service.save(
            EntityId.parse(draft.draft_id), source,
            expected_revision=draft.revision, actor="author", now=NOW,
        )
        restarted = WorkflowDraftApplicationService(self.path, self.definitions)
        restored = restarted.get(
            EntityId.parse(draft.draft_id), actor="author", now=NOW,
        )
        self.assertEqual(MAX_SOURCE_BYTES, len(restored.source_text.encode("utf-8")))
        self.assertEqual(saved.revision, restored.revision)

    def test_thirty_invalid_nodes_produce_diagnostics_within_budget(self) -> None:
        draft = self.draft()
        document = dsl()
        document["nodes"] = [
            {"id": f"node_{index}", "kind": "action"} for index in range(30)
        ]
        document["entry"] = ["node_0"]
        document["terminals"] = ["node_29"]
        document["edges"] = []
        saved = self.service.save(
            EntityId.parse(draft.draft_id), json.dumps(document),
            expected_revision=draft.revision, actor="author", now=NOW,
        )
        started = time.monotonic()
        checked = self.service.validate(
            EntityId.parse(draft.draft_id), expected_revision=saved.revision,
            actor="author", now=NOW,
        )
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual("invalid", checked.validation_status)
        self.assertGreaterEqual(len(checked.diagnostics), 30)

    def test_validation_records_diagnostics_and_gates_publish(self) -> None:
        draft = self.draft()
        saved = self.service.save(
            EntityId.parse(draft.draft_id), "{\"not\": \"a workflow\"}",
            expected_revision=draft.revision, actor="author", now=NOW,
        )
        checked = self.service.validate(
            EntityId.parse(draft.draft_id),
            expected_revision=saved.revision, actor="author", now=NOW,
        )
        self.assertEqual("invalid", checked.validation_status)
        self.assertTrue(checked.diagnostics)
        self.assertIn("json_path", checked.diagnostics[0])
        self.assertNotIn("path", checked.diagnostics[0])
        with self.assertRaises(DraftNotValidatedError):
            self.service.publish(
                EntityId.parse(draft.draft_id),
                expected_revision=checked.revision, actor="author", now=NOW,
            )

    def test_editing_after_validation_requires_revalidation(self) -> None:
        draft = self.draft()
        good = json.dumps(dsl(name="Renamed"))
        saved = self.service.save(
            EntityId.parse(draft.draft_id), good,
            expected_revision=draft.revision, actor="author", now=NOW,
        )
        validated = self.service.validate(
            EntityId.parse(draft.draft_id),
            expected_revision=saved.revision, actor="author", now=NOW,
        )
        self.assertEqual("valid", validated.validation_status)
        touched = self.service.save(
            EntityId.parse(draft.draft_id), good + "\n",
            expected_revision=validated.revision, actor="author", now=NOW,
        )
        self.assertEqual("dirty", touched.validation_status)
        with self.assertRaises(DraftNotValidatedError):
            self.service.publish(
                EntityId.parse(draft.draft_id),
                expected_revision=touched.revision, actor="author", now=NOW,
            )

    def test_publish_creates_the_next_version_and_completes_the_draft(self) -> None:
        draft = self.draft()
        saved = self.service.save(
            EntityId.parse(draft.draft_id), json.dumps(dsl(name="Renamed")),
            expected_revision=draft.revision, actor="author", now=NOW,
        )
        validated = self.service.validate(
            EntityId.parse(draft.draft_id),
            expected_revision=saved.revision, actor="author", now=NOW,
        )
        record, version = self.service.publish(
            EntityId.parse(draft.draft_id),
            expected_revision=validated.revision, actor="author", now=NOW,
        )
        self.assertEqual(2, version["version"])
        self.assertEqual("published", record.status)
        self.assertEqual(2, record.published_version)
        # The published version carries the draft's source for future drafts.
        stored = self.store.get("workflow:draftable", 2)
        self.assertIn("Renamed", stored.source_text)

    def test_noop_publish_returns_the_base_version(self) -> None:
        draft = self.draft()
        validated = self.service.validate(
            EntityId.parse(draft.draft_id),
            expected_revision=draft.revision, actor="author", now=NOW,
        )
        record, version = self.service.publish(
            EntityId.parse(draft.draft_id),
            expected_revision=validated.revision, actor="author", now=NOW,
        )
        self.assertEqual(1, version["version"])
        self.assertEqual(1, record.published_version)
        self.assertEqual("published", record.status)

    def test_stale_base_surfaces_a_version_conflict_and_keeps_the_draft(self) -> None:
        # Author A drafts from v1; author B publishes v2 meanwhile.
        draft = self.draft()
        other = self.service.create_or_resume(
            "workflow:draftable", base_version=1, actor="rival", now=NOW,
        )
        other_saved = self.service.save(
            EntityId.parse(other.draft_id), json.dumps(dsl(name="Rival")),
            expected_revision=other.revision, actor="rival", now=NOW,
        )
        other_valid = self.service.validate(
            EntityId.parse(other.draft_id),
            expected_revision=other_saved.revision, actor="rival", now=NOW,
        )
        self.service.publish(
            EntityId.parse(other.draft_id),
            expected_revision=other_valid.revision, actor="rival", now=NOW,
        )

        mine_saved = self.service.save(
            EntityId.parse(draft.draft_id), json.dumps(dsl(name="Mine")),
            expected_revision=draft.revision, actor="author", now=NOW,
        )
        mine_valid = self.service.validate(
            EntityId.parse(draft.draft_id),
            expected_revision=mine_saved.revision, actor="author", now=NOW,
        )
        with self.assertRaises(WorkflowVersionConflictError) as caught:
            self.service.publish(
                EntityId.parse(draft.draft_id),
                expected_revision=mine_valid.revision, actor="author", now=NOW,
            )
        self.assertEqual(1, caught.exception.base_version)
        self.assertEqual(2, caught.exception.latest_version)
        kept = self.service.get(
            EntityId.parse(draft.draft_id), actor="author", now=NOW
        )
        self.assertEqual("active", kept.status)
        self.assertIn("Mine", kept.source_text)

    def test_drafts_are_invisible_to_other_actors(self) -> None:
        draft = self.draft()
        with self.assertRaises(DraftNotFoundError):
            self.service.get(
                EntityId.parse(draft.draft_id), actor="rival", now=NOW
            )
        with self.assertRaises(DraftNotFoundError):
            self.service.save(
                EntityId.parse(draft.draft_id), "{}",
                expected_revision=draft.revision, actor="rival", now=NOW,
            )

    def test_discard_frees_the_active_slot(self) -> None:
        draft = self.draft()
        gone = self.service.discard(
            EntityId.parse(draft.draft_id),
            expected_revision=draft.revision, actor="author", now=NOW,
        )
        self.assertEqual("discarded", gone.status)
        fresh = self.draft()
        self.assertNotEqual(draft.draft_id, fresh.draft_id)

    def test_missing_source_versions_are_not_editable(self) -> None:
        # A version published without source_text (early CLI/test data).
        from orbit.workflow.dsl import compile_source

        compiled = compile_source(
            json.dumps(dsl("legacy", "Legacy")),
            self.definitions.catalogs.handlers,
            self.definitions.catalogs.schemas,
            source_format="json",
        )
        self.store.publish(
            compiled, expected_latest_version=0, source_format="json",
            source_text=None, actor="author",
        )
        with self.assertRaises(SourceUnavailableError):
            self.service.create_or_resume(
                "workflow:legacy", base_version=None, actor="author", now=NOW,
            )

    def test_crash_between_publish_and_bookkeeping_reconciles_on_read(self) -> None:
        draft = self.draft()
        saved = self.service.save(
            EntityId.parse(draft.draft_id), json.dumps(dsl(name="Crashy")),
            expected_revision=draft.revision, actor="author", now=NOW,
        )
        validated = self.service.validate(
            EntityId.parse(draft.draft_id),
            expected_revision=saved.revision, actor="author", now=NOW,
        )
        # Simulate the crash window: the version lands, the draft update
        # never happens.
        self.definitions.publish_workflow(
            validated.source_text, source_name="<crash>", source_format="json",
            expected_latest_version=1, actor="author",
        )
        recovered = self.service.get(
            EntityId.parse(draft.draft_id), actor="author", now=NOW
        )
        self.assertEqual("published", recovered.status)
        self.assertEqual(2, recovered.published_version)


if __name__ == "__main__":
    unittest.main()
