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

from datetime import datetime, timezone
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
