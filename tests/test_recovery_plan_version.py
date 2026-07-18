"""M7 gate 3, planner quadrant: recovery must advance the *current* plan.

A run that has been replanned has more than one plan version. If the recovery
scanner advances the graph against version 1, it routes a replanned run by a
graph that no longer describes it — nodes the patch added do not exist there,
and nodes it removed still do.

This is the kill point between `PlanService.commit()` (which commits the new
plan version) and `PlanService.activate()` (which submits advance_graph): a
crash in that window leaves recovery to do the activation, so recovery has to
pick the same plan version the planner would have.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from orbit.workflow.application.durable_runtime_service import (
    DurableRuntimeApplicationService,
)
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.versions import Revision
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.migrations import migrate_workflow_database
from tests.test_web_composition import publish_linear_workflow, start_run_command


RUN = EntityId("run", "e" * 64)
NOW = datetime(2026, 7, 19, tzinfo=timezone.utc)


class RecoveryPlanVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "runtime.db"
        connection = connect_workflow_database(self.db)
        migrate_workflow_database(connection)
        connection.close()
        _workflow_id, self.digest = publish_linear_workflow(self.db)

        self.service = DurableRuntimeApplicationService(self.db)
        self.service.submit(start_run_command(RUN, self.digest))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def clone_plan_as_version(self, version: int) -> None:
        """Add a later plan version, as a replan would."""

        with connect_workflow_database(self.db) as connection:
            row = connection.execute(
                "SELECT * FROM execution_plans WHERE run_id = ? AND plan_version = 1",
                (str(RUN),),
            ).fetchone()
            connection.execute(
                "INSERT INTO execution_plans(plan_id, run_id, plan_version, workflow_id,"
                " workflow_version, plan_schema_version, canonical_plan_json,"
                " definition_hash, created_event_id, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    f"plan:v{version}", str(RUN), version, row["workflow_id"],
                    row["workflow_version"], row["plan_schema_version"],
                    row["canonical_plan_json"], "sha256:" + f"{version:x}" * 64,
                    row["created_event_id"], NOW.isoformat(),
                ),
            )
            connection.commit()

    def advanced_plan_versions(self) -> list[int]:
        """Which plan version each advance_graph command asked for."""

        with connect_workflow_database(self.db, read_only=True) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM run_events WHERE run_id = ?"
                " AND event_type = 'workflow_run_transitioned'",
                (str(RUN),),
            ).fetchall()
        import json

        return [
            json.loads(row["payload_json"]).get("plan_version")
            for row in rows
            if "plan_version" in json.loads(row["payload_json"])
        ]

    def test_the_repository_can_report_the_latest_plan_version(self) -> None:
        """Recovery has a correct answer available; it must use it."""

        self.clone_plan_as_version(2)
        with self.service.uow_factory() as uow:
            versions = [
                record.plan_version.value for record in uow.plans.list_versions(RUN)
            ]
        self.assertEqual([1, 2], versions)

    def test_recovery_advances_the_latest_plan_not_version_one(self) -> None:
        self.clone_plan_as_version(2)

        submitted: list[dict] = []
        original = self.service.submit

        def record(command):
            if command.command_type == "advance_graph":
                submitted.append(dict(command.payload))
            return original(command)

        self.service.submit = record
        self.service.durable_recovery.scan_once(NOW)

        self.assertTrue(submitted, "recovery never advanced the graph")
        for payload in submitted:
            with self.subTest(payload=payload):
                self.assertEqual(
                    2, payload.get("plan_version"),
                    "recovery advanced a replanned run against plan version 1",
                )

    def test_a_run_that_was_never_replanned_still_advances_version_one(self) -> None:
        submitted: list[dict] = []
        original = self.service.submit

        def record(command):
            if command.command_type == "advance_graph":
                submitted.append(dict(command.payload))
            return original(command)

        self.service.submit = record
        self.service.durable_recovery.scan_once(NOW)

        for payload in submitted:
            self.assertEqual(1, payload.get("plan_version"))


if __name__ == "__main__":
    unittest.main()
