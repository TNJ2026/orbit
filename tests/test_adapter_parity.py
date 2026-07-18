"""M7 gate 2: the memory adapter and the SQLite adapter agree.

`MemoryUnitOfWork` exists so the kernel contract can run without SQLite. That
is only worth having if the two produce the *same* history — otherwise tests
written against memory prove nothing about production, and the fast adapter
becomes a place where bugs hide.

So: drive one command sequence through both, and compare the event stream and
the resulting projections field by field. Anything the two disagree about is
either a bug in an adapter or a leak of storage detail into the domain.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from orbit.workflow.application.durable_runtime_service import (
    DurableRuntimeApplicationService,
)
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.persistence.memory import MemoryRuntimeDatabase, MemoryUnitOfWork
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.migrations import migrate_workflow_database
from tests.test_web_composition import publish_linear_workflow, start_run_command


RUN = EntityId("run", "f" * 64)

# Storage bookkeeping, not domain facts: the two adapters are allowed to
# disagree here and nowhere else.
VOLATILE_EVENT_FIELDS = frozenset({"global_position", "stored_at", "rowid"})


def build(path: Path, *, in_memory: bool):
    """The same service, pointed at either store.

    The factory is injected at construction because the kernel, scheduler and
    recovery scanner capture it there; reassigning the attribute afterwards
    leaves them writing to SQLite, and the parity test would pass while
    comparing SQLite against itself.
    """

    if not in_memory:
        return DurableRuntimeApplicationService(path)
    database = MemoryRuntimeDatabase()
    service = DurableRuntimeApplicationService(
        path, uow_factory=lambda: MemoryUnitOfWork(database)
    )
    service.memory_database = database
    return service


def event_facts(uow_factory, run_id: EntityId) -> list[dict]:
    """The domain-visible shape of a run's event stream."""

    with uow_factory() as uow:
        events = uow.events.read_stream(run_id)
    return [
        {
            "event_type": stored.envelope.event_type,
            "event_version": stored.envelope.event_version.value,
            "aggregate_id": str(stored.envelope.aggregate_id),
            "sequence": stored.envelope.sequence.value,
            "correlation_id": str(stored.envelope.correlation_id),
            "causation_id": str(stored.envelope.causation_id),
            "payload": stored.envelope.payload,
        }
        for stored in events
    ]


class AdapterParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)

        self.sqlite_path = self.dir / "sqlite.db"
        connection = connect_workflow_database(self.sqlite_path)
        migrate_workflow_database(connection)
        connection.close()
        _workflow_id, self.digest = publish_linear_workflow(self.sqlite_path)

        # The memory adapter still reads workflow versions from SQLite: the
        # published definition is not runtime state, so it is deliberately not
        # part of what the two adapters have to duplicate.
        self.memory_path = self.sqlite_path

    def tearDown(self) -> None:
        self.temp.cleanup()

    def submit_start(self, service):
        return service.submit(start_run_command(RUN, self.digest))

    def test_both_adapters_accept_the_same_command(self) -> None:
        sqlite = self.submit_start(build(self.sqlite_path, in_memory=False))
        memory = self.submit_start(build(self.memory_path, in_memory=True))

        self.assertEqual(sqlite.disposition, memory.disposition)
        self.assertEqual(
            [d.code for d in sqlite.diagnostics], [d.code for d in memory.diagnostics]
        )
        self.assertEqual(len(sqlite.event_ids), len(memory.event_ids))

    def test_both_adapters_write_the_same_events(self) -> None:
        sqlite_service = build(self.sqlite_path, in_memory=False)
        self.submit_start(sqlite_service)
        memory_service = build(self.memory_path, in_memory=True)
        self.submit_start(memory_service)

        sqlite_facts = event_facts(sqlite_service.uow_factory, RUN)
        memory_facts = event_facts(memory_service.uow_factory, RUN)

        self.assertTrue(sqlite_facts, "no events were written at all")
        self.assertEqual(
            [f["event_type"] for f in sqlite_facts],
            [f["event_type"] for f in memory_facts],
        )
        self.assertEqual(sqlite_facts, memory_facts)

    def test_both_adapters_produce_the_same_run_projection(self) -> None:
        sqlite_service = build(self.sqlite_path, in_memory=False)
        self.submit_start(sqlite_service)
        memory_service = build(self.memory_path, in_memory=True)
        self.submit_start(memory_service)

        with sqlite_service.uow_factory() as uow:
            from_sqlite = uow.runs.get(RUN)
        with memory_service.uow_factory() as uow:
            from_memory = uow.runs.get(RUN)

        self.assertIsNotNone(from_sqlite)
        self.assertEqual(from_sqlite, from_memory)

    def test_both_adapters_produce_the_same_plan(self) -> None:
        sqlite_service = build(self.sqlite_path, in_memory=False)
        self.submit_start(sqlite_service)
        memory_service = build(self.memory_path, in_memory=True)
        self.submit_start(memory_service)

        from orbit.workflow.domain.versions import Revision

        with sqlite_service.uow_factory() as uow:
            plan_a = uow.plans.get(RUN, Revision(1))
        with memory_service.uow_factory() as uow:
            plan_b = uow.plans.get(RUN, Revision(1))

        self.assertIsNotNone(plan_a)
        self.assertEqual(plan_a, plan_b)

    def test_both_adapters_replay_a_command_identically(self) -> None:
        """Receipt-first idempotency has to behave the same in both stores."""

        for in_memory in (False, True):
            with self.subTest(in_memory=in_memory):
                service = build(
                    self.memory_path if in_memory else self.sqlite_path,
                    in_memory=in_memory,
                )
                first = self.submit_start(service)
                second = self.submit_start(service)
                self.assertEqual("applied", first.disposition.value)
                self.assertEqual("replayed", second.disposition.value)
                self.assertEqual(first.event_ids, second.event_ids)

    def test_both_adapters_schedule_the_same_first_job(self) -> None:
        sqlite_service = build(self.sqlite_path, in_memory=False)
        self.submit_start(sqlite_service)
        memory_service = build(self.memory_path, in_memory=True)
        self.submit_start(memory_service)

        def job_shapes(uow_factory):
            with uow_factory() as uow:
                jobs = uow.jobs.list_by_run(RUN)
            return sorted(
                (job.job_kind, str(job.node_run_id), job.status.value) for job in jobs
            )

        self.assertEqual(
            job_shapes(sqlite_service.uow_factory), job_shapes(memory_service.uow_factory)
        )


if __name__ == "__main__":
    unittest.main()
