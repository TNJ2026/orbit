"""M7 gate 3, API quadrant: crashing between the business write and the receipt.

The worker, timer and kernel kill points are covered in
`test_workflow_durable_faults.py` and `test_workflow_runtime_faults.py`. The
API boundary has its own window, and it is the nastiest one: the business
command has already happened, the caller never heard about it, and the retry
looks exactly like the original request.

The rule under test is that such a retry is *refused for reconciliation*
rather than silently executed a second time. An API that guesses here is an
API that double-charges a budget or starts a second run.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from orbit.workflow.api.routes import (
    ApiCommandExecutor, CommandInProgress, IdempotencyConflict,
)
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.migrations import migrate_workflow_database


class Crash(RuntimeError):
    """Stands in for the process dying at a named point."""


class ApiKillPointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "runtime.db"
        connection = connect_workflow_database(self.db)
        migrate_workflow_database(connection)
        connection.close()
        self.executed = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def executor(self, *, crash_at: str | None = None) -> ApiCommandExecutor:
        def hook(point: str) -> None:
            if point == crash_at:
                raise Crash(point)

        return ApiCommandExecutor(self.db, fault_hook=hook if crash_at else None)

    def business(self, body, actor, key):
        self.executed.append((actor, key, dict(body)))
        return {"ok": True, "calls": len(self.executed)}

    def call(self, executor, *, key="k1", body=None):
        """Named `call`, not `run`: TestCase.run is the test runner's entry."""

        return executor.execute(
            actor="operator", idempotency_key=key, method="POST",
            request_path="/api/v1/runs", body=body or {"workflow_id": "w"},
            handler=self.business,
        )

    def receipts(self):
        with connect_workflow_database(self.db, read_only=True) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT actor, idempotency_key, status_code FROM api_command_receipts"
                )
            ]

    # -- the happy path, for contrast -------------------------------------

    def test_a_completed_command_replays_from_its_receipt(self) -> None:
        executor = self.executor()
        first_status, first = self.call(executor)
        second_status, second = self.call(executor)

        self.assertEqual(first_status, second_status)
        self.assertEqual(first, second)
        self.assertEqual(1, len(self.executed), "the business ran twice")

    # -- the window -------------------------------------------------------

    def test_a_crash_after_business_leaves_a_pending_receipt(self) -> None:
        with self.assertRaises(Crash):
            self.call(self.executor(crash_at="after_business_before_api_receipt"))

        self.assertEqual(1, len(self.executed), "the business did run")
        pending = self.receipts()
        self.assertEqual(1, len(pending))
        self.assertEqual(102, pending[0]["status_code"], "not marked in-progress")

    def test_a_retry_after_that_crash_is_refused_not_repeated(self) -> None:
        """The whole point: an unknown outcome must not be guessed."""

        with self.assertRaises(Crash):
            self.call(self.executor(crash_at="after_business_before_api_receipt"))

        with self.assertRaises(CommandInProgress):
            self.call(self.executor())

        self.assertEqual(
            1, len(self.executed),
            "a retry re-ran the business command after an unknown outcome",
        )

    def test_the_refusal_survives_a_process_restart(self) -> None:
        """The pending row is durable, so a fresh executor refuses too."""

        with self.assertRaises(Crash):
            self.call(self.executor(crash_at="after_business_before_api_receipt"))

        for _ in range(3):
            with self.assertRaises(CommandInProgress):
                self.call(ApiCommandExecutor(self.db))
        self.assertEqual(1, len(self.executed))

    def test_a_different_command_is_unaffected_by_a_stuck_one(self) -> None:
        """One unreconciled command must not wedge the whole API."""

        with self.assertRaises(Crash):
            self.call(
                self.executor(crash_at="after_business_before_api_receipt"), key="stuck"
            )

        status, result = self.call(self.executor(), key="other")
        self.assertEqual(True, result["ok"])
        self.assertEqual(2, len(self.executed))

    def test_reusing_a_key_with_a_different_body_conflicts(self) -> None:
        executor = self.executor()
        self.call(executor, key="shared")
        with self.assertRaises(IdempotencyConflict):
            self.call(executor, key="shared", body={"workflow_id": "different"})
        self.assertEqual(1, len(self.executed))

    def test_a_failing_business_command_records_no_success(self) -> None:
        def failing(body, actor, key):
            raise ValueError("business rejected it")

        with self.assertRaises(ValueError):
            self.executor().execute(
                actor="operator", idempotency_key="fails", method="POST",
                request_path="/api/v1/runs", body={}, handler=failing,
            )

        # A rejected command may be retried: nothing external happened.
        status, result = self.call(self.executor(), key="fails")
        self.assertEqual(True, result["ok"])


if __name__ == "__main__":
    unittest.main()
