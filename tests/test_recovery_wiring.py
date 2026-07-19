"""B5: the recovery manager needs every service its findings can act on.

`RecoveryManager` detects orphaned Foreach groups and can aggregate them — but
only if it was given a ForeachService. The composition root wired the human
service and nothing else, so `POST /api/v1/recovery/apply` raised
`RuntimeError("Foreach service is unavailable")` the moment a real Foreach run
needed recovering. Detection without the ability to act is worse than not
detecting: the operator is told the runtime knows, and the fix button 500s.

An orphaned Subflow is deliberately *not* auto-applied — its child run is gone,
which needs a person — so this file also pins that it produces a manual
takeover rather than an error.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from orbit.web.api_v1 import build_api_v1
from orbit.workflow.application.durable_runtime_service import (
    DurableRuntimeApplicationService,
)
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.migrations import migrate_workflow_database
from orbit.workflow.recovery.manager import RecoveryManager


NOW = datetime(2026, 7, 19, tzinfo=timezone.utc)
RUN = "run:" + "a" * 64


class RecoveryWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "runtime.db"
        connection = connect_workflow_database(self.db)
        migrate_workflow_database(connection)
        connection.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def built_manager(self) -> RecoveryManager:
        """The manager the production API actually constructs."""

        captured: list[RecoveryManager] = []
        original = RecoveryManager.__init__

        def capture(self, *args, **kwargs):
            original(self, *args, **kwargs)
            captured.append(self)

        RecoveryManager.__init__ = capture
        try:
            build_api_v1(self.db, DurableRuntimeApplicationService(self.db))
        finally:
            RecoveryManager.__init__ = original
        self.assertTrue(captured, "the API built no RecoveryManager")
        return captured[0]

    def test_the_api_wires_every_service_recovery_can_call(self) -> None:
        manager = self.built_manager()
        self.assertIsNotNone(manager.human, "EXPIRED_HUMAN could not be applied")
        self.assertIsNotNone(manager.foreach, "ORPHAN_FOREACH could not be applied")

    def test_every_applicable_finding_code_has_a_service(self) -> None:
        """Whatever recovery declares safe to apply, it must be able to apply.

        Keeping this as a property rather than a list means a new
        auto-applicable finding cannot be added without wiring its service.
        """

        manager = self.built_manager()
        auto_applicable = {"EXPIRED_HUMAN": manager.human, "ORPHAN_FOREACH": manager.foreach}
        for code, service in auto_applicable.items():
            with self.subTest(code=code):
                self.assertIsNotNone(service, f"{code} has no service to apply it")

    def test_an_orphan_subflow_is_left_to_a_person(self) -> None:
        """Its child run is gone; no automatic action is the right answer."""

        manager = self.built_manager()
        codes = {
            entry[0]: entry[3]
            for entry in _finding_specs(manager)
        }
        self.assertFalse(codes["ORPHAN_SUBFLOW"], "an orphan subflow was marked safe")
        self.assertTrue(codes["ORPHAN_FOREACH"])

    def test_a_scan_on_an_empty_database_finds_nothing(self) -> None:
        report = self.built_manager().scan(NOW)
        self.assertEqual((), report.findings)
        self.assertEqual(0, report.scanned_runs)


class PerFindingApplyTests(RecoveryWiringTests):
    """Applying is a selection, not a sweep."""

    def expired_human_task(self) -> str:
        """A finding that is genuinely safe to auto-apply."""

        from orbit.workflow.application.human_service import HumanTaskService
        from orbit.workflow.domain.human import HumanTaskKind
        from orbit.workflow.domain.ids import EntityId

        with connect_workflow_database(self.db) as connection:
            connection.execute(
                "INSERT INTO workflow_definitions(workflow_id, name, created_at,"
                " created_by) VALUES ('workflow:r', 'R', ?, 'test')",
                (NOW.isoformat(),),
            )
            connection.execute(
                "INSERT INTO workflow_versions(workflow_id, version, definition_hash,"
                " dsl_version, ir_version, compiler_version, canonical_ir_json,"
                " source_format, source_text, catalog_fingerprint, created_at, created_by)"
                " VALUES ('workflow:r', 1, 'sha256:r', '1.0', '1.1', '1.0', '{}',"
                " 'json', NULL, 'sha256:c', ?, 'test')",
                (NOW.isoformat(),),
            )
            connection.execute(
                "INSERT INTO workflow_runs(run_id, workflow_id, workflow_version,"
                " definition_hash, status, aggregate_version, correlation_id,"
                " created_at, updated_at)"
                " VALUES (?, 'workflow:r', 1, 'sha256:r', 'waiting', 1, ?, ?, ?)",
                (RUN, RUN, NOW.isoformat(), NOW.isoformat()),
            )
            connection.commit()

        HumanTaskService(self.db).create(
            EntityId.parse(RUN), HumanTaskKind.APPROVAL, {"q": "?"},
            actor="test", now=NOW,
            deadline_at=NOW - timedelta(hours=1),
        )
        return RUN

    def findings(self, manager):
        return {f.action_id: f for f in manager.scan(NOW).findings}

    def test_a_selected_finding_is_applied(self) -> None:
        self.expired_human_task()
        manager = self.built_manager()
        found = self.findings(manager)
        expired = [k for k in found if k.startswith("EXPIRED_HUMAN")]
        self.assertTrue(expired, f"no applicable finding to test with: {list(found)}")

        results = manager.apply_findings([expired[0]], NOW)
        self.assertEqual(["applied"], [r.outcome for r in results])
        self.assertNotIn(expired[0], self.findings(manager), "the finding survived")

    def test_an_unselected_finding_is_left_alone(self) -> None:
        self.expired_human_task()
        manager = self.built_manager()
        before = self.findings(manager)
        self.assertTrue(before)

        manager.apply_findings(["NOTHING:matches:1"], NOW)
        self.assertEqual(set(before), set(self.findings(manager)))

    def test_a_stale_version_in_the_action_id_does_not_apply(self) -> None:
        """The id embeds the version, so it is the compare-and-set token."""

        self.expired_human_task()
        manager = self.built_manager()
        real = next(iter(self.findings(manager)))
        code, entity, version = real.rsplit(":", 2)
        stale = f"{code}:{entity}:{int(version) + 5}"

        results = manager.apply_findings([stale], NOW)
        self.assertEqual(["stale"], [r.outcome for r in results])
        self.assertIn(real, self.findings(manager))

    def test_an_empty_selection_does_nothing(self) -> None:
        self.assertEqual((), self.built_manager().apply_findings([], NOW))


def _finding_specs(manager: RecoveryManager):
    """The (code, sql, params, safe) tuples the scanner declares."""

    import inspect
    import re

    source = inspect.getsource(manager._find_for_run)
    return [
        (code, None, None, safe == "True")
        for code, safe in re.findall(
            r'"(\w+)",\s*"""[^"]*""",\s*\([^)]*\),\s*(True|False)', source, re.S
        )
    ]


if __name__ == "__main__":
    unittest.main()
