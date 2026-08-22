"""Runtime-owned durable hand-off to a Harness-managed subagent.

The Handler is fixed before the execution registry seals. Provider names are
data in the delegation request, so installing another Harness Provider never
mutates the sealed Orbit registry. The queue is also the idempotency boundary:
one deterministic delegation id can be claimed at most once, and an expired
lease becomes unknown rather than being offered to a second Agent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping

from ..catalogs import HandlerManifest
from ..domain.durable_execution import ExecutionSafety
from ..domain.handlers import (
    CancelAck, CancelDisposition, ExternalEffect, HandlerPermanentError,
    HandlerResult, HandlerResultStatus, HandlerValidationIssue,
    HandlerValidationResult, PreparedExecution, RawHandlerResult,
    RecoveryDisposition, RecoveryResult, ResourceProfile,
    UnknownExternalResultError,
)
from ..domain.serialization import canonical_json


HARNESS_SUBAGENT_MANIFEST = HandlerManifest(
    "harness.subagent", "1.0.0", ("action",),
    {"task": "schema://object/1.0"}, {"result": "schema://object/1.0"},
    {
        "type": "object",
        "properties": {
            "provider": {"type": "string", "minLength": 1},
            "max_wall_seconds": {"type": "integer", "minimum": 1, "maximum": 7200},
            "effects": {"type": "string", "enum": ["read", "write"]},
            "isolation_mode": {
                "type": "string",
                "enum": ["shared", "exclusive", "worktree", "snapshot"],
            },
            "max_concurrency": {"type": "integer", "minimum": 1, "maximum": 32},
        },
        "required": ["provider"], "additionalProperties": False,
    },
    ExecutionSafety.UNKNOWN_ON_LEASE_LOSS,
    ResourceProfile(0, 0, 0, 7200, 0, "harness-subagent"),
    "schema://object/1.0", ("agent.invoke",), (), True, True,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


class DelegationQueue:
    def __init__(self, path: Path | str, *, require_execution_lease: bool = True) -> None:
        self.path = Path(path)
        self.require_execution_lease = require_execution_lease
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS harness_delegations("
                "delegation_id TEXT PRIMARY KEY,actor TEXT NOT NULL,status TEXT NOT NULL,"
                "request_json TEXT NOT NULL,result_json TEXT,error TEXT,"
                "lease_owner TEXT,lease_expires_at TEXT,cancel_requested INTEGER NOT NULL DEFAULT 0,"
                "created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS harness_delegations_claim"
                " ON harness_delegations(actor,status,created_at)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS harness_execution_leases("
                "actor TEXT PRIMARY KEY,lease_id TEXT NOT NULL,workspace_id TEXT NOT NULL,"
                "allowed_providers_json TEXT NOT NULL,max_delegations INTEGER NOT NULL,"
                "used_delegations INTEGER NOT NULL,max_wall_seconds INTEGER NOT NULL,"
                "expires_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS harness_delegation_reconciliations("
                "delegation_id TEXT PRIMARY KEY,actor TEXT NOT NULL,outcome TEXT NOT NULL,"
                "note TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL)"
            )
            db.commit()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _dto(row) -> dict[str, Any]:
        return {
            "delegation_id": row["delegation_id"], "status": row["status"],
            "request": json.loads(row["request_json"]),
            "result": None if row["result_json"] is None else json.loads(row["result_json"]),
            "error": row["error"], "lease_expires_at": row["lease_expires_at"],
            "cancel_requested": bool(row["cancel_requested"]),
        }

    def enqueue(self, delegation_id: str, *, actor: str, request: Mapping[str, Any]):
        encoded, stamp = canonical_json(request), _stamp(_now())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM harness_delegations WHERE delegation_id=?",
                (delegation_id,),
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO harness_delegations"
                    "(delegation_id,actor,status,request_json,created_at,updated_at)"
                    " VALUES (?,?,'queued',?,?,?)",
                    (delegation_id, actor, encoded, stamp, stamp),
                )
            elif row["actor"] != actor or row["request_json"] != encoded:
                db.rollback()
                raise ValueError("delegation id was reused with a different request")
            db.commit()
        return self.get(delegation_id, actor=actor)

    def configure_execution_lease(
        self, *, actor: str, lease_id: str, workspace_id: str,
        allowed_providers, max_delegations: int, max_wall_seconds: int,
        expires_at: str,
    ) -> Mapping[str, Any]:
        providers = tuple(sorted(set(str(item).strip() for item in allowed_providers)))
        if not actor.strip() or not lease_id.strip() or not workspace_id.strip():
            raise ValueError("actor, lease_id and workspace_id are required")
        if any(not item for item in providers):
            raise ValueError("allowed_providers must contain only non-empty names")
        if not 1 <= max_delegations <= 10_000:
            raise ValueError("max_delegations must be between 1 and 10000")
        if not 1 <= max_wall_seconds <= 7200:
            raise ValueError("max_wall_seconds must be between 1 and 7200")
        current = _now()
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError("expires_at must be an ISO-8601 timestamp") from exc
        if expiry.tzinfo is None or expiry <= current:
            raise ValueError("execution lease must expire in the future")
        if expiry > current + timedelta(hours=24):
            raise ValueError("execution lease cannot exceed 24 hours")
        stamp = _stamp(current)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT * FROM harness_execution_leases WHERE actor=?", (actor,),
            ).fetchone()
            if (
                prior is not None and prior["lease_id"] != lease_id
                and prior["expires_at"] > stamp
            ):
                db.rollback()
                raise ValueError("actor already has a different active execution lease")
            if prior is not None and prior["lease_id"] == lease_id:
                if prior["workspace_id"] != workspace_id:
                    db.rollback(); raise ValueError("execution lease Workspace cannot change")
                if tuple(json.loads(prior["allowed_providers_json"])) != providers:
                    db.rollback(); raise ValueError("execution lease Provider allowlist cannot change")
                if (
                    int(prior["max_delegations"]) != max_delegations
                    or int(prior["max_wall_seconds"]) != max_wall_seconds
                ):
                    db.rollback(); raise ValueError("execution lease budgets cannot change")
            used = 0 if prior is None or prior["lease_id"] != lease_id else int(prior["used_delegations"])
            db.execute(
                "INSERT INTO harness_execution_leases VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(actor) DO UPDATE SET lease_id=excluded.lease_id,"
                "workspace_id=excluded.workspace_id,allowed_providers_json=excluded.allowed_providers_json,"
                "max_delegations=excluded.max_delegations,used_delegations=excluded.used_delegations,"
                "max_wall_seconds=excluded.max_wall_seconds,expires_at=excluded.expires_at,updated_at=excluded.updated_at",
                (actor, lease_id, workspace_id, canonical_json(providers), max_delegations,
                 used, max_wall_seconds, _stamp(expiry), stamp),
            )
            db.commit()
        return self.execution_lease(actor=actor)

    def execution_lease(self, *, actor: str) -> Mapping[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM harness_execution_leases WHERE actor=?", (actor,),
            ).fetchone()
        if row is None:
            raise LookupError("execution lease not found")
        return {
            "lease_id": row["lease_id"], "workspace_id": row["workspace_id"],
            "allowed_providers": json.loads(row["allowed_providers_json"]),
            "max_delegations": int(row["max_delegations"]),
            "used_delegations": int(row["used_delegations"]),
            "max_wall_seconds": int(row["max_wall_seconds"]),
            "expires_at": row["expires_at"],
        }

    def reconcile(
        self, delegation_id: str, *, actor: str, outcome: str, note: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        if outcome not in {"confirmed_succeeded", "confirmed_failed"}:
            raise ValueError("outcome must be confirmed_succeeded or confirmed_failed")
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        if len(note) > 4000:
            raise ValueError("reconciliation note is too long")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM harness_delegation_reconciliations"
                " WHERE delegation_id=? OR idempotency_key=?",
                (delegation_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if (
                    existing["delegation_id"] != delegation_id
                    or existing["actor"] != actor or existing["outcome"] != outcome
                    or existing["note"] != note
                ):
                    db.rollback()
                    raise ValueError("delegation was already reconciled differently")
                db.commit()
                return dict(existing)
            job = db.execute(
                "SELECT status FROM harness_delegations"
                " WHERE delegation_id=? AND actor=?", (delegation_id, actor),
            ).fetchone()
            if job is None:
                db.rollback(); raise LookupError("delegation not found")
            if job["status"] != "unknown":
                db.rollback(); raise ValueError("only an unknown delegation can be reconciled")
            created_at = _stamp(_now())
            db.execute(
                "INSERT INTO harness_delegation_reconciliations VALUES (?,?,?,?,?,?)",
                (delegation_id, actor, outcome, note, idempotency_key, created_at),
            )
            db.commit()
        return self.reconciliation(delegation_id, actor=actor)

    def reconciliation(self, delegation_id: str, *, actor: str):
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM harness_delegation_reconciliations"
                " WHERE delegation_id=? AND actor=?", (delegation_id, actor),
            ).fetchone()
        return None if row is None else dict(row)

    def stats(self, *, actor: str) -> Mapping[str, Any]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT status,COUNT(*) AS count FROM harness_delegations"
                " WHERE actor=? GROUP BY status", (actor,),
            ).fetchall()
            reconciled = db.execute(
                "SELECT COUNT(*) FROM harness_delegation_reconciliations WHERE actor=?",
                (actor,),
            ).fetchone()[0]
        return {
            "actor": actor,
            "by_status": {str(row["status"]): int(row["count"]) for row in rows},
            "reconciled": int(reconciled),
        }

    def prune(self, *, before: str, limit: int = 100) -> Mapping[str, Any]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        try:
            cutoff = datetime.fromisoformat(before.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError("before must be an ISO-8601 timestamp") from exc
        if cutoff.tzinfo is None:
            raise ValueError("before must include a timezone")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT d.delegation_id FROM harness_delegations d"
                " LEFT JOIN harness_delegation_reconciliations r"
                " ON r.delegation_id=d.delegation_id"
                " WHERE d.updated_at<? AND (d.status IN ('succeeded','failed','cancelled')"
                " OR (d.status='unknown' AND r.delegation_id IS NOT NULL))"
                " ORDER BY d.updated_at,d.delegation_id LIMIT ?",
                (_stamp(cutoff), limit),
            ).fetchall()
            identifiers = [str(row["delegation_id"]) for row in rows]
            for delegation_id in identifiers:
                db.execute(
                    "DELETE FROM harness_delegation_reconciliations WHERE delegation_id=?",
                    (delegation_id,),
                )
                db.execute(
                    "DELETE FROM harness_delegations WHERE delegation_id=?",
                    (delegation_id,),
                )
            leases = db.execute(
                "DELETE FROM harness_execution_leases WHERE expires_at<?",
                (_stamp(cutoff),),
            ).rowcount
            db.commit()
        return {"delegations": len(identifiers), "leases": int(leases)}

    def _admit(self, db, row, *, actor: str) -> str | None:
        if not self.require_execution_lease:
            return None
        lease = db.execute(
            "SELECT * FROM harness_execution_leases WHERE actor=?", (actor,),
        ).fetchone()
        if lease is None:
            return "Harness execution lease is not configured"
        if lease["expires_at"] <= _stamp(_now()):
            return "Harness execution lease expired"
        request = json.loads(row["request_json"])
        config = request.get("config") or {}
        if config.get("provider") not in json.loads(lease["allowed_providers_json"]):
            return "Harness Provider is not allowed by the execution lease"
        if int(config.get("max_wall_seconds", 1800)) > int(lease["max_wall_seconds"]):
            return "delegation wall-clock budget exceeds the execution lease"
        if int(lease["used_delegations"]) >= int(lease["max_delegations"]):
            return "execution lease delegation budget is exhausted"
        return None

    def _expire(self, db, *, actor: str) -> None:
        stamp = _stamp(_now())
        db.execute(
            "UPDATE harness_delegations SET status='unknown',"
            "error='delegation lease expired; reconciliation required',updated_at=?"
            " WHERE actor=? AND status='leased' AND lease_expires_at<=?",
            (stamp, actor, stamp),
        )

    def claim(self, *, actor: str, worker_id: str, lease_seconds: int = 30):
        if not worker_id.strip() or not 5 <= lease_seconds <= 300:
            raise ValueError("worker_id and lease_seconds between 5 and 300 are required")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db, actor=actor)
            row = db.execute(
                "SELECT * FROM harness_delegations WHERE actor=? AND status='queued'"
                " ORDER BY created_at,delegation_id LIMIT 1", (actor,),
            ).fetchone()
            if row is None:
                db.commit(); return None
            refusal = self._admit(db, row, actor=actor)
            if refusal is not None:
                db.execute(
                    "UPDATE harness_delegations SET status='failed',error=?,updated_at=?"
                    " WHERE delegation_id=? AND status='queued'",
                    (refusal, _stamp(_now()), row["delegation_id"]),
                )
                db.commit(); return None
            expires = _stamp(_now() + timedelta(seconds=lease_seconds))
            db.execute(
                "UPDATE harness_delegations SET status='leased',lease_owner=?,"
                "lease_expires_at=?,updated_at=? WHERE delegation_id=? AND status='queued'",
                (worker_id, expires, _stamp(_now()), row["delegation_id"]),
            )
            if self.require_execution_lease:
                db.execute(
                    "UPDATE harness_execution_leases SET used_delegations=used_delegations+1,updated_at=?"
                    " WHERE actor=?", (_stamp(_now()), actor),
                )
            db.commit()
        return self.get(row["delegation_id"], actor=actor)

    def renew(self, delegation_id: str, *, actor: str, worker_id: str, lease_seconds: int = 30):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db, actor=actor)
            expires = _stamp(_now() + timedelta(seconds=lease_seconds))
            changed = db.execute(
                "UPDATE harness_delegations SET lease_expires_at=?,updated_at=?"
                " WHERE delegation_id=? AND actor=? AND status='leased' AND lease_owner=?",
                (expires, _stamp(_now()), delegation_id, actor, worker_id),
            ).rowcount
            db.commit()
        if changed != 1:
            raise ValueError("delegation lease is not held by this worker")
        return self.get(delegation_id, actor=actor)

    def complete(self, delegation_id: str, *, actor: str, worker_id: str, result=None, error=None):
        if (result is None) == (error is None):
            raise ValueError("exactly one of result or error is required")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db, actor=actor)
            status = "succeeded" if error is None else "failed"
            changed = db.execute(
                "UPDATE harness_delegations SET status=?,result_json=?,error=?,updated_at=?"
                " WHERE delegation_id=? AND actor=? AND status='leased' AND lease_owner=?",
                (status, None if result is None else canonical_json(result), error,
                 _stamp(_now()), delegation_id, actor, worker_id),
            ).rowcount
            db.commit()
        if changed != 1:
            raise ValueError("delegation is no longer completable by this worker")
        return self.get(delegation_id, actor=actor)

    def cancel(self, delegation_id: str) -> CancelAck:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status FROM harness_delegations WHERE delegation_id=?",
                (delegation_id,),
            ).fetchone()
            if row is None or row["status"] in {"succeeded", "failed", "cancelled", "unknown"}:
                db.commit(); return CancelAck(CancelDisposition.CONFIRMED_STOPPED)
            if row["status"] == "queued":
                db.execute(
                    "UPDATE harness_delegations SET status='cancelled',cancel_requested=1,updated_at=?"
                    " WHERE delegation_id=?", (_stamp(_now()), delegation_id),
                )
            else:
                db.execute(
                    "UPDATE harness_delegations SET cancel_requested=1,updated_at=?"
                    " WHERE delegation_id=?", (_stamp(_now()), delegation_id),
                )
            db.commit()
        return CancelAck(CancelDisposition.CONFIRMED_STOPPED if row["status"] == "queued" else CancelDisposition.UNKNOWN)

    def get(self, delegation_id: str, *, actor: str | None = None):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM harness_delegations WHERE delegation_id=?"
                + ("" if actor is None else " AND actor=?"),
                (delegation_id,) if actor is None else (delegation_id, actor),
            ).fetchone()
            if (
                row is not None and row["status"] == "leased"
                and row["lease_expires_at"] <= _stamp(_now())
            ):
                db.execute(
                    "UPDATE harness_delegations SET status='unknown',"
                    "error='delegation lease expired; reconciliation required',updated_at=?"
                    " WHERE delegation_id=? AND status='leased'",
                    (_stamp(_now()), delegation_id),
                )
                row = db.execute(
                    "SELECT * FROM harness_delegations WHERE delegation_id=?",
                    (delegation_id,),
                ).fetchone()
            db.commit()
        if row is None:
            raise LookupError("delegation not found")
        return self._dto(row)


class HarnessSubagentHandler:
    def __init__(self, queue: DelegationQueue, *, poll_seconds: float = 0.1) -> None:
        self.queue, self.poll_seconds = queue, poll_seconds

    def validate(self, manifest, config):
        issues = []
        if manifest.execution_safety is not ExecutionSafety.UNKNOWN_ON_LEASE_LOSS:
            issues.append(HandlerValidationIssue(("execution_safety",), "Harness delegation requires unknown_on_lease_loss"))
        if not isinstance(config.get("provider"), str) or not config.get("provider", "").strip():
            issues.append(HandlerValidationIssue(("provider",), "provider must be a non-empty string"))
        effects = config.get("effects", "read")
        isolation = config.get("isolation_mode", "shared")
        if effects == "write" and isolation not in {"exclusive", "worktree"}:
            issues.append(HandlerValidationIssue(
                ("isolation_mode",),
                "write delegations require exclusive or worktree isolation",
            ))
        if config.get("max_concurrency", 1) != 1:
            issues.append(HandlerValidationIssue(
                ("max_concurrency",),
                "the current Harness delegation worker enforces max_concurrency=1",
            ))
        return HandlerValidationResult(tuple(issues))

    def prepare(self, request, context):
        digest = hashlib.sha256(str(request.idempotency_key).encode()).hexdigest()
        delegation_id = f"harness_delegation:{digest}"
        return PreparedExecution({
            "delegation_id": delegation_id, "actor": str(request.actor),
            "input": request.input, "config": request.config,
        }, delegation_id)

    def execute(self, prepared, context):
        payload = prepared.payload
        item = self.queue.enqueue(
            payload["delegation_id"], actor=payload["actor"],
            request={"input": payload["input"], "config": payload["config"]},
        )
        deadline = time.monotonic() + int(payload["config"].get("max_wall_seconds", 1800))
        while item["status"] in {"queued", "leased"} and time.monotonic() < deadline:
            time.sleep(self.poll_seconds)
            item = self.queue.get(payload["delegation_id"], actor=payload["actor"])
        if item["status"] == "succeeded":
            output = {"result": item["result"]}
            return RawHandlerResult(output, None, payload["delegation_id"], ExternalEffect.KNOWN_APPLIED)
        if item["status"] == "failed":
            raise HandlerPermanentError(item["error"] or "Harness subagent failed")
        raise UnknownExternalResultError(
            item["error"] or "Harness delegation outcome is unknown",
            provider_request_id=payload["delegation_id"],
            details={"resolution": {"kind": "reconciliation_required"}},
        )

    def normalize_result(self, raw, context):
        return HandlerResult(
            HandlerResultStatus.SUCCEEDED, raw.output, None, raw.usage, True,
            raw.external_effect, raw.provider_request_id,
        )

    def cancel(self, execution_ref, context):
        return self.queue.cancel(execution_ref)

    def recover(self, recovery_ref, context):
        try: item = self.queue.get(recovery_ref)
        except LookupError: return RecoveryResult(RecoveryDisposition.NOT_FOUND)
        if item["status"] != "succeeded":
            return RecoveryResult(RecoveryDisposition.UNKNOWN, provider_request_id=recovery_ref)
        output = {"result": item["result"]}
        return RecoveryResult(RecoveryDisposition.FOUND, HandlerResult(
            HandlerResultStatus.SUCCEEDED, output, None, None, True,
            ExternalEffect.KNOWN_APPLIED, recovery_ref,
        ), recovery_ref)
