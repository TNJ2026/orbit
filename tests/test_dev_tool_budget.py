"""M5.4: a development tool's work reaches the budget ledger.

The unit tests prove each adapter *reports* usage. This proves the report
actually lands: reporter → budget service → ledger → account, with the run's
consumed amount moving. Without this chain a development workflow runs beside
its budget rather than inside it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from orbit.workflow.application.budget_service import BudgetService
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.handlers.dev_tools import (
    GitIntegrateAdapter, GitStatusAdapter, VerifyAdapter, VerifyProfile,
    WorkspaceRunner,
)
from orbit.workflow.handlers.tools import ToolRequest
from orbit.workflow.handlers.usage import PersistentBudgetUsageReporter
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.migrations import migrate_workflow_database


NOW = datetime(2026, 7, 19, tzinfo=timezone.utc)
RUN = EntityId("run", "b" * 64)
ATTEMPT = EntityId("attempt", "c" * 64)

# What a deployment decides a child process is worth. Kept trivial here so the
# test is about the plumbing, not about pricing policy.
MICROUNITS_PER_TOOL_CALL = 250


def scripted_runner(outcomes):
    def run(argv, **kwargs):
        code, out = outcomes.pop(0) if outcomes else (0, "")
        return SimpleNamespace(
            returncode=code, stdout=out, stderr="", stdout_truncated=False,
            stderr_truncated=False, timed_out=False, cancelled=False,
        )

    return run


class DevToolBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "runtime.db"
        connection = connect_workflow_database(self.db)
        migrate_workflow_database(connection)
        connection.close()
        self._seed_run()

        self.budget = BudgetService(self.db)
        self.budget.open_account(RUN, 10_000, actor="test", now=NOW)
        self.reservation = self.budget.reserve(
            RUN, ATTEMPT, 5_000, actor="test", now=NOW
        )
        self.reporter = PersistentBudgetUsageReporter(
            self.budget,
            lambda attempt_id: self.reservation.reservation_id,
            lambda snapshot: snapshot.tool_calls * MICROUNITS_PER_TOOL_CALL,
        )
        self.context = SimpleNamespace(
            artifacts=None,
            request=SimpleNamespace(attempt_id=ATTEMPT),
            clock=lambda: NOW,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _seed_run(self) -> None:
        with connect_workflow_database(self.db) as connection:
            connection.execute(
                "INSERT INTO workflow_definitions(workflow_id, name, created_at,"
                " created_by) VALUES ('workflow:dev', 'Dev', ?, 'test')",
                (NOW.isoformat(),),
            )
            connection.execute(
                "INSERT INTO workflow_versions(workflow_id, version, definition_hash,"
                " dsl_version, ir_version, compiler_version, canonical_ir_json,"
                " source_format, source_text, catalog_fingerprint, created_at, created_by)"
                " VALUES ('workflow:dev', 1, 'sha256:w', '1.0', '1.1', '1.0', '{}',"
                " 'json', NULL, 'sha256:c', ?, 'test')",
                (NOW.isoformat(),),
            )
            connection.execute(
                "INSERT INTO workflow_runs(run_id, workflow_id, workflow_version,"
                " definition_hash, status, aggregate_version, correlation_id,"
                " created_at, updated_at)"
                " VALUES (?, 'workflow:dev', 1, 'sha256:w', 'running', 1, ?, ?, ?)",
                (str(RUN), str(RUN), NOW.isoformat(), NOW.isoformat()),
            )
            connection.commit()

    def account(self):
        with connect_workflow_database(self.db, read_only=True) as connection:
            return connection.execute(
                "SELECT * FROM budget_accounts WHERE run_id = ?", (str(RUN),)
            ).fetchone()

    def ledger(self):
        with connect_workflow_database(self.db, read_only=True) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT entry_id, kind, amount_microunits FROM budget_ledger_entries"
                    " WHERE run_id = ? ORDER BY entry_id", (str(RUN),)
                )
            ]

    def runner(self, outcomes):
        return WorkspaceRunner(
            SimpleNamespace(
                acquire=lambda ref: SimpleNamespace(path=Path(self.temp.name))
            ),
            runner=scripted_runner(outcomes),
        )

    # -- the chain --------------------------------------------------------

    def test_a_tool_run_moves_the_account(self) -> None:
        before = self.account()["consumed_microunits"]

        result = GitStatusAdapter(self.runner([(0, "")])).execute(
            ToolRequest({"workspace_ref": "ws1"}, "k", {}), self.context
        )
        self.reporter.report(result.usage)

        after = self.account()["consumed_microunits"]
        self.assertEqual(before + MICROUNITS_PER_TOOL_CALL, after)

    def test_integrate_costs_more_because_it_runs_more(self) -> None:
        result = GitIntegrateAdapter(
            self.runner([(0, ""), (0, ""), (0, "abc\n")])
        ).execute(
            ToolRequest({"workspace_ref": "ws1", "message": "done"}, "k", {}),
            self.context,
        )
        self.reporter.report(result.usage)
        self.assertEqual(
            3 * MICROUNITS_PER_TOOL_CALL, self.account()["consumed_microunits"]
        )

    def test_the_ledger_records_the_consumption(self) -> None:
        result = VerifyAdapter(
            self.runner([(0, "ok\n")]), [VerifyProfile("unit", ("true",))]
        ).execute(
            ToolRequest({"workspace_ref": "ws1", "profile": "unit"}, "k", {}),
            self.context,
        )
        self.reporter.report(result.usage)
        usage = [e for e in self.ledger() if e["kind"] == "usage"]
        self.assertEqual(1, len(usage), self.ledger())
        self.assertEqual(MICROUNITS_PER_TOOL_CALL, usage[0]["amount_microunits"])

    def test_reporting_the_same_usage_twice_bills_once(self) -> None:
        """A retried delivery must not double-charge the run."""

        result = GitStatusAdapter(self.runner([(0, "")])).execute(
            ToolRequest({"workspace_ref": "ws1"}, "k", {}), self.context
        )
        self.assertTrue(self.reporter.report(result.usage))
        self.assertFalse(self.reporter.report(result.usage))
        self.assertEqual(
            MICROUNITS_PER_TOOL_CALL, self.account()["consumed_microunits"]
        )

    def test_a_failed_verification_is_still_billed(self) -> None:
        result = VerifyAdapter(
            self.runner([(1, "2 failed\n")]), [VerifyProfile("unit", ("true",))]
        ).execute(
            ToolRequest({"workspace_ref": "ws1", "profile": "unit"}, "k", {}),
            self.context,
        )
        self.assertFalse(result.output["passed"])
        self.reporter.report(result.usage)
        self.assertEqual(
            MICROUNITS_PER_TOOL_CALL, self.account()["consumed_microunits"]
        )

    def test_consumption_accumulates_across_tools(self) -> None:
        """Usage is cumulative per attempt, so later reports must not reset it."""

        status = GitStatusAdapter(self.runner([(0, "")])).execute(
            ToolRequest({"workspace_ref": "ws1"}, "k", {}), self.context
        )
        self.reporter.report(status.usage)

        later = SimpleNamespace(
            artifacts=None,
            request=SimpleNamespace(attempt_id=EntityId("attempt", "d" * 64)),
            clock=lambda: NOW + timedelta(seconds=1),
        )
        second_reservation = self.budget.reserve(
            RUN, later.request.attempt_id, 2_000, actor="test", now=NOW
        )
        reporter = PersistentBudgetUsageReporter(
            self.budget,
            lambda attempt_id: second_reservation.reservation_id,
            lambda snapshot: snapshot.tool_calls * MICROUNITS_PER_TOOL_CALL,
        )
        integrate = GitIntegrateAdapter(
            self.runner([(0, ""), (0, ""), (0, "abc\n")])
        ).execute(
            ToolRequest({"workspace_ref": "ws1", "message": "done"}, "k", {}), later
        )
        reporter.report(integrate.usage)

        self.assertEqual(
            4 * MICROUNITS_PER_TOOL_CALL, self.account()["consumed_microunits"]
        )

    def test_usage_without_a_reservation_is_refused(self) -> None:
        """Billing work to nothing would silently lose it."""

        reporter = PersistentBudgetUsageReporter(
            self.budget, lambda attempt_id: None, lambda snapshot: 1
        )
        result = GitStatusAdapter(self.runner([(0, "")])).execute(
            ToolRequest({"workspace_ref": "ws1"}, "k", {}), self.context
        )
        with self.assertRaises(ValueError):
            reporter.report(result.usage)


if __name__ == "__main__":
    unittest.main()
