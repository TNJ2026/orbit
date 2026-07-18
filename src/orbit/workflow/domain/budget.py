"""Persistent budget ledger contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .ids import EntityId
from .versions import AggregateVersion, Revision


class ReservationStatus(str, Enum):
    ACTIVE = "active"
    SETTLED = "settled"
    RELEASED = "released"
    UNKNOWN = "unknown"


class LedgerEntryKind(str, Enum):
    ACCOUNT_OPENED = "account_opened"
    RESERVED = "reserved"
    USAGE = "usage"
    SETTLED = "settled"
    RELEASED = "released"
    BUDGET_ADDED = "budget_added"


@dataclass(frozen=True)
class BudgetAccountRecord:
    run_id: EntityId
    total_microunits: int
    reserved_microunits: int
    consumed_microunits: int
    version: AggregateVersion

    @property
    def remaining_microunits(self) -> int:
        return self.total_microunits - self.reserved_microunits - self.consumed_microunits

    @property
    def exhausted(self) -> bool:
        return self.remaining_microunits < 0


@dataclass(frozen=True)
class BudgetReservationRecord:
    reservation_id: EntityId
    run_id: EntityId
    owner_id: EntityId
    reserved_microunits: int
    consumed_microunits: int
    last_usage_sequence: int
    status: ReservationStatus


@dataclass(frozen=True)
class BudgetLedgerEntry:
    entry_id: EntityId
    run_id: EntityId
    reservation_id: EntityId | None
    kind: LedgerEntryKind
    amount_microunits: int
    usage_sequence: Revision | None
    occurred_at: datetime
