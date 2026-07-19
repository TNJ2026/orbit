"""Atomic Budget Account, Reservation and cumulative Usage ledger."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path

from ..domain.budget import BudgetAccountRecord, BudgetReservationRecord, ReservationStatus
from ..domain.ids import EntityId
from ..domain.serialization import canonical_json
from ..domain.versions import AggregateVersion, Revision
from ..persistence.control import append_control_event, audit
from ..persistence.database import connect_workflow_database


class BudgetVersionConflict(ValueError):
    """The caller acted on a budget account that has since moved on."""


def _entry(kind: str, *parts: object) -> str:
    return "ledger_entry:" + hashlib.sha256("|".join(map(str, (kind, *parts))).encode()).hexdigest()


def derive_budget_reservation_id(run_id: EntityId, owner_id: EntityId) -> EntityId:
    return EntityId(
        "reservation", hashlib.sha256(f"{run_id}|{owner_id}".encode()).hexdigest()
    )


def ensure_budget_account_in_uow(
    db, run_id: EntityId, total: int, *, actor: str, now: datetime,
) -> BudgetAccountRecord:
    """Open a Run account inside an existing Kernel transaction.

    Controllers use this boundary so child creation and its budget transfer
    cannot commit independently. An existing Run account remains authoritative;
    ``total`` is only the initial allocation when no account exists yet.
    """
    if total < 0:
        raise ValueError("budget total must be non-negative")
    prior = db.execute(
        "SELECT * FROM budget_accounts WHERE run_id=?", (str(run_id),)
    ).fetchone()
    if prior is not None:
        return BudgetService._account(prior)
    account_id = EntityId("budget_account", run_id.value)
    event = append_control_event(
        db, run_id=run_id, aggregate_id=account_id,
        event_type="budget_account_opened",
        payload={"total_microunits": total}, actor=actor,
        idempotency_key=f"open:{total}", occurred_at=now,
    )
    db.execute(
        "INSERT INTO budget_accounts VALUES (?, ?, 0, 0, 1, ?)",
        (str(run_id), total, now.isoformat()),
    )
    db.execute(
        "INSERT INTO budget_ledger_entries VALUES (?, ?, NULL, 'account_opened', ?, NULL, ?, '{}')",
        (_entry("open", run_id, total), str(run_id), total, now.isoformat()),
    )
    audit(
        db, run_id=run_id, actor=actor, action="budget.open",
        target_id=str(run_id), decision="allowed",
        details={"total": total, "event_id": str(event.event_id)},
        occurred_at=now,
    )
    return BudgetAccountRecord(run_id, total, 0, 0, AggregateVersion(1))


def reserve_budget_in_uow(
    db, run_id: EntityId, owner_id: EntityId, amount: int, *,
    actor: str, now: datetime,
) -> BudgetReservationRecord:
    """Reserve parent budget atomically with controller state changes."""
    if amount <= 0:
        raise ValueError("reservation must be positive")
    reservation_id = derive_budget_reservation_id(run_id, owner_id)
    prior = db.execute(
        "SELECT * FROM budget_reservations WHERE run_id=? AND owner_id=?",
        (str(run_id), str(owner_id)),
    ).fetchone()
    if prior is not None:
        if prior["reserved_microunits"] != amount:
            raise ValueError("reservation identity conflict")
        return BudgetService._reservation(prior)
    account = db.execute(
        "SELECT * FROM budget_accounts WHERE run_id=?", (str(run_id),)
    ).fetchone()
    if account is None:
        raise ValueError("budget account not found")
    remaining = (
        account["total_microunits"] - account["reserved_microunits"]
        - account["consumed_microunits"]
    )
    if amount > remaining:
        raise ValueError("reservation exceeds remaining budget")
    append_control_event(
        db, run_id=run_id, aggregate_id=reservation_id,
        event_type="budget_reserved",
        payload={"owner_id": str(owner_id), "amount_microunits": amount},
        actor=actor, idempotency_key=f"reserve:{amount}", occurred_at=now,
    )
    db.execute(
        "UPDATE budget_accounts SET reserved_microunits=reserved_microunits+?, aggregate_version=aggregate_version+1, updated_at=? WHERE run_id=?",
        (amount, now.isoformat(), str(run_id)),
    )
    db.execute(
        "INSERT INTO budget_reservations VALUES (?, ?, ?, ?, 0, 0, 'active', ?, ?)",
        (str(reservation_id), str(run_id), str(owner_id), amount,
         now.isoformat(), now.isoformat()),
    )
    db.execute(
        "INSERT INTO budget_ledger_entries VALUES (?, ?, ?, 'reserved', ?, NULL, ?, '{}')",
        (_entry("reserve", reservation_id), str(run_id), str(reservation_id),
         amount, now.isoformat()),
    )
    return BudgetReservationRecord(
        reservation_id, run_id, owner_id, amount, 0, 0, ReservationStatus.ACTIVE
    )


def settle_budget_transfer_in_uow(
    db, reservation_id: EntityId, cumulative_amount: int, *,
    actor: str, now: datetime, unknown: bool = False,
) -> BudgetAccountRecord:
    """Report a child allocation's actual usage and release its reservation."""
    if cumulative_amount < 0:
        raise ValueError("invalid cumulative usage")
    row = db.execute(
        "SELECT * FROM budget_reservations WHERE reservation_id=?",
        (str(reservation_id),),
    ).fetchone()
    if row is None:
        raise ValueError("reservation not found")
    run_id = EntityId.parse(row["run_id"])
    if row["status"] in {"settled", "released", "unknown"}:
        return BudgetService._account(db.execute(
            "SELECT * FROM budget_accounts WHERE run_id=?", (str(run_id),)
        ).fetchone())
    if cumulative_amount < row["consumed_microunits"]:
        raise ValueError("cumulative usage cannot decrease")
    delta = cumulative_amount - row["consumed_microunits"]
    append_control_event(
        db, run_id=run_id, aggregate_id=reservation_id,
        event_type="budget_usage_reported",
        payload={"sequence": 1, "cumulative_microunits": cumulative_amount,
                 "delta_microunits": delta},
        actor=actor, idempotency_key="usage:1", occurred_at=now,
    )
    db.execute(
        "UPDATE budget_reservations SET consumed_microunits=?, last_usage_sequence=1, updated_at=? WHERE reservation_id=?",
        (cumulative_amount, now.isoformat(), str(reservation_id)),
    )
    db.execute(
        "UPDATE budget_accounts SET consumed_microunits=consumed_microunits+?, aggregate_version=aggregate_version+1, updated_at=? WHERE run_id=?",
        (delta, now.isoformat(), str(run_id)),
    )
    db.execute(
        "INSERT OR IGNORE INTO budget_ledger_entries VALUES (?, ?, ?, 'usage', ?, 1, ?, '{}')",
        (_entry("usage", reservation_id, 1), str(run_id), str(reservation_id),
         delta, now.isoformat()),
    )
    status = "unknown" if unknown else "settled"
    append_control_event(
        db, run_id=run_id, aggregate_id=reservation_id,
        event_type="budget_reservation_settled",
        payload={"status": status,
                 "reserved_microunits": row["reserved_microunits"],
                 "consumed_microunits": cumulative_amount},
        actor=actor, idempotency_key=f"settle:{status}", occurred_at=now,
    )
    db.execute(
        "UPDATE budget_reservations SET status=?, updated_at=? WHERE reservation_id=?",
        (status, now.isoformat(), str(reservation_id)),
    )
    db.execute(
        "UPDATE budget_accounts SET reserved_microunits=reserved_microunits-?, aggregate_version=aggregate_version+1, updated_at=? WHERE run_id=?",
        (row["reserved_microunits"], now.isoformat(), str(run_id)),
    )
    db.execute(
        "INSERT OR IGNORE INTO budget_ledger_entries VALUES (?, ?, ?, 'settled', ?, NULL, ?, '{}')",
        (_entry("settle", reservation_id, status), str(run_id),
         str(reservation_id), cumulative_amount, now.isoformat()),
    )
    return BudgetService._account(db.execute(
        "SELECT * FROM budget_accounts WHERE run_id=?", (str(run_id),)
    ).fetchone())


class BudgetService:
    def __init__(self, path: Path | str) -> None: self.path = Path(path)

    ensure_account_in_uow = staticmethod(ensure_budget_account_in_uow)
    reserve_in_uow = staticmethod(reserve_budget_in_uow)
    settle_transfer_in_uow = staticmethod(settle_budget_transfer_in_uow)
    derive_reservation_id = staticmethod(derive_budget_reservation_id)

    def get_account(self, run_id: EntityId) -> BudgetAccountRecord | None:
        """Return the current account without creating or advancing it."""
        with connect_workflow_database(self.path, read_only=True) as db:
            row = db.execute(
                "SELECT * FROM budget_accounts WHERE run_id=?", (str(run_id),)
            ).fetchone()
            return None if row is None else self._account(row)

    def reconcile_planner_reservations(self, *, actor: str, now: datetime) -> int:
        """Settle reservations left behind by a dispatcher/process crash."""
        with connect_workflow_database(self.path, read_only=True) as db:
            rows = tuple(db.execute(
                """SELECT r.reservation_id,a.status,a.usage_json
                   FROM budget_reservations r
                   JOIN planner_attempts a ON a.attempt_id=r.owner_id
                   WHERE r.status='active'
                     AND a.status IN ('response_received','accepted','rejected',
                                      'unknown','failed')
                   ORDER BY r.reservation_id"""
            ))
        for row in rows:
            reservation_id = EntityId.parse(row["reservation_id"])
            usage = None if row["usage_json"] is None else json.loads(row["usage_json"])
            if usage is not None:
                self.report_usage(
                    reservation_id, 1, int(usage.get("cost_microunits", 0)),
                    actor=actor, now=now,
                )
            if row["status"] == "unknown":
                self.settle(reservation_id, actor=actor, now=now, unknown=True)
            elif usage is None:
                self.release(reservation_id, actor=actor, now=now)
            else:
                self.settle(reservation_id, actor=actor, now=now)
        return len(rows)

    def open_account(self, run_id: EntityId, total: int, *, actor: str, now: datetime) -> BudgetAccountRecord:
        if total < 0: raise ValueError("budget total must be non-negative")
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM budget_accounts WHERE run_id=?", (str(run_id),)).fetchone()
            if prior is not None:
                if prior["total_microunits"] != total: raise ValueError("budget account already exists with different total")
                return self._account(prior)
            account_id = EntityId("budget_account", run_id.value)
            event = append_control_event(db, run_id=run_id, aggregate_id=account_id, event_type="budget_account_opened", payload={"total_microunits": total}, actor=actor, idempotency_key=f"open:{total}", occurred_at=now)
            db.execute("INSERT INTO budget_accounts VALUES (?, ?, 0, 0, 1, ?)", (str(run_id), total, now.isoformat()))
            db.execute("INSERT INTO budget_ledger_entries VALUES (?, ?, NULL, 'account_opened', ?, NULL, ?, '{}')", (_entry("open", run_id, total), str(run_id), total, now.isoformat()))
            audit(db, run_id=run_id, actor=actor, action="budget.open", target_id=str(run_id), decision="allowed", details={"total": total, "event_id": str(event.event_id)}, occurred_at=now)
            db.commit(); return BudgetAccountRecord(run_id, total, 0, 0, AggregateVersion(1))

    def reserve(self, run_id: EntityId, owner_id: EntityId, amount: int, *, actor: str, now: datetime) -> BudgetReservationRecord:
        if amount <= 0: raise ValueError("reservation must be positive")
        reservation_id = EntityId("reservation", hashlib.sha256(f"{run_id}|{owner_id}".encode()).hexdigest())
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM budget_reservations WHERE run_id=? AND owner_id=?", (str(run_id), str(owner_id))).fetchone()
            if prior is not None:
                if prior["reserved_microunits"] != amount: raise ValueError("reservation identity conflict")
                return self._reservation(prior)
            account = db.execute("SELECT * FROM budget_accounts WHERE run_id=?", (str(run_id),)).fetchone()
            if account is None: raise ValueError("budget account not found")
            remaining = account["total_microunits"] - account["reserved_microunits"] - account["consumed_microunits"]
            if amount > remaining: raise ValueError("reservation exceeds remaining budget")
            append_control_event(db, run_id=run_id, aggregate_id=reservation_id, event_type="budget_reserved", payload={"owner_id": str(owner_id), "amount_microunits": amount}, actor=actor, idempotency_key=f"reserve:{amount}", occurred_at=now)
            db.execute("UPDATE budget_accounts SET reserved_microunits=reserved_microunits+?, aggregate_version=aggregate_version+1, updated_at=? WHERE run_id=?", (amount, now.isoformat(), str(run_id)))
            db.execute("INSERT INTO budget_reservations VALUES (?, ?, ?, ?, 0, 0, 'active', ?, ?)", (str(reservation_id), str(run_id), str(owner_id), amount, now.isoformat(), now.isoformat()))
            db.execute("INSERT INTO budget_ledger_entries VALUES (?, ?, ?, 'reserved', ?, NULL, ?, '{}')", (_entry("reserve", reservation_id), str(run_id), str(reservation_id), amount, now.isoformat()))
            db.commit(); return BudgetReservationRecord(reservation_id, run_id, owner_id, amount, 0, 0, ReservationStatus.ACTIVE)

    def report_usage(self, reservation_id: EntityId, sequence: int, cumulative_amount: int, *, actor: str, now: datetime) -> BudgetAccountRecord:
        if sequence < 1 or cumulative_amount < 0: raise ValueError("invalid cumulative usage")
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            reservation = db.execute("SELECT * FROM budget_reservations WHERE reservation_id=?", (str(reservation_id),)).fetchone()
            if reservation is None or reservation["status"] not in {"active", "unknown"}: raise ValueError("reservation cannot accept usage")
            if sequence == reservation["last_usage_sequence"]:
                if cumulative_amount != reservation["consumed_microunits"]:
                    raise ValueError("same usage sequence has different cumulative amount")
                account = db.execute("SELECT * FROM budget_accounts WHERE run_id=?", (reservation["run_id"],)).fetchone()
                return self._account(account)
            if sequence < reservation["last_usage_sequence"]:
                account = db.execute("SELECT * FROM budget_accounts WHERE run_id=?", (reservation["run_id"],)).fetchone()
                return self._account(account)
            if cumulative_amount < reservation["consumed_microunits"]: raise ValueError("cumulative usage cannot decrease")
            delta = cumulative_amount - reservation["consumed_microunits"]
            run_id = EntityId.parse(reservation["run_id"])
            append_control_event(db, run_id=run_id, aggregate_id=reservation_id, event_type="budget_usage_reported", payload={"sequence": sequence, "cumulative_microunits": cumulative_amount, "delta_microunits": delta}, actor=actor, idempotency_key=f"usage:{sequence}", occurred_at=now)
            db.execute("UPDATE budget_reservations SET consumed_microunits=?, last_usage_sequence=?, updated_at=? WHERE reservation_id=?", (cumulative_amount, sequence, now.isoformat(), str(reservation_id)))
            db.execute("UPDATE budget_accounts SET consumed_microunits=consumed_microunits+?, aggregate_version=aggregate_version+1, updated_at=? WHERE run_id=?", (delta, now.isoformat(), str(run_id)))
            db.execute("INSERT INTO budget_ledger_entries VALUES (?, ?, ?, 'usage', ?, ?, ?, '{}')", (_entry("usage", reservation_id, sequence), str(run_id), str(reservation_id), delta, sequence, now.isoformat()))
            account = db.execute("SELECT * FROM budget_accounts WHERE run_id=?", (str(run_id),)).fetchone()
            if account["total_microunits"] - account["reserved_microunits"] - account["consumed_microunits"] < 0:
                db.execute("UPDATE workflow_runs SET status='budget_exhausted', aggregate_version=aggregate_version+1, updated_at=? WHERE run_id=? AND status IN ('running','waiting')", (now.isoformat(), str(run_id)))
            db.commit(); return self._account(account)

    def settle(self, reservation_id: EntityId, *, actor: str, now: datetime, unknown: bool = False) -> BudgetAccountRecord:
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM budget_reservations WHERE reservation_id=?", (str(reservation_id),)).fetchone()
            if row is None: raise ValueError("reservation not found")
            if row["status"] in {"settled", "released"}:
                return self._account(db.execute("SELECT * FROM budget_accounts WHERE run_id=?", (row["run_id"],)).fetchone())
            status = "unknown" if unknown else "settled"; run_id = EntityId.parse(row["run_id"])
            append_control_event(db, run_id=run_id, aggregate_id=reservation_id, event_type="budget_reservation_settled", payload={"status": status, "reserved_microunits": row["reserved_microunits"], "consumed_microunits": row["consumed_microunits"]}, actor=actor, idempotency_key=f"settle:{status}", occurred_at=now)
            db.execute("UPDATE budget_reservations SET status=?, updated_at=? WHERE reservation_id=?", (status, now.isoformat(), str(reservation_id)))
            db.execute("UPDATE budget_accounts SET reserved_microunits=reserved_microunits-?, aggregate_version=aggregate_version+1, updated_at=? WHERE run_id=?", (row["reserved_microunits"], now.isoformat(), str(run_id)))
            db.execute("INSERT OR IGNORE INTO budget_ledger_entries VALUES (?, ?, ?, 'settled', ?, NULL, ?, '{}')", (_entry("settle", reservation_id, status), str(run_id), str(reservation_id), row["consumed_microunits"], now.isoformat()))
            db.commit(); return self._account(db.execute("SELECT * FROM budget_accounts WHERE run_id=?", (str(run_id),)).fetchone())

    def release(self, reservation_id: EntityId, *, actor: str, now: datetime) -> BudgetAccountRecord:
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE"); row=db.execute("SELECT * FROM budget_reservations WHERE reservation_id=?",(str(reservation_id),)).fetchone()
            if row is None:raise ValueError("reservation not found")
            if row["status"]=="released":return self._account(db.execute("SELECT * FROM budget_accounts WHERE run_id=?",(row["run_id"],)).fetchone())
            if row["status"]!="active" or row["consumed_microunits"]:raise ValueError("only unused active reservations can be released")
            run_id=EntityId.parse(row["run_id"]);append_control_event(db,run_id=run_id,aggregate_id=reservation_id,event_type="budget_reservation_released",payload={"reserved_microunits":row["reserved_microunits"]},actor=actor,idempotency_key="release",occurred_at=now)
            db.execute("UPDATE budget_reservations SET status='released',updated_at=? WHERE reservation_id=?",(now.isoformat(),str(reservation_id)));db.execute("UPDATE budget_accounts SET reserved_microunits=reserved_microunits-?,aggregate_version=aggregate_version+1,updated_at=? WHERE run_id=?",(row["reserved_microunits"],now.isoformat(),str(run_id)));db.execute("INSERT OR IGNORE INTO budget_ledger_entries VALUES (?, ?, ?, 'released', ?, NULL, ?, '{}')",(_entry("release",reservation_id),str(run_id),str(reservation_id),row["reserved_microunits"],now.isoformat()));db.commit();return self._account(db.execute("SELECT * FROM budget_accounts WHERE run_id=?",(str(run_id),)).fetchone())

    def add_budget(
        self, run_id: EntityId, amount: int, *, expected_version: int, actor: str,
        now: datetime, idempotency_key: str | None = None,
    ) -> BudgetAccountRecord:
        """Top the account up, refusing if it moved since the caller looked.

        `expected_version` is required rather than optional: two operators
        looking at the same exhausted run would otherwise both grant, and the
        second grant would silently stack on top of the first instead of
        telling its author that the situation had changed.
        """

        if amount <= 0: raise ValueError("additional budget must be positive")
        key = idempotency_key or f"legacy:{amount}:{now.isoformat()}"
        if not key.strip():
            raise ValueError("Budget addition idempotency key is required")
        entry_id = _entry("add", run_id, key)
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT amount_microunits FROM budget_ledger_entries WHERE entry_id = ?",
                (entry_id,),
            ).fetchone()
            if prior is not None:
                # Replay is checked before the version, and deliberately does
                # not check it: this grant already happened, so the account has
                # moved past the version the caller is holding. Comparing here
                # would reject the retry of a command that succeeded.
                if prior["amount_microunits"] != amount:
                    raise ValueError("Budget idempotency key reused with different amount")
                return self._account(
                    db.execute(
                        "SELECT * FROM budget_accounts WHERE run_id = ?", (str(run_id),)
                    ).fetchone()
                )
            current = db.execute(
                "SELECT aggregate_version FROM budget_accounts WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            if current is None: raise ValueError("budget account not found")
            if current["aggregate_version"] != expected_version:
                raise BudgetVersionConflict(
                    f"budget account is at version {current['aggregate_version']},"
                    f" not {expected_version}"
                )
            append_control_event(db, run_id=run_id, aggregate_id=EntityId("budget_account", run_id.value), event_type="budget_added", payload={"amount_microunits": amount}, actor=actor, idempotency_key=f"add:{key}", occurred_at=now)
            cursor = db.execute("UPDATE budget_accounts SET total_microunits=total_microunits+?, aggregate_version=aggregate_version+1, updated_at=? WHERE run_id=? AND aggregate_version=?", (amount, now.isoformat(), str(run_id), expected_version))
            if cursor.rowcount != 1: raise BudgetVersionConflict("budget account changed concurrently")
            db.execute(
                "INSERT INTO budget_ledger_entries VALUES (?, ?, NULL, 'budget_added', ?, NULL, ?, ?)",
                (
                    entry_id,
                    str(run_id),
                    amount,
                    now.isoformat(),
                    canonical_json({"idempotency": key}),
                ),
            )
            db.execute("UPDATE workflow_runs SET status='running', aggregate_version=aggregate_version+1, updated_at=? WHERE run_id=? AND status IN ('budget_exhausted','waiting_for_budget')", (now.isoformat(), str(run_id)))
            db.commit(); return self._account(db.execute("SELECT * FROM budget_accounts WHERE run_id=?", (str(run_id),)).fetchone())

    @staticmethod
    def _account(row): return BudgetAccountRecord(EntityId.parse(row["run_id"]), row["total_microunits"], row["reserved_microunits"], row["consumed_microunits"], AggregateVersion(row["aggregate_version"]))
    @staticmethod
    def _reservation(row): return BudgetReservationRecord(EntityId.parse(row["reservation_id"]), EntityId.parse(row["run_id"]), EntityId.parse(row["owner_id"]), row["reserved_microunits"], row["consumed_microunits"], row["last_usage_sequence"], ReservationStatus(row["status"]))
