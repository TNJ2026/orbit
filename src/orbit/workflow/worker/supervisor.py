"""Lease renewal and cooperative cancellation for a running Handler."""

from __future__ import annotations

from datetime import timedelta
from threading import Event, Thread


class LeaseSupervisor:
    def __init__(
        self, service, claimed, cancellation_token, *, clock, deadline,
        renew_interval_seconds: float = 10.0,
        lease_ttl: timedelta = timedelta(seconds=30), on_cancel=None,
        max_consecutive_renewal_failures: int = 3,
    ) -> None:
        if renew_interval_seconds <= 0:
            raise ValueError("renew_interval_seconds must be positive")
        if max_consecutive_renewal_failures < 1:
            raise ValueError("max_consecutive_renewal_failures must be positive")
        self.service = service
        self.claimed = claimed
        self.token = cancellation_token
        self.clock = clock
        self.deadline = deadline
        self.renew_interval_seconds = renew_interval_seconds
        self.lease_ttl = lease_ttl
        self.on_cancel = on_cancel or (lambda: None)
        self.max_consecutive_renewal_failures = max_consecutive_renewal_failures
        self._stop = Event()
        self._thread = None
        self.renewal_failures = 0
        self.consecutive_renewal_failures = 0
        self.last_known_expiry = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("LeaseSupervisor already started")
        self._thread = Thread(
            target=self._run, name="workflow-lease-supervisor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.renew_interval_seconds * 2))

    def _cancel(self, reason="cancelled") -> None:
        self.token.cancel(reason)
        try:
            self.on_cancel()
        except Exception:
            pass

    def _run(self) -> None:
        try:
            self._renew_until_done()
        except BaseException:  # noqa: BLE001 - see below
            # This thread is the only thing keeping the lease alive. If it dies
            # for a reason the loop below does not name, the Handler keeps
            # running against a lease nobody is renewing, and the attempt ends
            # as `unknown_external_result` half a minute later with nothing to
            # explain it. Cancelling instead makes the Handler stop and report.
            self._cancel("lease_lost")
            raise

    def _renew_until_done(self) -> None:
        while not self._stop.wait(self.renew_interval_seconds):
            now = self.clock()
            if now >= self.deadline:
                self._cancel("timeout")
                return
            if self.token.cancelled:
                self._cancel("cancelled")
                return
            try:
                lease = self.service.get_lease(self.claimed.lease_id)
                if lease is None or lease.status.value != "active":
                    self._cancel("lease_lost")
                    return
                self.last_known_expiry = lease.expires_at
                requested = max(lease.expires_at, now) + timedelta(microseconds=1)
                requested = max(requested, now + self.lease_ttl)
                self.service.renew_lease(
                    self.claimed, expected_revision=lease.renewal_revision,
                    expires_at=requested,
                )
                self.consecutive_renewal_failures = 0
                self.last_known_expiry = requested
            except Exception:
                self.renewal_failures += 1
                self.consecutive_renewal_failures += 1
                if (
                    self.consecutive_renewal_failures >= self.max_consecutive_renewal_failures
                    or self.last_known_expiry is not None and now >= self.last_known_expiry
                ):
                    self._cancel("lease_lost")
                    return
