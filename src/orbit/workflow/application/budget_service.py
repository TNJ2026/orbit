"""Atomic Budget Account, Reservation and cumulative Usage ledger."""

from __future__ import annotations

from datetime import datetime
import hashlib
from pathlib import Path

from ..domain.budget import BudgetAccountRecord, BudgetReservationRecord, ReservationStatus
from ..domain.ids import EntityId
from ..domain.serialization import canonical_json
from ..domain.versions import AggregateVersion, Revision
from ..persistence.control import append_control_event, audit
from ..persistence.database import connect_workflow_database


def _entry(kind: str, *parts: object) -> str:
    return "ledger_entry:" + hashlib.sha256("|".join(map(str, (kind, *parts))).encode()).hexdigest()


class BudgetService:
    def __init__(self, path: Path | str) -> None: self.path = Path(path)

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
        self, run_id: EntityId, amount: int, *, actor: str, now: datetime,
        idempotency_key: str | None = None,
    ) -> BudgetAccountRecord:
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
                if prior["amount_microunits"] != amount:
                    raise ValueError("Budget idempotency key reused with different amount")
                return self._account(
                    db.execute(
                        "SELECT * FROM budget_accounts WHERE run_id = ?", (str(run_id),)
                    ).fetchone()
                )
            append_control_event(db, run_id=run_id, aggregate_id=EntityId("budget_account", run_id.value), event_type="budget_added", payload={"amount_microunits": amount}, actor=actor, idempotency_key=f"add:{key}", occurred_at=now)
            cursor = db.execute("UPDATE budget_accounts SET total_microunits=total_microunits+?, aggregate_version=aggregate_version+1, updated_at=? WHERE run_id=?", (amount, now.isoformat(), str(run_id)))
            if cursor.rowcount != 1: raise ValueError("budget account not found")
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
