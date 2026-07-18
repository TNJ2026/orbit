"""Stable usage and budget-accounting invariants.

Persistence and exhaustion policy are intentionally deferred to Step 10.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .ids import EntityId
from .versions import Revision


def _non_negative(name: str, value: int) -> None:
    if isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class UsageSnapshot:
    attempt_id: EntityId
    sequence: Revision
    input_tokens: int
    output_tokens: int
    tool_calls: int
    provider_request_id: str | None
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.attempt_id.kind != "attempt":
            raise ValueError("usage snapshot requires an attempt id")
        _non_negative("input_tokens", self.input_tokens)
        _non_negative("output_tokens", self.output_tokens)
        _non_negative("tool_calls", self.tool_calls)
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")


@dataclass(frozen=True)
class BudgetReservation:
    reservation_id: EntityId
    run_id: EntityId
    amount_microunits: int

    def __post_init__(self) -> None:
        if self.reservation_id.kind != "reservation":
            raise ValueError("budget reservation requires a reservation id")
        if self.run_id.kind != "run":
            raise ValueError("budget reservation requires a run id")
        _non_negative("amount_microunits", self.amount_microunits)
        if self.amount_microunits == 0:
            raise ValueError("reservation amount must be positive")


@dataclass(frozen=True)
class BudgetAccount:
    run_id: EntityId
    total_microunits: int
    reserved_microunits: int = 0
    consumed_microunits: int = 0
    version: Revision = Revision(1)

    def __post_init__(self) -> None:
        if self.run_id.kind != "run":
            raise ValueError("budget account requires a run id")
        _non_negative("total_microunits", self.total_microunits)
        _non_negative("reserved_microunits", self.reserved_microunits)
        _non_negative("consumed_microunits", self.consumed_microunits)
        if self.reserved_microunits > self.total_microunits:
            raise ValueError("reserved budget exceeds total budget")

    @property
    def remaining_microunits(self) -> int:
        return (
            self.total_microunits
            - self.reserved_microunits
            - self.consumed_microunits
        )

    @property
    def is_exhausted(self) -> bool:
        return self.remaining_microunits < 0

    def reserve(self, amount_microunits: int) -> BudgetAccount:
        _non_negative("amount_microunits", amount_microunits)
        if amount_microunits == 0 or amount_microunits > self.remaining_microunits:
            raise ValueError("reservation exceeds remaining budget")
        return BudgetAccount(
            run_id=self.run_id,
            total_microunits=self.total_microunits,
            reserved_microunits=self.reserved_microunits + amount_microunits,
            consumed_microunits=self.consumed_microunits,
            version=self.version.next(),
        )

    def settle(self, reserved_microunits: int, actual_microunits: int) -> BudgetAccount:
        _non_negative("reserved_microunits", reserved_microunits)
        _non_negative("actual_microunits", actual_microunits)
        if reserved_microunits > self.reserved_microunits:
            raise ValueError("cannot settle more than the reserved budget")
        new_reserved = self.reserved_microunits - reserved_microunits
        new_consumed = self.consumed_microunits + actual_microunits
        return BudgetAccount(
            run_id=self.run_id,
            total_microunits=self.total_microunits,
            reserved_microunits=new_reserved,
            consumed_microunits=new_consumed,
            version=self.version.next(),
        )

    def release(self, amount_microunits: int) -> BudgetAccount:
        _non_negative("amount_microunits", amount_microunits)
        if amount_microunits == 0 or amount_microunits > self.reserved_microunits:
            raise ValueError("release exceeds reserved budget")
        return BudgetAccount(
            run_id=self.run_id,
            total_microunits=self.total_microunits,
            reserved_microunits=self.reserved_microunits - amount_microunits,
            consumed_microunits=self.consumed_microunits,
            version=self.version.next(),
        )
