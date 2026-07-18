"""M7: a restart in the middle of a run needs no human and no repair.

Manual gate 4 says the operator must not have to touch the database after a
restart. That is a claim about the log being the state, so it is tested by
actually stopping a composition mid-flight and starting a fresh one on the
same file — new objects, new workers, new leases, same database.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest

from orbit.web.app import RuntimeComposition
from orbit.workflow.api.read_models import ReadModelService
from orbit.workflow.application.run_service import RunApplicationService
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.integrity import check_database
from tests.test_web_composition import (
    SCHEMAS, publish_linear_workflow, transform_registration,
)


class RestartRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "runtime.db"
        first = self.new_composition()
        publish_linear_workflow(self.db)
        self.compositions = [first]

    def tearDown(self) -> None:
        for composition in self.compositions:
            composition.stop()
        self.temp.cleanup()

    def new_composition(self, workers: int = 2) -> RuntimeComposition:
        composition = RuntimeComposition(
            self.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            worker_count=workers, poll_seconds=0.01,
        )
        if hasattr(self, "compositions"):
            self.compositions.append(composition)
        return composition

    def start_run(self, composition, key: str) -> str:
        return RunApplicationService(self.db, composition.service).start_run(
            workflow_id="workflow:linear", inputs={"value": 1},
            actor="restart", idempotency_key=key,
        ).run_id

    def status_of(self, run_id: str) -> str:
        with connect_workflow_database(self.db, read_only=True) as connection:
            row = connection.execute(
                "SELECT status FROM workflow_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return row["status"]

    def wait_until(self, run_id: str, status: str, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.status_of(run_id) == status:
                return
            time.sleep(0.02)
        self.fail(f"{run_id} stayed {self.status_of(run_id)}, wanted {status}")

    def test_a_run_started_before_a_restart_finishes_after_it(self) -> None:
        first = self.compositions[0]
        run_id = self.start_run(first, "restart-1")
        self.assertNotEqual("succeeded", self.status_of(run_id))

        # Never started a worker: the run exists only as events on disk.
        second = self.new_composition()
        second.start()
        self.wait_until(run_id, "succeeded")

    def test_stopping_mid_flight_leaves_no_repair_for_a_human(self) -> None:
        first = self.compositions[0]
        first.start()
        run_id = self.start_run(first, "restart-2")
        # Stop without waiting: some job is almost certainly leased right now.
        first.stop(timeout=30)

        second = self.new_composition()
        second.start()
        self.wait_until(run_id, "succeeded")

        report = check_database(self.db)
        self.assertTrue(
            report.ok,
            "a restart required manual repair:\n"
            + "\n".join(f"{item.code}: {item.message}" for item in report.issues),
        )

    def test_a_restart_does_not_duplicate_work(self) -> None:
        first = self.compositions[0]
        first.start()
        run_id = self.start_run(first, "restart-3")
        first.stop(timeout=30)

        second = self.new_composition()
        second.start()
        self.wait_until(run_id, "succeeded")

        with connect_workflow_database(self.db, read_only=True) as connection:
            duplicates = connection.execute(
                "SELECT node_run_id, attempt_number, COUNT(*) c FROM node_attempts"
                " GROUP BY node_run_id, attempt_number HAVING c > 1"
            ).fetchall()
        self.assertEqual([], duplicates)

    def test_the_projection_survives_the_restart_intact(self) -> None:
        first = self.compositions[0]
        first.start()
        run_id = self.start_run(first, "restart-4")
        self.wait_until(run_id, "succeeded")
        before = ReadModelService(self.db).run_summary(EntityId.parse(run_id))
        first.stop(timeout=30)

        second = self.new_composition()
        second.start()
        after = ReadModelService(self.db).run_summary(EntityId.parse(run_id))
        self.assertEqual(before, after)

    def test_the_second_composition_reports_ready(self) -> None:
        self.compositions[0].stop(timeout=30)
        second = self.new_composition()
        second.start()
        for _ in range(200):
            ready, checks = second.readiness()
            if ready:
                break
            time.sleep(0.02)
        self.assertTrue(ready, checks)


if __name__ == "__main__":
    unittest.main()
