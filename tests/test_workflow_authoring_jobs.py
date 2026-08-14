"""Asynchronous Workflow authoring: the Job, and what it says it changed.

Generation and Prompt modification are Agent calls that take minutes, so the
product runs them as Jobs rather than holding an HTTP request open. Two things
matter enough to pin here: a Job never publishes what it did not finish, and
the change summary it reports is verified against the definition it produced
rather than taken on the Agent's word.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import time
import unittest

from orbit.workflow.application.authoring_job_service import (
    AuthoringJobConflict, AuthoringJobService,
)
from orbit.workflow.application.workflows import (
    WorkflowCatalogs, WorkflowDefinitionService,
)
from orbit.workflow.authoring.generator import (
    GenerationOutcome, WorkflowAuthoringService, _clean_change_summary,
)
from orbit.workflow.catalogs import (
    HandlerManifest, InMemoryHandlerCatalog, InMemorySchemaCatalog,
)
from orbit.workflow.catalogs.extensions import InMemoryExtensionRegistry
from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.handlers import ResourceProfile
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.migrations import migrate_workflow_database
from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore


MANIFEST = HandlerManifest(
    "transform", "1.0.0", ("action",),
    {"value": "example://integer/1.0"}, {"value": "example://integer/1.0"},
    {"type": "object"}, ExecutionSafety.REPLAY_SAFE,
    ResourceProfile(100_000, 100_000, 0, 300, 0, "builtin"),
    "schema://object/1.0", (), (), True, True,
)


def dsl(*, nodes=("collect",), name="Research") -> dict:
    """A 1.3 document whose action nodes are named by the caller."""

    action_nodes = [
        {
            "id": node_id, "kind": "action",
            "label": node_id.replace("_", " ").title(),
            "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
            "outputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
            "handler": {"name": "transform", "version": "1.0.0"},
        }
        for node_id in nodes
    ]
    edges = [
        {
            "id": f"to_{after}", "from": {"node": before, "port": "value"},
            "to": {"node": after, "port": "value"},
        }
        for before, after in zip(nodes, nodes[1:])
    ]
    edges.append({
        "id": "finish", "from": {"node": nodes[-1], "port": "value"},
        "to": {"node": "done", "port": "value"},
    })
    return {
        "dsl_version": "1.3",
        "metadata": {"id": "research", "name": name},
        "nodes": [
            *action_nodes,
            {
                "id": "done", "kind": "terminal",
                "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
            },
        ],
        "edges": edges,
        "entry": [nodes[0]], "terminals": ["done"],
        "result": {"node": nodes[-1], "port": "value"},
    }


class AuthoringJobTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # A SQLite connection opened during the test may still be waiting to
        # be collected when the directory goes, and closing it writes `-wal`
        # back into a directory `rmtree` has already walked. That failed in
        # `tearDown`, in whichever test happened to be last — the assertions
        # had all passed — about one run in eight.
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "jobs.db"
        with connect_workflow_database(self.path) as connection:
            migrate_workflow_database(connection)
        self.catalogs = WorkflowCatalogs(
            InMemoryHandlerCatalog([MANIFEST]),
            InMemorySchemaCatalog({
                "example://integer/1.0": {"type": "integer"},
                "schema://object/1.0": {"type": "object"},
            }),
            InMemoryExtensionRegistry(),
        )
        self.definitions = WorkflowDefinitionService(
            self.catalogs, SQLiteWorkflowVersionStore(self.path)
        )
        # What the Agent will answer next. Each test sets it before acting.
        self.answer = json.dumps(dsl())

    def authoring(self) -> WorkflowAuthoringService:
        return WorkflowAuthoringService(
            self.catalogs.handlers, self.catalogs.schemas,
            lambda _prompt: self.answer,
            handler_facts=[{
                "name": "transform", "version": "1.0.0", "kinds": ["action"],
                "inputs": {"value": "example://integer/1.0"},
                "outputs": {"value": "example://integer/1.0"},
            }],
        )

    def service(self) -> AuthoringJobService:
        return AuthoringJobService(self.path, self.authoring(), self.definitions)

    def settled(self, jobs, job_id, *, actor="author", timeout=10.0):
        """Jobs run on their own thread; wait for the terminal state."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = jobs.get(job_id, actor=actor)
            if job["status"] not in {"queued", "running"}:
                return job
            time.sleep(0.02)
        self.fail(f"job {job_id} never settled")


class GenerationJobTests(AuthoringJobTestCase):
    def test_a_generation_job_publishes_the_workflow_it_produced(self) -> None:
        jobs = self.service()
        created = jobs.create(actor="author", prompt="Research a topic", idempotency_key="g1")
        # The worker thread may already have claimed it; both are "in flight".
        self.assertIn(created["status"], {"queued", "running"})
        self.assertIsNone(created["deadline_at"])

        job = self.settled(jobs, created["job_id"])
        self.assertEqual("done", job["status"])
        self.assertRegex(
            job["result"]["workflow_id"],
            r"^workflow:wf_[0-9a-f]{8}-[0-9a-f-]{27}$",
        )
        with connect_workflow_database(self.path, read_only=True) as connection:
            stored = json.loads(connection.execute(
                "SELECT canonical_ir_json FROM workflow_versions WHERE workflow_id=?",
                (job["result"]["workflow_id"],),
            ).fetchone()["canonical_ir_json"])
        self.assertEqual("research", stored["slug"])
        # A generation has nothing to compare against, so it reports no summary.
        self.assertNotIn("change_summary", job["result"])

    def test_one_active_generation_per_actor_and_idempotent_replay(self) -> None:
        jobs = self.service()
        first = jobs.create(actor="author", prompt="Research", idempotency_key="g1")
        replay = jobs.create(actor="author", prompt="Research", idempotency_key="g1")
        self.assertEqual(first["job_id"], replay["job_id"])
        self.settled(jobs, first["job_id"])

        # A second actor is never blocked by the first actor's work.
        other = jobs.create(actor="second", prompt="Research", idempotency_key="g1")
        self.assertNotEqual(first["job_id"], other["job_id"])
        self.settled(jobs, other["job_id"], actor="second")

    def test_a_failed_generation_publishes_nothing(self) -> None:
        self.answer = "this is not a workflow"
        jobs = self.service()
        created = jobs.create(actor="author", prompt="Research", idempotency_key="g1")
        job = self.settled(jobs, created["job_id"])
        self.assertEqual("failed", job["status"])
        self.assertIsNotNone(job["error"])
        with connect_workflow_database(self.path, read_only=True) as connection:
            published = connection.execute(
                "SELECT COUNT(*) AS total FROM workflow_versions"
            ).fetchone()["total"]
        self.assertEqual(0, published)

    def test_a_settled_job_records_attempts_and_failing_diagnostics(self) -> None:
        """Prompt tuning has no input unless failures say what was refused."""

        jobs = self.service()
        done = self.settled(jobs, jobs.create(
            actor="author", prompt="Research", idempotency_key="ok",
        )["job_id"])
        self.assertEqual("done", done["status"])
        self.assertEqual(1, done["attempts"])

        self.answer = "this is not a workflow"
        failed = self.settled(jobs, jobs.create(
            actor="author", prompt="Research", idempotency_key="bad",
        )["job_id"])
        self.assertEqual("failed", failed["status"])
        self.assertEqual(5, failed["attempts"])
        codes = [item.get("code") for item in failed["error"]["diagnostics"]]
        self.assertIn("GENERATION_PROTOCOL", codes)
        chunks, _ = jobs.output(failed["job_id"])
        validation_log = "".join(
            chunk["text"] for chunk in chunks if chunk["stream"] == "stderr"
        )
        self.assertIn("[validation 1/5] rejected", validation_log)
        self.assertIn("GENERATION_PROTOCOL", validation_log)
        self.assertIn("rule:", validation_log)

    def test_an_interrupted_job_is_failed_as_an_unknown_result(self) -> None:
        """A process that died mid-call cannot claim the Agent did nothing."""

        jobs = self.service()
        created = jobs.create(actor="author", prompt="Research", idempotency_key="g1")
        self.settled(jobs, created["job_id"])
        with connect_workflow_database(self.path) as connection:
            connection.execute(
                "UPDATE workflow_authoring_jobs SET status='running',result_json=NULL"
            )
            connection.commit()

        # Recovery runs in the constructor: a new process adopting this database.
        recovered = self.service().get(created["job_id"], actor="author")
        self.assertEqual("failed", recovered["status"])
        self.assertEqual("unknown_external_result", recovered["error"]["code"])
        with connect_workflow_database(self.path, read_only=True) as connection:
            actions = [
                row["action"] for row in connection.execute(
                    "SELECT action FROM audit_records WHERE target_id=?",
                    (created["job_id"],),
                )
            ]
        self.assertIn("workflow.authoring.unknown_result", actions)


class ModificationJobTests(AuthoringJobTestCase):
    def publish(self) -> None:
        self.definitions.publish_workflow(
            json.dumps(dsl()), source_name="<test>", source_format="json",
            expected_latest_version=0, actor="author",
        )

    def modify(self, jobs, *, key="m1", mode="modify"):
        return jobs.create(
            actor="author", prompt="Add a fact check", idempotency_key=key,
            workflow_id="workflow:research", mode=mode,
        )

    def test_the_agents_own_summary_is_used_when_every_step_checks_out(self) -> None:
        self.publish()
        self.answer = json.dumps({
            "workflow": dsl(nodes=("collect", "fact_check")),
            "change_summary": [
                {
                    "kind": "added", "node_id": "fact_check",
                    "label": "Fact check", "detail": "runs before the report",
                },
            ],
        })
        jobs = self.service()
        job = self.settled(jobs, self.modify(jobs)["job_id"])

        self.assertEqual("done", job["status"])
        summary = job["result"]["change_summary"]
        self.assertEqual("agent", summary["source"])
        self.assertEqual(
            [{
                "kind": "added", "node_id": "fact_check",
                "label": "Fact check", "detail": "runs before the report",
            }],
            summary["entries"],
        )

    def test_a_summary_naming_a_step_that_does_not_exist_is_replaced(self) -> None:
        """An Agent must not be able to describe work it did not do."""

        self.publish()
        self.answer = json.dumps({
            "workflow": dsl(nodes=("collect", "fact_check")),
            "change_summary": [
                {"kind": "added", "node_id": "invented", "label": "Invented step"},
            ],
        })
        jobs = self.service()
        job = self.settled(jobs, self.modify(jobs)["job_id"])

        summary = job["result"]["change_summary"]
        self.assertEqual("diff", summary["source"])
        self.assertNotIn(
            "Invented step", [entry["label"] for entry in summary["entries"]],
        )
        self.assertEqual(
            [{"kind": "added", "node_id": "fact_check", "label": "Fact Check"}],
            summary["entries"],
        )

    def test_a_bare_document_still_modifies_and_falls_back_to_the_diff(self) -> None:
        """An Agent that only emits DSL is not broken, just silent."""

        self.publish()
        self.answer = json.dumps(dsl(nodes=("collect", "fact_check")))
        jobs = self.service()
        job = self.settled(jobs, self.modify(jobs)["job_id"])

        self.assertEqual("done", job["status"])
        summary = job["result"]["change_summary"]
        self.assertEqual("diff", summary["source"])
        self.assertEqual(["fact_check"], [e["node_id"] for e in summary["entries"]])
        self.assertEqual(1, summary["edges_added"])

    def test_the_diff_names_removed_steps_from_the_previous_definition(self) -> None:
        self.definitions.publish_workflow(
            json.dumps(dsl(nodes=("collect", "fact_check"))),
            source_name="<test>", source_format="json",
            expected_latest_version=0, actor="author",
        )
        self.answer = json.dumps(dsl(nodes=("collect",)))
        jobs = self.service()
        job = self.settled(jobs, self.modify(jobs)["job_id"])

        summary = job["result"]["change_summary"]
        self.assertEqual(
            [{"kind": "removed", "node_id": "fact_check", "label": "Fact Check"}],
            summary["entries"],
        )

    def test_a_step_that_kept_its_id_but_changed_is_reported_as_changed(self) -> None:
        """Otherwise swapping a step's Agent reads as "nothing changed"."""

        self.publish()
        renamed = dsl()
        renamed["nodes"][0]["label"] = "Gather the source material"
        self.answer = json.dumps(renamed)
        jobs = self.service()
        job = self.settled(jobs, self.modify(jobs)["job_id"])

        summary = job["result"]["change_summary"]
        self.assertEqual("diff", summary["source"])
        self.assertEqual(
            [{
                "kind": "changed", "node_id": "collect",
                "label": "Gather the source material",
            }],
            summary["entries"],
        )

    def test_an_unchanged_definition_reports_no_step_changes(self) -> None:
        self.publish()
        self.answer = json.dumps(dsl(name="Research, retitled"))
        jobs = self.service()
        job = self.settled(jobs, self.modify(jobs)["job_id"])

        summary = job["result"]["change_summary"]
        self.assertEqual([], summary["entries"])
        self.assertEqual(0, summary["edges_added"])

    def test_one_active_modification_per_workflow(self) -> None:
        self.publish()
        # A generator that blocks keeps the first job in flight while the
        # second request arrives, which is the race this rule exists for.
        released = False

        def slow(_prompt):
            while not released:
                time.sleep(0.01)
            return json.dumps(dsl(nodes=("collect", "fact_check")))

        authoring = self.authoring()
        authoring.generate_text = slow
        jobs = AuthoringJobService(self.path, authoring, self.definitions)
        first = self.modify(jobs, key="m1")
        try:
            with self.assertRaises(AuthoringJobConflict) as caught:
                self.modify(jobs, key="m2")
            self.assertEqual("draft_already_active", caught.exception.code)
            self.assertEqual(first["job_id"], caught.exception.job["job_id"])
        finally:
            released = True
        self.settled(jobs, first["job_id"])

    def test_a_cancelled_job_leaves_the_current_definition_alone(self) -> None:
        self.publish()
        released = False

        def slow(_prompt):
            while not released:
                time.sleep(0.01)
            return json.dumps(dsl(nodes=("collect", "fact_check"), name="Replaced"))

        authoring = self.authoring()
        authoring.generate_text = slow
        jobs = AuthoringJobService(self.path, authoring, self.definitions)
        created = self.modify(jobs)
        cancelled = jobs.cancel(created["job_id"], actor="author")
        released = True

        self.assertEqual("cancelled", cancelled["status"])
        self.assertEqual([], cancelled["allowed_commands"])
        # Whatever the Agent returns after the cancellation is discarded.
        time.sleep(0.2)
        self.assertEqual("cancelled", jobs.get(created["job_id"], actor="author")["status"])
        with connect_workflow_database(self.path, read_only=True) as connection:
            versions = connection.execute(
                "SELECT COUNT(*) AS total FROM workflow_versions"
            ).fetchone()["total"]
        self.assertEqual(1, versions)

    def test_regenerate_keeps_the_workflow_identity(self) -> None:
        self.publish()
        self.answer = json.dumps(dsl(nodes=("plan", "write"), name="Rebuilt"))
        jobs = self.service()
        job = self.settled(jobs, self.modify(jobs, mode="regenerate")["job_id"])

        self.assertEqual("done", job["status"])
        self.assertEqual("workflow:research", job["result"]["workflow_id"])
        self.assertEqual("Rebuilt", job["result"]["name"])

    def test_an_unknown_mode_is_refused_before_any_agent_runs(self) -> None:
        self.publish()
        jobs = self.service()
        with self.assertRaises(ValueError):
            jobs.create(
                actor="author", prompt="Add a step", idempotency_key="m1",
                workflow_id="workflow:research", mode="rewrite",
            )


class UnknownResultTests(AuthoringJobTestCase):
    """A silenced Agent is recorded as unresolved, never as a plain failure."""

    def audits(self, job_id):
        with connect_workflow_database(self.path, read_only=True) as connection:
            return [
                (row["action"], json.loads(row["details_json"] or "{}"))
                for row in connection.execute(
                    "SELECT action,details_json FROM audit_records WHERE target_id=?",
                    (job_id,),
                )
            ]

    def failing(self, error):
        def raise_it(_prompt):
            raise error

        authoring = self.authoring()
        authoring.generate_text = raise_it
        return AuthoringJobService(self.path, authoring, self.definitions)

    def test_a_silenced_agent_is_marked_unknown_and_audited_as_such(self) -> None:
        from orbit.workflow.authoring import AuthoringUnknownResultError

        jobs = self.failing(AuthoringUnknownResultError("cli exceeded its deadline"))
        created = jobs.create(actor="author", prompt="Research", idempotency_key="g1")
        job = self.settled(jobs, created["job_id"])

        self.assertEqual("failed", job["status"])
        self.assertEqual("unknown_external_result", job["error"]["code"])
        details = dict(self.audits(created["job_id"]))["workflow.authoring.complete"]
        self.assertIs(True, details["unknown_result"])

    def test_an_ordinary_failure_is_not_dressed_up_as_unknown(self) -> None:
        from orbit.workflow.authoring import AuthoringUnavailableError

        jobs = self.failing(AuthoringUnavailableError("generator CLI cannot run"))
        created = jobs.create(actor="author", prompt="Research", idempotency_key="g1")
        job = self.settled(jobs, created["job_id"])

        self.assertEqual("failed", job["status"])
        self.assertNotEqual("unknown_external_result", job["error"]["code"])
        details = dict(self.audits(created["job_id"]))["workflow.authoring.complete"]
        self.assertIs(False, details["unknown_result"])

    def test_cancelling_a_running_job_stops_the_agent_rather_than_waiting(self) -> None:
        """Discarding the answer is not enough: the CLI has to be told to stop."""

        from orbit.workflow.authoring.generator import active_scope

        stopped = threading.Event()
        started = threading.Event()

        class Handle:
            def cancel(self, *, grace_seconds=None):
                self.grace = grace_seconds
                stopped.set()

        handle = Handle()

        def slow(_prompt):
            # Stand in for the CLI child: report it, then block until stopped.
            active_scope().attach(handle)
            started.set()
            stopped.wait(timeout=10)
            return json.dumps(dsl())

        authoring = self.authoring()
        authoring.generate_text = slow
        jobs = AuthoringJobService(
            self.path, authoring, self.definitions, cancel_grace_seconds=2,
        )
        created = jobs.create(actor="author", prompt="Research", idempotency_key="g1")
        self.assertTrue(started.wait(timeout=5))

        cancelled = jobs.cancel(created["job_id"], actor="author")

        self.assertTrue(stopped.is_set())
        self.assertEqual(2, handle.grace)
        self.assertEqual("cancelled", cancelled["status"])
        details = dict(self.audits(created["job_id"]))["workflow.authoring.cancel"]
        # The Agent was mid-call, so what it had already done is unknowable.
        self.assertIs(True, details["unknown_result"])

    def test_deadline_expires_without_a_reader_and_stops_the_agent(self) -> None:
        """The watchdog, not a GET/list poll, owns deadline enforcement."""

        from orbit.workflow.authoring.generator import active_scope

        stopped = threading.Event()
        started = threading.Event()
        now = [datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)]

        class Handle:
            def cancel(self, *, grace_seconds=None):
                self.grace = grace_seconds
                stopped.set()

        handle = Handle()

        def slow(_prompt):
            active_scope().attach(handle)
            started.set()
            stopped.wait(timeout=10)
            return json.dumps(dsl())

        authoring = self.authoring()
        authoring.generate_text = slow
        jobs = AuthoringJobService(
            self.path, authoring, self.definitions, timeout_seconds=30,
            cancel_grace_seconds=2, clock=lambda: now[0],
        )
        created = jobs.create(actor="author", prompt="Research", idempotency_key="g1")
        self.assertTrue(started.wait(timeout=5))

        # Invoke what the independently scheduled Timer invokes, after moving
        # the deterministic clock past the stored deadline.
        now[0] += timedelta(seconds=31)
        with jobs._scope_lock:
            watchdog = jobs._deadline_timers[created["job_id"]]
        watchdog.function()

        self.assertTrue(stopped.is_set())
        self.assertEqual(2, handle.grace)
        with connect_workflow_database(self.path, read_only=True) as connection:
            row = connection.execute(
                "SELECT status,error_code FROM workflow_authoring_jobs WHERE job_id=?",
                (created["job_id"],),
            ).fetchone()
        self.assertEqual(("failed", "authoring_timeout"), tuple(row))

    def test_recovered_deadline_survives_removing_the_timeout_config(self) -> None:
        """Persisted policy is not revoked by a later process configuration."""

        jobs = self.service()
        created = jobs.create(actor="author", prompt="Research", idempotency_key="g1")
        self.settled(jobs, created["job_id"])
        expired = datetime.now(timezone.utc) - timedelta(seconds=1)
        with connect_workflow_database(self.path) as connection:
            connection.execute(
                "UPDATE workflow_authoring_jobs SET status='running',"
                " cancel_requested=0,deadline_at=? WHERE job_id=?",
                (jobs._time(expired), created["job_id"]),
            )
            connection.commit()

        self.assertIsNone(jobs.timeout_seconds)
        jobs._expire_due()

        job = jobs.get(created["job_id"], actor="author")
        self.assertEqual("failed", job["status"])
        self.assertEqual("authoring_timeout", job["error"]["code"])

    def test_cancelling_a_queued_job_claims_no_unknown_effect(self) -> None:
        release = threading.Event()

        def blocked(_prompt):
            release.wait(timeout=10)
            return json.dumps(dsl())

        authoring = self.authoring()
        authoring.generate_text = blocked
        jobs = AuthoringJobService(self.path, authoring, self.definitions)
        first = jobs.create(actor="author", prompt="Research", idempotency_key="g1")
        try:
            with connect_workflow_database(self.path) as connection:
                # Force the queued shape deterministically rather than racing
                # the worker thread for it.
                connection.execute(
                    "UPDATE workflow_authoring_jobs SET status='queued'"
                    " WHERE job_id=?",
                    (first["job_id"],),
                )
                connection.commit()
            cancelled = jobs.cancel(first["job_id"], actor="author")
            self.assertEqual("cancelled", cancelled["status"])
            details = dict(self.audits(first["job_id"]))["workflow.authoring.cancel"]
            self.assertIs(False, details["unknown_result"])
        finally:
            release.set()


class ChangeSummaryCleaningTests(unittest.TestCase):
    """The Agent's summary is data, not instructions: it is filtered."""

    def test_entries_are_kept_only_when_they_name_a_node_that_exists(self) -> None:
        cleaned = _clean_change_summary(
            [
                {"kind": "added", "node_id": "known", "label": "Known"},
                {"kind": "added", "node_id": "ghost", "label": "Ghost"},
                {"kind": "removed", "node_id": "gone", "label": "Gone"},
            ],
            frozenset({"known"}),
        )
        self.assertEqual(
            [("added", "known"), ("removed", "gone")],
            [(item["kind"], item["node_id"]) for item in cleaned],
        )

    def test_malformed_entries_are_dropped_rather_than_repaired(self) -> None:
        for value in (
            "not a list",
            [{"kind": "invented", "node_id": "known", "label": "Known"}],
            [{"kind": "added", "node_id": "known"}],
            [{"kind": "added", "label": "No id"}],
            [None, 7],
        ):
            with self.subTest(value=value):
                self.assertEqual((), _clean_change_summary(value, frozenset({"known"})))

    def test_long_text_is_bounded(self) -> None:
        cleaned = _clean_change_summary(
            [{
                "kind": "changed", "node_id": "known",
                "label": "L" * 500, "detail": "D" * 500,
            }],
            frozenset({"known"}),
        )
        self.assertLessEqual(len(cleaned[0]["label"]), 200)
        self.assertLessEqual(len(cleaned[0]["detail"]), 200)

    def test_the_entry_count_is_capped(self) -> None:
        cleaned = _clean_change_summary(
            [
                {"kind": "added", "node_id": "known", "label": f"Step {index}"}
                for index in range(50)
            ],
            frozenset({"known"}),
        )
        self.assertLessEqual(len(cleaned), 12)


class GenerationOutcomeTests(unittest.TestCase):
    def test_a_generation_outcome_defaults_to_no_summary(self) -> None:
        outcome = GenerationOutcome(
            source="{}", workflow_id="workflow:x", definition_hash="sha256:x",
            node_count=1, attempts=1,
        )
        self.assertEqual((), outcome.change_summary)


if __name__ == "__main__":
    unittest.main()
