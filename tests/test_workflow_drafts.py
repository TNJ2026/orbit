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
    DraftRevisionStateError, DraftSourceTooLargeError, DraftVersionConflictError,
    MAX_SOURCE_BYTES,
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


class DraftReviseTests(DraftTestCase):
    def _reviser(self, new_source):
        from orbit.workflow.authoring.generator import GenerationOutcome
        from orbit.workflow.domain.serialization import definition_hash

        calls = []

        def reviser(current_source, instruction, *, expected_workflow_id, agent=None):
            calls.append((current_source, instruction, expected_workflow_id, agent))
            from orbit.workflow.dsl import compile_source

            compiled = compile_source(
                new_source, self.definitions.catalogs.handlers,
                self.definitions.catalogs.schemas, source_format="json",
            )
            return GenerationOutcome(
                source=new_source, workflow_id=compiled.ir.workflow_id,
                definition_hash=compiled.definition_hash.value,
                node_count=len(compiled.ir.nodes), attempts=1,
            )

        return reviser, calls

    def _service_with(self, reviser):
        return WorkflowDraftApplicationService(
            self.path, self.definitions, reviser=reviser,
        )

    def _run_worker(self, service, *, worker="worker-1", clock=None):
        """One dispatcher turn: lease the queued job and settle it.

        The Agent call is a durable job now, so a test that wants a candidate
        must run the worker just as the background loop would.
        """
        from datetime import timedelta

        claimed = service.claim_revision(
            worker, NOW, lease_ttl=timedelta(minutes=5),
        )
        if claimed is None:
            return None
        job, token = claimed
        return service.execute_revision(
            job, token, clock=clock or (lambda: NOW),
            agent_command="fake-cli", model_id="fake-model",
        )

    def test_revise_stages_a_candidate_then_accepts_and_publishes(self) -> None:
        renamed = json.dumps(dsl(name="Revised"))
        reviser, calls = self._reviser(renamed)
        service = self._service_with(reviser)
        draft = service.create_or_resume(
            "workflow:draftable", base_version=None, actor="author", now=NOW,
        )
        staged = service.revise(
            EntityId.parse(draft.draft_id), "rename to Revised",
            expected_revision=draft.revision, actor="author", now=NOW,
        )
        # The call only enqueues; the source is untouched until a worker runs.
        self.assertNotIn("Revised", staged.source_text)
        self.assertEqual(draft.revision + 1, staged.revision)
        queued, _, _ = service.revision_context(
            EntityId.parse(draft.draft_id), actor="author",
        )
        self.assertEqual("queued", queued.status)
        self.assertIsNone(queued.proposed_source_text)

        settled = self._run_worker(service)
        self.assertEqual("pending", settled.status)
        self.assertEqual("fake-cli", settled.agent_command)
        self.assertIsNotNone(settled.duration_ms)

        pending, history, undoable = service.revision_context(
            EntityId.parse(draft.draft_id), actor="author",
        )
        self.assertIn("Revised", pending.proposed_source_text)
        self.assertEqual("pending", history[0].status)
        self.assertFalse(undoable)
        # The reviser saw the seeded source, the instruction and the workflow id.
        self.assertEqual("workflow:draftable", calls[0][2])

        revised = service.accept_revision(
            EntityId.parse(draft.draft_id),
            expected_revision=staged.revision, actor="author", now=NOW,
        )
        self.assertEqual("valid", revised.validation_status)
        self.assertIn("Revised", revised.source_text)

        record, version = service.publish(
            EntityId.parse(draft.draft_id),
            expected_revision=revised.revision, actor="author", now=NOW,
        )
        self.assertEqual(2, version["version"])
        stored = self.store.get("workflow:draftable", 2)
        self.assertIn("Revised", stored.source_text)

    def test_reject_preserves_source_and_undo_restores_previous_revision(self) -> None:
        reviser, _ = self._reviser(json.dumps(dsl(name="Candidate")))
        service = self._service_with(reviser)
        draft = service.create_or_resume(
            "workflow:draftable", base_version=None, actor="author", now=NOW,
        )
        staged = service.revise(
            EntityId.parse(draft.draft_id), "try a change",
            expected_revision=draft.revision, actor="author", now=NOW,
        )
        self._run_worker(service)
        rejected = service.reject_revision(
            EntityId.parse(draft.draft_id),
            expected_revision=staged.revision, actor="author", now=NOW,
        )
        self.assertEqual(draft.source_hash, rejected.source_hash)

        staged = service.revise(
            EntityId.parse(draft.draft_id), "accept a change",
            expected_revision=rejected.revision, actor="author", now=NOW,
        )
        self._run_worker(service, worker="worker-2")
        accepted = service.accept_revision(
            EntityId.parse(draft.draft_id),
            expected_revision=staged.revision, actor="author", now=NOW,
        )
        self.assertIn("Candidate", accepted.source_text)
        undone = service.undo_revision(
            EntityId.parse(draft.draft_id),
            expected_revision=accepted.revision, actor="author", now=NOW,
        )
        self.assertEqual(draft.source_hash, undone.source_hash)
        _, history, undoable = service.revision_context(
            EntityId.parse(draft.draft_id), actor="author",
        )
        self.assertEqual(["undone", "rejected"], [item.status for item in history])
        self.assertFalse(undoable)

    def test_pending_candidate_blocks_another_agent_call_and_publish(self) -> None:
        reviser, calls = self._reviser(json.dumps(dsl(name="Candidate")))
        service = self._service_with(reviser)
        draft = service.create_or_resume(
            "workflow:draftable", base_version=None, actor="author", now=NOW,
        )
        staged = service.revise(
            EntityId.parse(draft.draft_id), "first change",
            expected_revision=draft.revision, actor="author", now=NOW,
        )
        # Single flight starts at enqueue: a second prompt is refused while the
        # first is merely queued, so two model calls can never overlap.
        with self.assertRaises(DraftRevisionStateError):
            service.revise(
                EntityId.parse(draft.draft_id), "second change",
                expected_revision=staged.revision, actor="author", now=NOW,
            )
        self.assertEqual(0, len(calls))

        self._run_worker(service)
        with self.assertRaises(DraftRevisionStateError):
            service.revise(
                EntityId.parse(draft.draft_id), "third change",
                expected_revision=staged.revision, actor="author", now=NOW,
            )
        with self.assertRaises(DraftRevisionStateError):
            service.publish(
                EntityId.parse(draft.draft_id),
                expected_revision=staged.revision, actor="author", now=NOW,
            )
        self.assertEqual(1, len(calls))

    def test_revise_without_a_reviser_is_unavailable(self) -> None:
        from orbit.workflow.application.workflow_draft_service import (
            RevisionUnavailableError,
        )

        draft = self.draft()
        with self.assertRaises(RevisionUnavailableError):
            self.service.revise(
                EntityId.parse(draft.draft_id), "change something",
                expected_revision=draft.revision, actor="author", now=NOW,
            )

    def test_a_stale_revision_conflicts_before_calling_the_agent(self) -> None:
        reviser, calls = self._reviser(json.dumps(dsl(name="X")))
        service = self._service_with(reviser)
        draft = service.create_or_resume(
            "workflow:draftable", base_version=None, actor="author", now=NOW,
        )
        with self.assertRaises(DraftVersionConflictError):
            service.revise(
                EntityId.parse(draft.draft_id), "change",
                expected_revision=99, actor="author", now=NOW,
            )
        self.assertEqual([], calls)

    def test_a_fresh_draft_is_not_mistaken_for_published(self) -> None:
        # Merely seeding source from a published version is not a publish.
        draft = self.draft()
        again = self.service.get(
            EntityId.parse(draft.draft_id), actor="author", now=NOW,
        )
        self.assertEqual("active", again.status)
        self.assertIsNone(again.published_version)


class RevisionJobTests(DraftReviseTests):
    """The Agent call as a durable job: leases, cancel, expiry, audit."""

    def _queued(self, service, draft, instruction="do a thing"):
        return service.revise(
            EntityId.parse(draft.draft_id), instruction,
            expected_revision=draft.revision, actor="author", now=NOW,
        )

    def test_the_chosen_agent_is_recorded_and_used_when_the_job_runs(self) -> None:
        """The dispatcher runs minutes later; it must honour the choice made then."""

        reviser, calls = self._reviser(json.dumps(dsl(name="By codex")))
        service = self._service_with(reviser)
        draft = service.create_or_resume(
            "workflow:draftable", base_version=None, actor="author", now=NOW,
        )
        service.revise(
            EntityId.parse(draft.draft_id), "rename it",
            expected_revision=draft.revision, actor="author", now=NOW,
            agent="codex",
        )
        job, _, _ = service.revision_context(
            EntityId.parse(draft.draft_id), actor="author",
        )
        self.assertEqual("codex", job.requested_agent)
        self._run_worker(service)
        self.assertEqual("codex", calls[0][3])

    def test_no_choice_leaves_the_runtime_default_in_place(self) -> None:
        reviser, calls = self._reviser(json.dumps(dsl(name="By default")))
        service = self._service_with(reviser)
        draft = service.create_or_resume(
            "workflow:draftable", base_version=None, actor="author", now=NOW,
        )
        self._queued(service, draft)
        self._run_worker(service)
        self.assertIsNone(calls[0][3])

    def test_enqueue_returns_before_the_agent_runs(self) -> None:
        reviser, calls = self._reviser(json.dumps(dsl(name="Later")))
        service = self._service_with(reviser)
        draft = service.create_or_resume(
            "workflow:draftable", base_version=None, actor="author", now=NOW,
        )
        self._queued(service, draft)
        # The request did not wait for the CLI.
        self.assertEqual([], calls)
        job, _, _ = service.revision_context(
            EntityId.parse(draft.draft_id), actor="author",
        )
        self.assertEqual("queued", job.status)
        self.assertTrue(job.in_flight)

    def test_a_claimed_job_records_agent_model_and_duration(self) -> None:
        from datetime import timedelta

        reviser, _ = self._reviser(json.dumps(dsl(name="Timed")))
        service = self._service_with(reviser)
        draft = service.create_or_resume(
            "workflow:draftable", base_version=None, actor="author", now=NOW,
        )
        self._queued(service, draft)
        claimed = service.claim_revision(
            "worker-1", NOW, lease_ttl=timedelta(minutes=5),
        )
        self.assertIsNotNone(claimed)
        job, token = claimed
        self.assertEqual("running", job.status)
        self.assertEqual(1, job.fencing_token)

        ticks = iter([NOW, NOW + timedelta(seconds=7)])
        settled = service.execute_revision(
            job, token, clock=lambda: next(ticks),
            agent_command="claude", model_id="claude-x",
        )
        self.assertEqual("pending", settled.status)
        self.assertEqual("claude", settled.agent_command)
        self.assertEqual("claude-x", settled.model_id)
        self.assertEqual(7000, settled.duration_ms)

    def test_only_one_worker_can_claim_a_job(self) -> None:
        from datetime import timedelta

        reviser, _ = self._reviser(json.dumps(dsl(name="Once")))
        service = self._service_with(reviser)
        draft = service.create_or_resume(
            "workflow:draftable", base_version=None, actor="author", now=NOW,
        )
        self._queued(service, draft)
        first = service.claim_revision("w1", NOW, lease_ttl=timedelta(minutes=5))
        second = service.claim_revision("w2", NOW, lease_ttl=timedelta(minutes=5))
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_a_failing_agent_settles_the_job_with_its_reason(self) -> None:
        from datetime import timedelta

        def angry(current_source, instruction, *, expected_workflow_id, agent=None):
            raise RuntimeError("the model refused")

        service = self._service_with(angry)
        draft = service.create_or_resume(
            "workflow:draftable", base_version=None, actor="author", now=NOW,
        )
        self._queued(service, draft)
        job, token = service.claim_revision(
            "w1", NOW, lease_ttl=timedelta(minutes=5),
        )
        settled = service.execute_revision(job, token, clock=lambda: NOW)
        self.assertEqual("failed", settled.status)
        self.assertEqual("RuntimeError", settled.error_code)
        self.assertIn("refused", settled.error_message)
        # A failed job frees the draft for another attempt.
        again = service.revise(
            EntityId.parse(draft.draft_id), "try again",
            expected_revision=draft.revision + 1, actor="author", now=NOW,
        )
        self.assertIsNotNone(again)

    def test_cancelling_a_queued_job_never_calls_the_agent(self) -> None:
        reviser, calls = self._reviser(json.dumps(dsl(name="Nope")))
        service = self._service_with(reviser)
        draft = service.create_or_resume(
            "workflow:draftable", base_version=None, actor="author", now=NOW,
        )
        self._queued(service, draft)
        job, _, _ = service.revision_context(
            EntityId.parse(draft.draft_id), actor="author",
        )
        service.cancel_revision(
            EntityId.parse(draft.draft_id), EntityId.parse(job.revision_id),
            actor="author", now=NOW,
        )
        self.assertIsNone(self._run_worker(service))
        self.assertEqual([], calls)
        current, _, _ = service.revision_context(
            EntityId.parse(draft.draft_id), actor="author",
        )
        self.assertIsNone(current)

    def test_cancelling_a_running_job_discards_the_answer(self) -> None:
        from datetime import timedelta

        reviser, _ = self._reviser(json.dumps(dsl(name="Discarded")))
        service = self._service_with(reviser)
        draft = service.create_or_resume(
            "workflow:draftable", base_version=None, actor="author", now=NOW,
        )
        self._queued(service, draft)
        job, token = service.claim_revision(
            "w1", NOW, lease_ttl=timedelta(minutes=5),
        )
        service.cancel_revision(
            EntityId.parse(draft.draft_id), EntityId.parse(job.revision_id),
            actor="author", now=NOW,
        )
        settled = service.execute_revision(job, token, clock=lambda: NOW)
        # The Agent answered, but the operator had already said stop: the reply
        # is dropped rather than shown as a candidate.
        self.assertEqual("cancelled", settled.status)
        self.assertIsNone(settled.proposed_source_text)

    def test_an_abandoned_lease_expires_into_a_failure(self) -> None:
        from datetime import timedelta

        reviser, _ = self._reviser(json.dumps(dsl(name="Orphan")))
        service = self._service_with(reviser)
        draft = service.create_or_resume(
            "workflow:draftable", base_version=None, actor="author", now=NOW,
        )
        self._queued(service, draft)
        job, token = service.claim_revision(
            "dead-worker", NOW, lease_ttl=timedelta(minutes=5),
        )
        expired = service.expire_revisions(NOW + timedelta(minutes=6))
        self.assertEqual((job.revision_id,), expired)
        current, _, _ = service.revision_context(
            EntityId.parse(draft.draft_id), actor="author",
        )
        self.assertIsNone(current)

        # The straggler comes back after the lease was reclaimed: its write is
        # fenced off so it cannot resurrect a job the operator saw fail.
        from orbit.workflow.application.workflow_draft_service import (
            RevisionLeaseError,
        )

        with self.assertRaises(RevisionLeaseError):
            service.execute_revision(job, token, clock=lambda: NOW)


if __name__ == "__main__":
    unittest.main()
