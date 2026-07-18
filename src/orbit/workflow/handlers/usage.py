"""Deduplicating cumulative UsageSnapshot reporters."""

from __future__ import annotations

from threading import Lock

from ..domain.accounting import UsageSnapshot
from ..domain.ids import EntityId


class UsageConflictError(ValueError):
    pass


def _check_progression(previous: UsageSnapshot, current: UsageSnapshot) -> None:
    if current.observed_at < previous.observed_at:
        raise UsageConflictError("usage observed_at cannot move backwards")
    if current.provider_request_id is not None and previous.provider_request_id not in {
        None, current.provider_request_id,
    }:
        raise UsageConflictError("provider_request_id cannot change")
    for field in ("input_tokens", "output_tokens", "tool_calls"):
        if getattr(current, field) < getattr(previous, field):
            raise UsageConflictError(f"cumulative {field} cannot decrease")


class InMemoryUsageReporter:
    def __init__(self) -> None:
        self._latest: dict[EntityId, UsageSnapshot] = {}
        self._by_sequence: dict[tuple[EntityId, int], UsageSnapshot] = {}
        self._lock = Lock()

    def report(self, snapshot: UsageSnapshot) -> bool:
        key = (snapshot.attempt_id, snapshot.sequence.value)
        with self._lock:
            known = self._by_sequence.get(key)
            if known is not None:
                if known != snapshot:
                    raise UsageConflictError("same usage sequence has different content")
                return False
            previous = self._latest.get(snapshot.attempt_id)
            if previous is not None:
                if snapshot.sequence.value < previous.sequence.value:
                    self._by_sequence[key] = snapshot
                    return False
                _check_progression(previous, snapshot)
            self._by_sequence[key] = snapshot
            self._latest[snapshot.attempt_id] = snapshot
            return True

    def latest(self, attempt_id: EntityId) -> UsageSnapshot | None:
        with self._lock:
            return self._latest.get(attempt_id)


class NoopUsageReporter:
    def report(self, snapshot: UsageSnapshot) -> bool:
        return False

    def latest(self, attempt_id: EntityId) -> UsageSnapshot | None:
        return None


class PersistentBudgetUsageReporter:
    """Bridges cumulative Handler usage to the durable Budget ledger."""
    def __init__(self, budget_service, reservation_by_attempt, cost_estimator, *, actor="system:usage") -> None:
        self.budget_service=budget_service;self.reservation_by_attempt=reservation_by_attempt;self.cost_estimator=cost_estimator;self.actor=actor;self._memory=InMemoryUsageReporter()
    def report(self,snapshot:UsageSnapshot)->bool:
        accepted=self._memory.report(snapshot)
        if not accepted:return False
        reservation_id=self.reservation_by_attempt(snapshot.attempt_id)
        if reservation_id is None:raise ValueError("Attempt has no Budget Reservation")
        cumulative=int(self.cost_estimator(snapshot))
        self.budget_service.report_usage(reservation_id,snapshot.sequence.value,cumulative,actor=self.actor,now=snapshot.observed_at)
        return True
    def latest(self,attempt_id:EntityId)->UsageSnapshot|None:return self._memory.latest(attempt_id)
