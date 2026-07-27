from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import threading
import time
import unittest

from orbit.workflow.worker.runtime import CancellationToken
from orbit.workflow.worker.supervisor import LeaseSupervisor


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


class _Service:
    def __init__(self): self.renewals = []
    def get_lease(self, lease_id):
        return SimpleNamespace(
            status=SimpleNamespace(value="active"), expires_at=NOW + timedelta(seconds=20),
            renewal_revision=len(self.renewals),
        )
    def renew_lease(self, claimed, **kwargs): self.renewals.append(kwargs)


class _FlakyService(_Service):
    def __init__(self): super().__init__(); self.calls = 0
    def renew_lease(self, claimed, **kwargs):
        self.calls += 1
        if self.calls == 1: raise RuntimeError("sqlite busy")
        super().renew_lease(claimed, **kwargs)


class LeaseSupervisorTests(unittest.TestCase):
    def test_supervisor_renews_without_blocking_handler(self):
        service = _Service(); token = CancellationToken()
        supervisor = LeaseSupervisor(
            service, SimpleNamespace(lease_id="lease:1"), token,
            clock=lambda: NOW, deadline=NOW + timedelta(minutes=1),
            renew_interval_seconds=0.01,
        )
        supervisor.start(); time.sleep(0.035); supervisor.stop()
        self.assertGreaterEqual(len(service.renewals), 2)
        self.assertFalse(token.cancelled)

    def test_deadline_cancels_and_invokes_handler_cancel(self):
        called = []
        token = CancellationToken()
        supervisor = LeaseSupervisor(
            _Service(), SimpleNamespace(lease_id="lease:1"), token,
            clock=lambda: NOW + timedelta(minutes=2),
            deadline=NOW + timedelta(minutes=1), renew_interval_seconds=0.01,
            on_cancel=lambda: called.append(True),
        )
        supervisor.start(); time.sleep(0.025); supervisor.stop()
        self.assertTrue(token.cancelled)
        self.assertEqual([True], called)

    def test_single_transient_renewal_failure_is_tolerated(self):
        service = _FlakyService(); token = CancellationToken()
        supervisor = LeaseSupervisor(
            service, SimpleNamespace(lease_id="lease:1"), token,
            clock=lambda: NOW, deadline=NOW + timedelta(minutes=1),
            renew_interval_seconds=0.01,
        )
        supervisor.start(); time.sleep(0.04); supervisor.stop()
        self.assertEqual(1, supervisor.renewal_failures)
        self.assertGreaterEqual(len(service.renewals), 1)
        self.assertFalse(token.cancelled)


    def test_a_supervisor_that_dies_takes_the_handler_down_with_it(self):
        """Silent death is the worst outcome: the Handler runs on unrenewed.

        Whatever kills this thread, the attempt must not be left for the lease
        reaper to discover half a minute later with no explanation.
        """

        class _Exploding:
            reason = None
            @property
            def cancelled(self):
                raise MemoryError("supervisor thread is doomed")
            def cancel(self, reason="cancelled"):
                _Exploding.reason = reason

        cancelled = []
        # The thread re-raises so a real deployment sees it in its logs; here
        # that would only spray a traceback across the test output.
        previous_hook = threading.excepthook
        threading.excepthook = lambda args: None
        self.addCleanup(setattr, threading, "excepthook", previous_hook)
        supervisor = LeaseSupervisor(
            _Service(), SimpleNamespace(lease_id="lease:1"), _Exploding(),
            clock=lambda: NOW, deadline=NOW + timedelta(minutes=1),
            renew_interval_seconds=0.01,
            on_cancel=lambda: cancelled.append(True),
        )
        supervisor.start()
        time.sleep(0.05)
        supervisor.stop()
        self.assertEqual("lease_lost", _Exploding.reason)
        self.assertEqual([True], cancelled)


class _UnwritableService(_Service):
    """A database that will not take writes — the shape of the real outage."""

    def renew_lease(self, claimed, **kwargs):
        raise RuntimeError("attempt to write a readonly database")


class _FencedOutService(_Service):
    """Another worker holds the lease; the store refuses on purpose."""

    def renew_lease(self, claimed, **kwargs):
        raise ValueError("lease renewal exceeds maximum extension")


class RenewalEvidenceTests(unittest.TestCase):
    """What a failed renewal window leaves behind for whoever investigates."""

    def supervise(self, service, **kwargs):
        token = CancellationToken()
        supervisor = LeaseSupervisor(
            service, SimpleNamespace(lease_id="lease:1"), token,
            clock=lambda: NOW, deadline=NOW + timedelta(minutes=1),
            renew_interval_seconds=0.01, **kwargs,
        )
        supervisor.start(); time.sleep(0.08); supervisor.stop()
        return supervisor

    def test_the_window_is_recorded_without_writing_anywhere(self) -> None:
        """The database is the thing that failed; the record cannot need it."""

        supervisor = self.supervise(_UnwritableService())
        summary = supervisor.summary

        self.assertTrue(summary.observed)
        self.assertGreaterEqual(summary.failures, 3)
        self.assertEqual("lease_lost", summary.cancel_reason)
        self.assertIsNotNone(summary.first_failure_at)
        self.assertIsNotNone(summary.last_failure_at)
        self.assertEqual("RuntimeError", summary.last_error_type)

    def test_an_unavailable_store_is_distinguished_from_a_refused_renewal(self) -> None:
        """One means the database is down; the other means fencing worked.

        Collapsing them loses the only diagnostic this record exists for.
        """

        self.assertEqual(
            "renewal_unavailable",
            self.supervise(_UnwritableService()).summary.last_error_code,
        )
        self.assertEqual(
            "renewal_rejected",
            self.supervise(_FencedOutService()).summary.last_error_code,
        )

    def test_a_healthy_window_leaves_nothing_to_persist(self) -> None:
        """No failures, no summary — an audit row per attempt would be noise."""

        supervisor = self.supervise(_Service())
        self.assertFalse(supervisor.summary.observed)

    def test_a_recovered_renewal_records_when_it_last_worked(self) -> None:
        supervisor = self.supervise(_FlakyService())
        summary = supervisor.summary

        self.assertEqual(1, summary.failures)
        self.assertEqual(0, summary.consecutive_failures)
        self.assertEqual(NOW, summary.last_success_at)

    def test_dispatching_termination_is_recorded_separately_from_settling(self) -> None:
        """"We asked it to stop" is not "it stopped", and neither is "settled"."""

        supervisor = self.supervise(_UnwritableService(), on_cancel=lambda: None)
        self.assertTrue(supervisor.summary.termination_dispatched)
        # Forced settlement does not exist yet; an open question must not be
        # recorded as a negative answer.
        self.assertIsNone(supervisor.summary.settled)

    def test_a_failing_cancel_hook_is_not_reported_as_dispatched(self) -> None:
        def explode() -> None:
            raise OSError("no such process")

        supervisor = self.supervise(_UnwritableService(), on_cancel=explode)
        self.assertFalse(supervisor.summary.termination_dispatched)

    def test_metrics_count_failures_by_stable_code(self) -> None:
        from orbit.workflow.worker.runtime import InMemoryMetrics

        metrics = InMemoryMetrics()
        self.supervise(_UnwritableService(), metrics=metrics)

        counted = {name: value for (name, _), value in metrics.counters.items()}
        self.assertGreaterEqual(counted.get("lease_renewal_failed", 0), 3)
        self.assertGreaterEqual(
            counted.get("lease_renewal_failed_renewal_unavailable", 0), 3
        )
        self.assertEqual(1, counted.get("lease_supervisor_cancelled_lease_lost"))

    def test_the_summary_serialises_to_primitives(self) -> None:
        """It is written as audit details, so it has to survive canonical JSON."""

        import json

        summary = self.supervise(_UnwritableService()).summary
        encoded = json.dumps(summary.to_primitive())

        self.assertIn("renewal_unavailable", encoded)
        self.assertIn("lease_lost", encoded)


if __name__ == "__main__": unittest.main()
