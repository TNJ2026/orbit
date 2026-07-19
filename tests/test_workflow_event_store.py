from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from orbit.workflow.domain.concurrency import CommandDisposition
from orbit.workflow.domain.envelopes import CommandEnvelope, EventEnvelope
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.persistence import (
    AttemptRecord,
    BranchTokenRecord,
    ConcurrencyConflictError,
    DuplicateEventIdError,
    EventSequenceError,
    ExecutionPlanRecord,
    IdempotencyConflictError,
    NodeRunRecord,
)
from orbit.workflow.domain.serialization import definition_hash
from orbit.workflow.domain.states import (
    AttemptStatus,
    BranchTokenStatus,
    NodeRunStatus,
    WorkflowRunStatus,
)
from orbit.workflow.domain.versions import AggregateVersion, Revision, SchemaVersion
from orbit.workflow.persistence import SQLiteEventStore, SQLiteReadSession, SQLiteUnitOfWork
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.migrations import migrate_workflow_database


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


class WorkflowEventStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "events.db"
        with connect_workflow_database(self.path) as connection:
            migrate_workflow_database(connection)
        with SQLiteUnitOfWork(self.path) as uow:
            uow.connection.execute(
                "INSERT INTO workflow_definitions VALUES (?, ?, ?, ?)",
                ("workflow:flow", "Flow", "2026-07-17T00:00:00Z", "test"),
            )
            uow.connection.execute(
                """
                INSERT INTO workflow_versions VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    "workflow:flow", 1, "sha256:" + "a" * 64, "1.0", "1.0", "1.0",
                    "{}", "json", None, "sha256:" + "b" * 64,
                    "2026-07-17T00:00:00Z", "test",
                ),
            )
            uow.connection.execute(
                """
                INSERT INTO workflow_runs (
                    run_id, workflow_id, workflow_version, definition_hash,
                    status, aggregate_version, correlation_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "run:r1", "workflow:flow", 1, "sha256:" + "a" * 64,
                    "created", 0, "run:r1", "2026-07-17T00:00:00Z",
                    "2026-07-17T00:00:00Z",
                ),
            )
            uow.commit()
        self.run_id = EntityId("run", "r1")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def command(self, *, payload=None, key="start") -> CommandEnvelope:
        return CommandEnvelope(
            EntityId("command", "c1"), "start_run", self.run_id, self.run_id,
            AggregateVersion(0), key, "test", NOW, payload or {},
        )

    def event(self, command: CommandEnvelope, *, event_id="e1", sequence=1, aggregate=None):
        return EventEnvelope(
            EntityId("event", event_id), "run_started", Revision(1),
            aggregate or command.aggregate_id, Revision(sequence), command.correlation_id,
            command.command_id, NOW, {"status": "running"},
        )

    def test_append_and_read_preserve_aggregate_and_global_order(self) -> None:
        command = self.command()
        event = self.event(command)
        with SQLiteUnitOfWork(self.path) as uow:
            stored = uow.events.append(
                self.run_id, self.run_id, AggregateVersion(0), (event,)
            )
            uow.commit()
        self.assertEqual(1, stored[0].global_position)
        with SQLiteReadSession(self.path) as connection:
            store = SQLiteEventStore(connection)
            stream = store.read_stream(self.run_id)
            run_events = store.read_run(self.run_id)
            all_events = store.read_all()
        self.assertEqual((event,), tuple(item.envelope for item in stream))
        self.assertEqual(stream, run_events)
        self.assertEqual(stream, all_events)

    def test_sequence_expected_version_and_event_id_are_enforced(self) -> None:
        command = self.command()
        with SQLiteUnitOfWork(self.path) as uow:
            with self.assertRaises(EventSequenceError):
                uow.events.append(
                    self.run_id, self.run_id, AggregateVersion(0),
                    (self.event(command, sequence=2),),
                )
        with SQLiteUnitOfWork(self.path) as uow:
            uow.events.append(
                self.run_id, self.run_id, AggregateVersion(0),
                (self.event(command),),
            )
            uow.commit()
        with SQLiteUnitOfWork(self.path) as uow:
            with self.assertRaises(ConcurrencyConflictError):
                uow.events.append(
                    self.run_id, self.run_id, AggregateVersion(0),
                    (self.event(command, event_id="e2"),),
                )
        other = EntityId("node_run", "n1")
        with SQLiteUnitOfWork(self.path) as uow:
            with self.assertRaises(DuplicateEventIdError):
                uow.events.append(
                    self.run_id, other, AggregateVersion(0),
                    (self.event(command, aggregate=other),),
                )

    def test_event_rows_cannot_be_updated_or_deleted(self) -> None:
        command = self.command()
        with SQLiteUnitOfWork(self.path) as uow:
            uow.events.append(self.run_id, self.run_id, AggregateVersion(0), (self.event(command),))
            uow.commit()
        with connect_workflow_database(self.path) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE run_events SET event_type = 'changed'")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM run_events")

    def test_receipt_replays_after_restart_and_rejects_conflicting_payload(self) -> None:
        command = self.command()
        event = self.event(command)
        with SQLiteUnitOfWork(self.path) as uow:
            uow.events.append(self.run_id, self.run_id, AggregateVersion(0), (event,))
            uow.receipts.record(self.run_id, command, (event.event_id,), NOW)
            uow.commit()
        with SQLiteUnitOfWork(self.path) as restarted:
            decision = restarted.receipts.decide(command)
            self.assertEqual(CommandDisposition.REPLAY_PRIOR_RESULT, decision.disposition)
            self.assertEqual((event.event_id,), decision.prior_event_ids)
            conflict = self.command(payload={"different": True})
            with self.assertRaises(IdempotencyConflictError):
                restarted.receipts.decide(conflict)

    def test_event_and_receipt_roll_back_together_on_fault(self) -> None:
        command = self.command()
        event = self.event(command)
        for kill_point in (
            "before_event_insert", "after_event_insert",
            "before_receipt_insert", "after_receipt_insert",
        ):
            def fail(point: str, expected=kill_point) -> None:
                if point == expected:
                    raise RuntimeError("injected")

            with self.subTest(kill_point=kill_point), self.assertRaises(RuntimeError):
                with SQLiteUnitOfWork(self.path, fault_hook=fail) as uow:
                    uow.events.append(self.run_id, self.run_id, AggregateVersion(0), (event,))
                    uow.receipts.record(self.run_id, command, (event.event_id,), NOW)
                    uow.commit()
            with SQLiteReadSession(self.path) as connection:
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM run_events").fetchone()[0])
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0])

    def test_projection_repositories_share_event_transaction_and_allow_rework(self) -> None:
        command = self.command()
        event = self.event(command)
        plan_value = {"nodes": ["collect"]}
        with SQLiteUnitOfWork(self.path) as uow:
            current = uow.runs.get(self.run_id)
            self.assertEqual(WorkflowRunStatus.CREATED, current.status)
            uow.events.append(self.run_id, self.run_id, AggregateVersion(0), (event,))
            running = replace(
                current,
                status=WorkflowRunStatus.RUNNING,
                aggregate_version=AggregateVersion(1),
            )
            uow.runs.update(running, AggregateVersion(0))
            plan = ExecutionPlanRecord(
                EntityId("plan", "p1"), self.run_id, Revision(1),
                EntityId("workflow", "flow"), Revision(1), SchemaVersion("1.0"),
                plan_value, definition_hash(plan_value), event.event_id, NOW,
            )
            uow.plans.append(plan)
            first = NodeRunRecord(
                EntityId("node_run", "n1"), self.run_id, "collect", Revision(1),
                NodeRunStatus.PENDING, AggregateVersion(0), NOW, NOW,
            )
            rework = replace(first, node_run_id=EntityId("node_run", "n2"))
            uow.node_runs.create(first)
            uow.node_runs.create(rework)
            attempt = AttemptRecord(
                EntityId("attempt", "a1"), first.node_run_id, Revision(1),
                AttemptStatus.CREATED, AggregateVersion(0), NOW, NOW,
            )
            uow.attempts.create(attempt)
            token = BranchTokenRecord(
                EntityId("branch_token", "t1"), self.run_id, first.node_run_id,
                BranchTokenStatus.ACTIVE, AggregateVersion(0), {}, NOW, NOW,
            )
            uow.tokens.create(token)
            uow.commit()

        with SQLiteUnitOfWork(self.path) as uow:
            self.assertEqual(WorkflowRunStatus.RUNNING, uow.runs.get(self.run_id).status)
            self.assertEqual(1, len(uow.plans.list_versions(self.run_id)))
            self.assertEqual(2, len(uow.node_runs.list_by_run(self.run_id)))
            self.assertEqual(1, len(uow.attempts.list_by_node_run(EntityId("node_run", "n1"))))
            self.assertEqual(1, len(uow.tokens.list_by_run(self.run_id, active_only=True)))
            with self.assertRaises(ConcurrencyConflictError) as raised:
                uow.runs.update(running, AggregateVersion(0))
            self.assertEqual(1, raised.exception.actual)

    def test_concurrent_expected_version_and_idempotency_have_one_winner(self) -> None:
        command = self.command()
        event = self.event(command)
        first_appended = threading.Event()
        second_started = threading.Event()
        outcomes = []

        def first_writer():
            with SQLiteUnitOfWork(self.path) as uow:
                uow.events.append(self.run_id, self.run_id, AggregateVersion(0), (event,))
                uow.receipts.record(self.run_id, command, (event.event_id,), NOW)
                first_appended.set()
                second_started.wait(2)
                uow.commit()
                outcomes.append("committed")

        def second_writer():
            first_appended.wait(2)
            second_started.set()
            with SQLiteUnitOfWork(self.path) as uow:
                decision = uow.receipts.decide(command)
                outcomes.append(decision.disposition.value)

        threads = [threading.Thread(target=first_writer), threading.Thread(target=second_writer)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertCountEqual(["committed", "replay_prior_result"], outcomes)
        with SQLiteReadSession(self.path) as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM run_events").fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0])

    def test_projection_and_token_kill_points_roll_back(self) -> None:
        command = self.command()
        event = self.event(command)
        plan_value = {"nodes": ["collect"]}
        with SQLiteUnitOfWork(self.path) as uow:
            uow.events.append(self.run_id, self.run_id, AggregateVersion(0), (event,))
            uow.plans.append(ExecutionPlanRecord(
                EntityId("plan", "base"), self.run_id, Revision(1),
                EntityId("workflow", "flow"), Revision(1), SchemaVersion("1.0"),
                plan_value, definition_hash(plan_value), event.event_id, NOW,
            ))
            uow.commit()

        for number, kill_point in enumerate(
            ("before_node_run_create", "after_node_run_create"), 1
        ):
            node = NodeRunRecord(
                EntityId("node_run", f"fault{number}"), self.run_id, "collect", Revision(1),
                NodeRunStatus.PENDING, AggregateVersion(0), NOW, NOW,
            )
            with self.subTest(kill_point=kill_point), self.assertRaises(RuntimeError):
                with SQLiteUnitOfWork(
                    self.path,
                    fault_hook=lambda point, expected=kill_point: (
                        (_ for _ in ()).throw(RuntimeError("injected"))
                        if point == expected else None
                    ),
                ) as uow:
                    uow.node_runs.create(node)
                    uow.commit()
        with SQLiteUnitOfWork(self.path) as uow:
            node = NodeRunRecord(
                EntityId("node_run", "base"), self.run_id, "collect", Revision(1),
                NodeRunStatus.PENDING, AggregateVersion(0), NOW, NOW,
            )
            uow.node_runs.create(node)
            uow.commit()
        for number, kill_point in enumerate(("before_token_create", "after_token_create"), 1):
            token = BranchTokenRecord(
                EntityId("branch_token", f"fault{number}"), self.run_id, node.node_run_id,
                BranchTokenStatus.ACTIVE, AggregateVersion(0), {}, NOW, NOW,
            )
            with self.subTest(kill_point=kill_point), self.assertRaises(RuntimeError):
                with SQLiteUnitOfWork(
                    self.path,
                    fault_hook=lambda point, expected=kill_point: (
                        (_ for _ in ()).throw(RuntimeError("injected"))
                        if point == expected else None
                    ),
                ) as uow:
                    uow.tokens.create(token)
                    uow.commit()
        with SQLiteReadSession(self.path) as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM node_runs").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM branch_tokens").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
    ExecutionPlanRecord,
    NodeRunRecord,
    WorkflowRunRecord,
