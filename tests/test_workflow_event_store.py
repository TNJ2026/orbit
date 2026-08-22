"""The append-side guarantees of the event log, which only refusals express.

`SQLiteEventStore` is what three application services write their control
events through. What covered it was those services succeeding, and a
successful append walks none of the checks that make the log a log: the
optimistic version, the sequence, the duplicate, the payload ceiling, the
requirement that a caller already hold a transaction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from orbit.workflow.domain.envelopes import EventEnvelope
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.persistence import (
    PERSISTENCE_ERROR_REGISTRY, ConcurrencyConflictError, DuplicateEventIdError,
    EventSequenceError, PersistenceError,
)
from orbit.workflow.domain.serialization import freeze_json
from orbit.workflow.domain.versions import AggregateVersion, Revision
from orbit.workflow.persistence.event_store import (
    MAX_EVENT_PAYLOAD_BYTES, MAX_EVENTS_PER_APPEND, SQLiteEventStore,
)
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.migrations import migrate_workflow_database

RUN = EntityId("run", "a" * 32)
AGG = EntityId("workflow", "b" * 32)
WHEN = datetime(2026, 1, 1, tzinfo=timezone.utc)


def event(sequence: int, *, event_id: str | None = None, aggregate=AGG, payload=None):
    return EventEnvelope(
        EntityId("event", event_id or f"{sequence:032x}"),
        "workflow_published", Revision(1), aggregate, Revision(sequence), RUN,
        EntityId("command", "c" * 32), WHEN, freeze_json(payload or {"n": sequence}),
    )


class EventStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.directory.cleanup)
        self.opened = connect_workflow_database(
            Path(self.directory.name) / "events.sqlite3",
        )
        self.connection = self.opened.__enter__()
        self.addCleanup(self.opened.__exit__, None, None, None)
        migrate_workflow_database(self.connection)
        # The log keys to a run, a run to the version it executes, and a
        # version to the workflow it belongs to. All three come first.
        self.connection.execute(
            "INSERT INTO workflow_definitions(workflow_id,name,created_at,created_by)"
            " VALUES (?,?,?,'test')",
            ("workflow:test", "Event store fixture", WHEN.isoformat()),
        )
        self.connection.execute(
            "INSERT INTO workflow_versions(workflow_id,version,definition_hash,"
            "dsl_version,ir_version,compiler_version,canonical_ir_json,"
            "source_format,catalog_fingerprint,created_at,created_by)"
            " VALUES (?,1,?,'1.3','1.0','1.0','{}','json',?,?,'test')",
            ("workflow:test", "sha256:" + "0" * 64, "sha256:" + "1" * 64,
             WHEN.isoformat()),
        )
        self.connection.execute(
            "INSERT INTO workflow_runs(run_id,workflow_id,workflow_version,"
            "definition_hash,status,aggregate_version,correlation_id,"
            "created_at,updated_at) VALUES (?,?,1,?,'running',0,?,?,?)",
            (str(RUN), "workflow:test", "sha256:" + "0" * 64,
             str(RUN),
             WHEN.isoformat(), WHEN.isoformat()),
        )
        self.connection.commit()
        self.store = SQLiteEventStore(self.connection)

    def begin(self):
        self.connection.execute("BEGIN IMMEDIATE")

    def test_an_empty_stream_starts_at_zero(self) -> None:
        self.assertEqual(AggregateVersion(0), self.store.stream_head(AGG))

    def test_appending_advances_the_head_and_reads_back(self) -> None:
        self.begin()
        stored = self.store.append(RUN, AGG, AggregateVersion(0), (event(1), event(2)))
        self.connection.commit()

        self.assertEqual(2, len(stored))
        self.assertEqual(AggregateVersion(2), self.store.stream_head(AGG))
        self.assertEqual(
            [1, 2], [item.envelope.sequence.value for item in self.store.read_stream(AGG)],
        )

    def test_an_append_outside_a_transaction_is_refused(self) -> None:
        """The log and the state it describes commit together or not at all."""

        with self.assertRaisesRegex(RuntimeError, "active UnitOfWork"):
            self.store.append(RUN, AGG, AggregateVersion(0), (event(1),))

    def test_an_append_must_carry_at_least_one_event(self) -> None:
        self.begin()
        with self.assertRaisesRegex(ValueError, "at least one event"):
            self.store.append(RUN, AGG, AggregateVersion(0), ())

    def test_an_append_may_not_exceed_the_batch_limit(self) -> None:
        self.begin()
        too_many = tuple(event(n) for n in range(1, MAX_EVENTS_PER_APPEND + 2))
        with self.assertRaisesRegex(ValueError, "exceeds limit"):
            self.store.append(RUN, AGG, AggregateVersion(0), too_many)

    def test_a_stale_expected_version_is_a_concurrency_conflict(self) -> None:
        self.begin()
        self.store.append(RUN, AGG, AggregateVersion(0), (event(1),))
        self.connection.commit()

        self.begin()
        with self.assertRaises(ConcurrencyConflictError):
            self.store.append(RUN, AGG, AggregateVersion(0), (event(2),))

    def test_a_gap_in_the_sequence_is_refused(self) -> None:
        self.begin()
        with self.assertRaisesRegex(EventSequenceError, "expected event sequence 1"):
            self.store.append(RUN, AGG, AggregateVersion(0), (event(2),))

    def test_an_event_for_another_aggregate_cannot_ride_along(self) -> None:
        self.begin()
        stranger = event(1, aggregate=EntityId("workflow", "d" * 32))
        with self.assertRaisesRegex(EventSequenceError, "does not match append stream"):
            self.store.append(RUN, AGG, AggregateVersion(0), (stranger,))

    def test_a_repeated_event_id_is_named_as_a_duplicate(self) -> None:
        """Two different events with one id would make the log ambiguous."""

        self.begin()
        self.store.append(RUN, AGG, AggregateVersion(0), (event(1, event_id="e" * 32),))
        self.connection.commit()

        self.begin()
        with self.assertRaises(DuplicateEventIdError):
            self.store.append(
                RUN, AGG, AggregateVersion(1), (event(2, event_id="e" * 32),),
            )

    def test_a_payload_over_the_ceiling_is_refused(self) -> None:
        self.begin()
        fat = event(1, payload={"blob": "x" * (MAX_EVENT_PAYLOAD_BYTES + 10)})
        with self.assertRaisesRegex(ValueError, "payload exceeds"):
            self.store.append(RUN, AGG, AggregateVersion(0), (fat,))

    def test_reads_are_scoped_to_what_was_asked_for(self) -> None:
        other = EntityId("workflow", "f" * 32)
        self.begin()
        self.store.append(RUN, AGG, AggregateVersion(0), (event(1),))
        self.store.append(RUN, other, AggregateVersion(0), (event(1, aggregate=other,
                                                                  event_id="a1" + "0" * 30),))
        self.connection.commit()

        self.assertEqual(1, len(self.store.read_stream(AGG)))
        self.assertEqual(2, len(self.store.read_run(RUN)))
        self.assertEqual(2, len(self.store.read_all()))


class ErrorRegistryTests(unittest.TestCase):
    """The mapping the README calls stable, pinned so that it is.

    `PERSISTENCE_ERROR_REGISTRY` is the vocabulary a driver exception is
    translated into before it reaches the Kernel. Nothing imports it — it is a
    declaration, like the contract-stability table — so a rename would have
    gone unnoticed by every test until the code it names was needed.
    """

    def test_every_code_maps_to_a_persistence_error(self) -> None:
        self.assertTrue(PERSISTENCE_ERROR_REGISTRY)
        for code, error in PERSISTENCE_ERROR_REGISTRY.items():
            with self.subTest(code=code):
                self.assertRegex(code, r"^[A-Z][A-Z_]*$")
                self.assertTrue(issubclass(error, PersistenceError))

    def test_the_codes_the_store_actually_raises_are_registered(self) -> None:
        registered = set(PERSISTENCE_ERROR_REGISTRY.values())
        for error in (
            ConcurrencyConflictError, DuplicateEventIdError, EventSequenceError,
        ):
            with self.subTest(error=error.__name__):
                self.assertIn(error, registered)

    def test_no_two_codes_name_the_same_error(self) -> None:
        values = list(PERSISTENCE_ERROR_REGISTRY.values())
        self.assertEqual(len(values), len(set(values)))


if __name__ == "__main__":
    unittest.main()
