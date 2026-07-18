from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
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


if __name__ == "__main__": unittest.main()
