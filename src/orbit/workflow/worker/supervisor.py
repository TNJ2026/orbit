"""Lease renewal and cooperative cancellation for a running Handler."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Event, Thread
from typing import Any

from ..domain.deadlines import MAX_CONSECUTIVE_RENEWAL_FAILURES


# What a renewal failure is called, stably, regardless of which library raised.
# The exception type is kept alongside for a human reading the record; the code
# is what a query groups by, so it must not change when a dependency renames
# its errors.
RENEWAL_UNAVAILABLE = "renewal_unavailable"
RENEWAL_REJECTED = "renewal_rejected"


def _renewal_error_code(exc: BaseException) -> str:
    """Whether the store refused the renewal or could not answer at all.

    The distinction is the whole diagnostic value of this record. A rejected
    renewal means another worker holds the lease — a fencing outcome, working
    as designed. An unavailable one means the database could not be written,
    which is the failure that produced an unexplained `unknown_external_result`
    and the reason this evidence is being collected.
    """

    return RENEWAL_REJECTED if isinstance(exc, ValueError) else RENEWAL_UNAVAILABLE


@dataclass
class RenewalFailureSummary:
    """One failure window, aggregated.

    Aggregated rather than one record per interval: the failures come from a
    database that cannot be written, and a per-interval record would try to
    write to it once every renewal — amplifying the very outage it documents.
    """

    failures: int = 0
    consecutive_failures: int = 0
    first_failure_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error_code: str | None = None
    last_error_type: str | None = None
    last_success_at: datetime | None = None
    known_expiry_at: datetime | None = None
    # Whether cancelling the Handler was dispatched, and how it ended. `settled`
    # stays None until forced settlement exists to set it: an unanswered
    # question is not the same as a negative answer.
    cancel_reason: str | None = None
    termination_dispatched: bool = False
    settled: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def observed(self) -> bool:
        """Whether anything happened worth persisting."""

        return self.failures > 0 or self.cancel_reason is not None

    def to_primitive(self) -> dict[str, Any]:
        def moment(value: datetime | None) -> str | None:
            return None if value is None else value.isoformat()

        return {
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "first_failure_at": moment(self.first_failure_at),
            "last_failure_at": moment(self.last_failure_at),
            "last_error_code": self.last_error_code,
            "last_error_type": self.last_error_type,
            "last_success_at": moment(self.last_success_at),
            "known_expiry_at": moment(self.known_expiry_at),
            "cancel_reason": self.cancel_reason,
            "termination_dispatched": self.termination_dispatched,
            "settled": self.settled,
            **self.extra,
        }


class LeaseSupervisor:
    def __init__(
        self, service, claimed, cancellation_token, *, clock, deadline,
        renew_interval_seconds: float = 10.0,
        lease_ttl: timedelta = timedelta(seconds=30), on_cancel=None,
        max_consecutive_renewal_failures: int = MAX_CONSECUTIVE_RENEWAL_FAILURES,
        metrics=None,
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
        self.metrics = metrics
        self._stop = Event()
        self._thread = None
        # Whether the renewal loop returned rather than died. A supervisor that
        # cancelled on the deadline and returned has done its job; one whose
        # thread is simply gone has not, and only the second needs somebody
        # else to finish the cancellation. Without this flag the two are
        # indistinguishable from outside — both are just a dead thread.
        self._exited_cleanly = False
        # The structured account of this supervisor's life, kept in memory
        # because the thing it records is usually the database being
        # unwritable. Whoever stops this supervisor decides when — and whether
        # — it can be persisted.
        self.summary = RenewalFailureSummary()
        self.last_known_expiry = None

    # Kept as properties so existing readers and tests see the same numbers
    # the summary carries, rather than two counters drifting apart.
    @property
    def renewal_failures(self) -> int:
        return self.summary.failures

    @property
    def consecutive_renewal_failures(self) -> int:
        return self.summary.consecutive_failures

    def _count(self, name: str) -> None:
        if self.metrics is None:
            return
        try:
            self.metrics.increment(name)
        except Exception:  # noqa: BLE001 - reporting must never break renewal
            pass

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
        # First writer wins. A worker cancelling an already-cancelled attempt
        # must not rewrite why it stopped: the first reason is the one that
        # explains the outcome, the second only says somebody noticed later.
        if self.summary.cancel_reason is None:
            self.summary.cancel_reason = reason
        self._count(f"lease_supervisor_cancelled_{reason}")
        self.token.cancel(reason)
        try:
            self.on_cancel()
            # Dispatched, not confirmed: `on_cancel` asks the executor to stop
            # its process group. Whether every descendant is actually gone is
            # the process layer's answer, not this thread's.
            self.summary.termination_dispatched = True
        except Exception:
            self._count("lease_supervisor_termination_failed")

    @property
    def abandoned(self) -> bool:
        """Whether this supervisor died instead of finishing.

        The worker's control thread watches this: a supervisor that is gone
        without having returned leaves nobody renewing the lease and nobody
        watching the deadline, so somebody else has to cancel the Handler and
        settle the attempt.
        """

        thread = self._thread
        if thread is None:
            return False
        return not thread.is_alive() and not self._exited_cleanly

    def cancel_now(self, reason: str) -> None:
        """Cancel from outside this thread, when this thread can no longer.

        Idempotent through the token: a second cancel on an already-cancelled
        attempt changes nothing but the recorded reason, which stays the first
        one written.
        """

        self._cancel(reason)

    def _run(self) -> None:
        try:
            self._renew_until_done()
            self._exited_cleanly = True
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
                self.summary.consecutive_failures = 0
                self.summary.last_success_at = now
                self.last_known_expiry = requested
                self.summary.known_expiry_at = requested
            except Exception as exc:
                self._record_failure(exc, now)
                if (
                    self.summary.consecutive_failures >= self.max_consecutive_renewal_failures
                    or self.last_known_expiry is not None and now >= self.last_known_expiry
                ):
                    self._cancel("lease_lost")
                    return

    def _record_failure(self, exc: BaseException, now) -> None:
        """Account for one failed renewal, without writing anywhere durable."""

        summary = self.summary
        summary.failures += 1
        summary.consecutive_failures += 1
        summary.last_error_code = _renewal_error_code(exc)
        summary.last_error_type = type(exc).__name__
        summary.last_failure_at = now
        if summary.first_failure_at is None:
            summary.first_failure_at = now
        summary.known_expiry_at = self.last_known_expiry
        self._count("lease_renewal_failed")
        self._count(f"lease_renewal_failed_{summary.last_error_code}")
