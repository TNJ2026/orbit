"""M7 gate 8: concurrency and capacity.

Thirty-two workers against one SQLite file, a long timeline, and a wide graph.
These are the shapes where a durable runtime stops being correct in the way a
unit test would notice: a job claimed twice, a paged read that skips or repeats
a row, a projection that drifts from the log.

They are deliberately modest in absolute size — the point is the invariant
under contention, not a benchmark number.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from orbit.web.app import RuntimeComposition
from orbit.workflow.api.read_models import ReadModelService
from orbit.workflow.application.run_service import RunApplicationService
from orbit.workflow.persistence.database import connect_workflow_database
from tests.test_web_composition import (
    SCHEMAS, publish_linear_workflow, transform_registration,
)


WORKER_COUNT = 32
RUN_COUNT = 24


class CapacityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "runtime.db"
        self.composition = RuntimeComposition(
            self.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            worker_count=WORKER_COUNT, poll_seconds=0.01,
        )
        publish_linear_workflow(self.db)
        self.runs = RunApplicationService(self.db, self.composition.service)
        self.reads = ReadModelService(self.db)

    def tearDown(self) -> None:
        self.composition.stop()
        self.temp.cleanup()

    def start(self, index: int) -> str:
        return self.runs.start_run(
            workflow_id="workflow:linear", inputs={"value": index},
            actor="capacity", idempotency_key=f"capacity-{index}",
        ).run_id

    def drain(self, timeout: float = 120) -> None:
        """Run the workers until every run reaches a terminal state."""

        import time

        self.composition.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with connect_workflow_database(self.db, read_only=True) as connection:
                pending = connection.execute(
                    "SELECT COUNT(*) FROM workflow_runs"
                    " WHERE status NOT IN ('succeeded','failed','cancelled')"
                ).fetchone()[0]
            if not pending:
                return
            time.sleep(0.05)
        self.fail(f"{pending} runs never finished")


class WorkerConcurrencyTests(CapacityTestCase):
    def test_thirty_two_workers_finish_every_run_exactly_once(self) -> None:
        run_ids = [self.start(index) for index in range(RUN_COUNT)]
        self.drain()

        with connect_workflow_database(self.db, read_only=True) as connection:
            statuses = dict(
                connection.execute("SELECT run_id, status FROM workflow_runs").fetchall()
            )
            # The decisive invariant: a job claimed twice would produce a second
            # attempt for the same node run.
            duplicates = connection.execute(
                "SELECT node_run_id, attempt_number, COUNT(*) c FROM node_attempts"
                " GROUP BY node_run_id, attempt_number HAVING c > 1"
            ).fetchall()

        self.assertEqual(RUN_COUNT, len(statuses))
        self.assertEqual({"succeeded"}, set(statuses.values()))
        self.assertEqual([], duplicates, "the same attempt was recorded twice")
        for run_id in run_ids:
            self.assertEqual("succeeded", statuses[run_id])

    def test_concurrent_starts_of_one_key_produce_one_run(self) -> None:
        """Idempotency has to hold under real contention, not just in sequence."""

        def start_same():
            return self.runs.start_run(
                workflow_id="workflow:linear", inputs={"value": 1},
                actor="capacity", idempotency_key="shared-key",
            ).run_id

        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(lambda _: start_same(), range(8)))

        self.assertEqual(1, len(set(ids)))
        with connect_workflow_database(self.db, read_only=True) as connection:
            count = connection.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0]
        self.assertEqual(1, count)

    def test_every_background_loop_survives_the_load(self) -> None:
        for index in range(RUN_COUNT):
            self.start(index)
        self.drain()

        for loop in self.composition.loops:
            status = loop.status()
            with self.subTest(loop=status["name"]):
                self.assertTrue(status["alive"])
                self.assertIsNone(status["last_error"])

    def test_shutdown_leaves_no_straggler(self) -> None:
        for index in range(8):
            self.start(index)
        self.drain()
        self.assertEqual([], self.composition.stop(timeout=30))


class PaginationUnderLoadTests(CapacityTestCase):
    def test_a_long_timeline_pages_without_gaps_or_repeats(self) -> None:
        run_id = self.start(0)
        self.drain()

        from orbit.workflow.domain.ids import EntityId

        identifier = EntityId.parse(run_id)
        seen: list[int] = []
        cursor = None
        for _ in range(200):
            items, cursor = self.reads.timeline(identifier, cursor=cursor, limit=3)
            seen.extend(item["position"] for item in items)
            if cursor is None:
                break
        else:
            self.fail("timeline paging never terminated")

        self.assertEqual(sorted(seen), seen, "pages came back out of order")
        self.assertEqual(len(seen), len(set(seen)), "a row was returned twice")

        with connect_workflow_database(self.db, read_only=True) as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM run_events WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
        self.assertEqual(total, len(seen), "paging lost rows")

    def test_the_run_list_pages_over_many_runs(self) -> None:
        for index in range(RUN_COUNT):
            self.start(index)

        seen: list[str] = []
        cursor = None
        for _ in range(100):
            items, cursor = self.reads.list_runs(cursor=cursor, limit=5)
            seen.extend(item["run_id"] for item in items)
            if cursor is None:
                break
        else:
            self.fail("run list paging never terminated")

        self.assertEqual(RUN_COUNT, len(seen))
        self.assertEqual(RUN_COUNT, len(set(seen)))


class ProjectionConsistencyTests(CapacityTestCase):
    def test_projections_agree_with_the_event_log_after_load(self) -> None:
        for index in range(RUN_COUNT):
            self.start(index)
        self.drain()

        from orbit.workflow.persistence.integrity import check_database

        report = check_database(self.db)
        self.assertTrue(
            report.ok,
            "\n".join(f"{item.code}: {item.message}" for item in report.issues),
        )

    def test_no_run_ends_holding_an_open_responsibility(self) -> None:
        run_ids = [self.start(index) for index in range(8)]
        self.drain()

        from orbit.workflow.domain.ids import EntityId

        for run_id in run_ids:
            with self.subTest(run_id=run_id):
                self.assertEqual(
                    [], self.reads.responsibilities(EntityId.parse(run_id))
                )


class UiReadCapacityTests(CapacityTestCase):
    """P9 UI read shapes: large catalogs stay cursor-bounded."""

    def test_one_thousand_runs_page_without_gaps_or_duplicates(self) -> None:
        first = self.start(0)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
        with connect_workflow_database(self.db) as connection:
            source = connection.execute(
                "SELECT workflow_id,workflow_version,definition_hash FROM workflow_runs"
                " WHERE run_id=?", (first,),
            ).fetchone()
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "INSERT INTO workflow_runs(run_id,workflow_id,workflow_version,"
                "definition_hash,status,aggregate_version,correlation_id,created_at,"
                "updated_at,goal,display_name) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        f"run:catalog-{index:04d}", source["workflow_id"],
                        source["workflow_version"], source["definition_hash"],
                        "succeeded", 0, f"run:catalog-{index:04d}", now, now,
                        f"Catalog goal {index}", f"Catalog run {index}",
                    )
                    for index in range(1, 1_000)
                ],
            )
            connection.commit()

        seen: list[str] = []
        cursor = None
        while True:
            items, cursor = self.reads.list_runs(cursor=cursor, limit=200)
            seen.extend(item["run_id"] for item in items)
            if cursor is None:
                break
        self.assertEqual(1_000, len(seen))
        self.assertEqual(1_000, len(set(seen)))

    def test_ten_thousand_timeline_events_page_in_global_order(self) -> None:
        run_id = self.start(0)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
        with connect_workflow_database(self.db) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "INSERT INTO run_events(event_id,run_id,aggregate_id,"
                "aggregate_sequence,event_type,event_version,correlation_id,"
                "causation_id,occurred_at,payload_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        f"event:capacity-{index:05d}", run_id,
                        f"capacity_event:{index:05d}", 1, "CapacityObserved", 1,
                        run_id, f"event:capacity-cause-{index:05d}", now, "{}",
                    )
                    for index in range(10_000)
                ],
            )
            connection.commit()

        from orbit.workflow.domain.ids import EntityId

        seen: list[int] = []
        cursor = None
        while True:
            items, cursor = self.reads.timeline(
                EntityId.parse(run_id), cursor=cursor, limit=200
            )
            seen.extend(item["position"] for item in items)
            if cursor is None:
                break
        self.assertGreaterEqual(len(seen), 10_000)
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(len(seen), len(set(seen)))


if __name__ == "__main__":
    unittest.main()
