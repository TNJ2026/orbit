"""What the worker does when the Handler never comes back.

`executor.execute` blocks, so a wedged Handler used to wedge the worker with
it: no line of code was left to notice the supervisor had died or to write the
attempt off, and the lease reaper reached it half a minute later with nothing
to say. These cases cover the control thread that now outlives the Handler.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace
import unittest

from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.handlers import HandlerResultStatus
from orbit.workflow.worker.runtime import CancellationToken, WorkerRuntime


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


class Clock:
    """A clock the test moves, so deadlines arrive without real waiting."""

    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def request(
    *, safety=ExecutionSafety.UNKNOWN_ON_LEASE_LOSS, settlement_offset=5,
):
    return SimpleNamespace(
        attempt_id="attempt:1",
        deadline=NOW + timedelta(seconds=60),
        process_deadline=NOW + timedelta(seconds=20),
        settlement_deadline=(
            None if settlement_offset is None
            else NOW + timedelta(seconds=settlement_offset)
        ),
        execution_safety=safety,
    )


class Service:
    """Enough of the runtime service for the worker's control flow."""

    def __init__(self) -> None:
        self.settlements: list[tuple[str, dict]] = []
        self.request = request()
        self.armed_deadline = None

    applied = SimpleNamespace(disposition=SimpleNamespace(value="applied"))

    def claim_job(self, worker_id, now, lease_ttl=None):
        return SimpleNamespace(
            job_id="job:1", lease_id="lease:1", attempt_id="attempt:1",
            lease_token="t", fencing_token=1,
        )

    def start_job(self, claimed, now, *, settlement_deadline=None):
        # Recorded because arming the durable timeout depends on it: the worker
        # has to hand the same deadline to StartJob that it holds itself to.
        self.armed_deadline = settlement_deadline
        return self.applied

    def get_job(self, job_id):
        return SimpleNamespace(status=SimpleNamespace(value="running"))

    def get_lease(self, lease_id):
        return SimpleNamespace(
            status=SimpleNamespace(value="active"),
            expires_at=NOW + timedelta(seconds=60), renewal_revision=1,
        )

    def renew_lease(self, claimed, **kwargs):
        return None

    def build_executor_request(self, claimed, now):
        return self.request

    def _record(self, kind, payload):
        self.settlements.append((kind, payload))
        return self.applied

    def fail_job(self, claimed, now, error, handler_result=None):
        return self._record("fail_job", error)

    def report_unknown_job_result(self, claimed, now, result):
        return self._record("report_unknown", dict(result.error))

    def complete_job(self, claimed, now, output, handler_result=None):
        return self._record("complete_job", dict(output))


class WedgedExecutor:
    """A Handler that does not return until the test lets it."""

    def __init__(self) -> None:
        self.release = Event()
        self.started = Event()
        self.cancelled: list[str] = []

    def execute(self, request, token):
        self.started.set()
        self.release.wait(timeout=10)
        return SimpleNamespace(
            status=HandlerResultStatus.SUCCEEDED, output={"value": 1}, error=None,
        )

    def cancel_current(self, attempt_id=None):
        self.cancelled.append(attempt_id)
        return True


def worker(service, executor, clock, **kwargs):
    return WorkerRuntime(
        service, executor, clock=clock, renew_interval_seconds=0.05,
        **kwargs,
    )


class ForcedSettlementTests(unittest.TestCase):
    def drive(self, service, executor, clock):
        """Run the worker while the test advances the clock past settlement."""

        import threading

        instance = worker(service, executor, clock)
        finished = Event()

        def run() -> None:
            try:
                instance.run_once()
            finally:
                finished.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(executor.started.wait(timeout=5))
        clock.advance(120)
        self.assertTrue(finished.wait(timeout=10), "the worker never settled")
        return instance

    def test_an_unreturning_handler_is_settled_as_unknown(self) -> None:
        """The Handler may already have acted; a retry could repeat it."""

        service, executor, clock = Service(), WedgedExecutor(), Clock()

        self.drive(service, executor, clock)

        self.assertEqual(1, len(service.settlements))
        kind, error = service.settlements[0]
        self.assertEqual("report_unknown", kind)
        self.assertEqual("external_result_unknown", error["code"])
        executor.release.set()

    def test_a_replay_safe_handler_is_settled_as_an_ordinary_timeout(self) -> None:
        """Nothing outside was touched, so this may be retried normally."""

        service, executor, clock = Service(), WedgedExecutor(), Clock()
        service.request = request(safety=ExecutionSafety.REPLAY_SAFE)

        self.drive(service, executor, clock)

        kind, error = service.settlements[0]
        self.assertEqual("fail_job", kind)
        self.assertEqual("attempt_timeout", error["code"])
        self.assertEqual("timeout", error["category"])
        executor.release.set()

    def test_the_timer_is_armed_from_the_schedule_the_worker_holds(self) -> None:
        """One schedule, two components. Recomputing would give them two.

        The durable timer is a backstop for this worker's own settlement, so
        it has to be armed from the same deadline — otherwise the timer and
        the worker disagree about when the attempt is late.
        """

        service, executor, clock = Service(), WedgedExecutor(), Clock()

        self.drive(service, executor, clock)

        self.assertEqual(service.request.settlement_deadline, service.armed_deadline)
        executor.release.set()

    def test_the_settlement_records_why_it_was_forced(self) -> None:
        service, executor, clock = Service(), WedgedExecutor(), Clock()

        self.drive(service, executor, clock)

        _, error = service.settlements[0]
        self.assertIn("reason", error["details"])
        self.assertIsNotNone(error["details"]["settlement_deadline"])
        executor.release.set()

    def test_a_late_handler_result_does_not_settle_a_second_time(self) -> None:
        """The attempt already has an outcome; this one only arrives later."""

        service, executor, clock = Service(), WedgedExecutor(), Clock()

        self.drive(service, executor, clock)
        self.assertEqual(1, len(service.settlements))

        executor.release.set()
        import time
        time.sleep(0.2)

        self.assertEqual(1, len(service.settlements))


class DegradedCapacityTests(unittest.TestCase):
    """A leaked Handler thread holds the worker's slot until it exits."""

    def leak(self):
        service, executor, clock = Service(), WedgedExecutor(), Clock()
        instance = ForcedSettlementTests.drive(
            self, service, executor, clock,
        )
        return instance, service, executor

    def test_a_worker_holding_a_leaked_thread_stops_taking_work(self) -> None:
        """Its capacity is still in use, whatever the attempt's state says."""

        instance, service, executor = self.leak()

        self.assertTrue(instance.degraded)
        self.assertFalse(instance.run_once())
        # No second claim was settled: the worker refused the work rather than
        # running two Handlers while believing it runs one.
        self.assertEqual(1, len(service.settlements))
        executor.release.set()

    def test_the_slot_returns_when_the_thread_finally_exits(self) -> None:
        """Waiting is the only recovery — Python cannot kill a thread."""

        instance, _, executor = self.leak()
        self.assertTrue(instance.degraded)

        executor.release.set()
        import time
        for _ in range(100):
            if not instance.degraded:
                break
            time.sleep(0.05)

        self.assertFalse(instance.degraded)

    def test_being_degraded_is_counted_not_silent(self) -> None:
        """A quietly idle worker and an out-of-service one look identical."""

        instance, _, executor = self.leak()

        instance.run_once()

        counted = {name: value for (name, _), value in instance.metrics.counters.items()}
        self.assertGreaterEqual(counted.get("worker_degraded", 0), 1)
        self.assertGreaterEqual(counted.get("worker_forced_settlement", 0), 1)
        executor.release.set()


class SupervisorLossTests(unittest.TestCase):
    """The one thing the control thread exists for besides the deadline."""

    def test_the_control_thread_cancels_when_the_supervisor_dies(self) -> None:
        """A dead supervisor renews nothing and watches nothing.

        Its thread is gone, so the Handler would run against a lease nobody
        maintains until the reaper noticed. The control thread takes over the
        cancellation the supervisor can no longer perform.
        """

        import threading

        class DyingService(Service):
            def get_lease(self, lease_id):
                raise MemoryError("supervisor thread is doomed")

        service, executor, clock = DyingService(), WedgedExecutor(), Clock()
        previous = threading.excepthook
        threading.excepthook = lambda args: None
        self.addCleanup(setattr, threading, "excepthook", previous)

        instance = worker(service, executor, clock)
        finished = Event()

        def run() -> None:
            try:
                instance.run_once()
            finally:
                finished.set()

        threading.Thread(target=run, daemon=True).start()
        self.assertTrue(executor.started.wait(timeout=5))
        clock.advance(120)
        self.assertTrue(finished.wait(timeout=10))

        self.assertEqual(["attempt:1"], executor.cancelled[:1])
        self.assertEqual("report_unknown", service.settlements[0][0])
        executor.release.set()


class NoScheduleTests(unittest.TestCase):
    """Callers that build requests without a schedule keep the old behaviour."""

    def test_without_a_settlement_deadline_the_worker_waits(self) -> None:
        import threading
        import time

        service, executor, clock = Service(), WedgedExecutor(), Clock()
        service.request = request(settlement_offset=None)
        instance = worker(service, executor, clock)
        finished = Event()

        threading.Thread(
            target=lambda: (instance.run_once(), finished.set()), daemon=True,
        ).start()
        self.assertTrue(executor.started.wait(timeout=5))
        clock.advance(3600)
        time.sleep(0.3)

        self.assertFalse(finished.is_set(), "it settled without a deadline to settle at")
        self.assertEqual([], service.settlements)

        executor.release.set()
        self.assertTrue(finished.wait(timeout=5))
        self.assertEqual("complete_job", service.settlements[0][0])


if __name__ == "__main__":
    unittest.main()
