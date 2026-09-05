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

# Host-neutral delegation to the Agent App that initiated the Run. Unlike the
# Harness-specific handler it does not select a provider: the current App is
# already the provider boundary. The queue remains actor-scoped, so another
# conversation cannot pick up this conversation's work.
APP_DELEGATE_MANIFEST = HandlerManifest(
    "app.delegate", "1.0.0", ("action",),
    {"task": "schema://object/1.0"}, {"result": "schema://object/1.0"},
    {
        "type": "object",
        "properties": {
            "target": {
                "type": "string", "enum": ["run_initiator", "background_pool"],
            },
            "pool": {"type": "string", "minLength": 1},
            "max_wall_seconds": {"type": "integer", "minimum": 1, "maximum": 7200},
            "effects": {"type": "string", "enum": ["read", "write"]},
            "isolation_mode": {
                "type": "string",
                "enum": ["shared", "exclusive", "worktree", "snapshot"],
            },
            "max_concurrency": {"type": "integer", "minimum": 1, "maximum": 1},
        },
        "required": ["target"], "additionalProperties": False,
    },
    ExecutionSafety.UNKNOWN_ON_LEASE_LOSS,
    ResourceProfile(0, 0, 0, 7200, 0, "app-delegate"),
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
                "checkpoint_json TEXT,checkpoint_revision INTEGER NOT NULL DEFAULT 0,"
                "created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
            )
            columns = {
                str(row[1]) for row in db.execute(
                    "PRAGMA table_info(harness_delegations)"
                ).fetchall()
            }
            if "checkpoint_json" not in columns:
                db.execute(
                    "ALTER TABLE harness_delegations ADD COLUMN checkpoint_json TEXT"
                )
            if "checkpoint_revision" not in columns:
                db.execute(
                    "ALTER TABLE harness_delegations ADD COLUMN "
                    "checkpoint_revision INTEGER NOT NULL DEFAULT 0"
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
            "delegation_id": row["delegation_id"], "actor": row["actor"],
            "status": row["status"],
            "request": json.loads(row["request_json"]),
            "result": None if row["result_json"] is None else json.loads(row["result_json"]),
            "error": row["error"], "lease_expires_at": row["lease_expires_at"],
            "worker_id": row["lease_owner"],
            "checkpoint": (
                None if row["checkpoint_json"] is None
                else json.loads(row["checkpoint_json"])
            ),
            "checkpoint_revision": int(row["checkpoint_revision"]),
            "cancel_requested": bool(row["cancel_requested"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
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
        idempotency_key: str, result=None, error: str | None = None,
    ) -> Mapping[str, Any]:
        if outcome not in {"confirmed_succeeded", "confirmed_failed"}:
            raise ValueError("outcome must be confirmed_succeeded or confirmed_failed")
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        if len(note) > 4000:
            raise ValueError("reconciliation note is too long")
        if result is not None and not isinstance(result, Mapping):
            raise ValueError("reconciled delegation result must be an object")
        if outcome == "confirmed_succeeded" and error is not None:
            raise ValueError("a successful reconciliation cannot include error")
        if outcome == "confirmed_failed" and result is not None:
            raise ValueError("a failed reconciliation cannot include result")
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
                "SELECT * FROM harness_delegations"
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
            if outcome == "confirmed_succeeded" and result is not None:
                db.execute(
                    "UPDATE harness_delegations SET status='succeeded',result_json=?,"
                    "error=NULL,updated_at=? WHERE delegation_id=? AND actor=?",
                    (canonical_json(result), created_at, delegation_id, actor),
                )
            elif outcome == "confirmed_failed":
                db.execute(
                    "UPDATE harness_delegations SET status='failed',error=?,updated_at=?"
                    " WHERE delegation_id=? AND actor=?",
                    (error or note or "delegation was confirmed failed", created_at,
                     delegation_id, actor),
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

    def list(
        self, *, actor: str, statuses: tuple[str, ...] = (), limit: int = 100,
    ) -> tuple[Mapping[str, Any], ...]:
        allowed = {"queued", "leased", "succeeded", "failed", "cancelled", "unknown"}
        selected = tuple(dict.fromkeys(statuses or ("queued", "leased", "unknown")))
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if not selected or any(status not in allowed for status in selected):
            raise ValueError("statuses contains an unsupported delegation status")
        placeholders = ",".join("?" for _ in selected)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db, actor=actor)
            rows = db.execute(
                "SELECT * FROM harness_delegations WHERE actor=?"
                f" AND status IN ({placeholders})"
                " ORDER BY created_at,delegation_id",
                (actor, *selected),
            ).fetchall()
            db.commit()
        visible = []
        for row in rows:
            config = json.loads(row["request_json"]).get("config") or {}
            # Healthy background work belongs to the daemon. An unknown item
            # still belongs in the actor's recovery view because only a human
            # verdict may reconcile an ambiguous external effect.
            if (config.get("target") == "background_pool"
                    and row["status"] != "unknown"):
                continue
            visible.append(self._dto(row))
            if len(visible) == limit:
                break
        return tuple(visible)

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
            rows = db.execute(
                "SELECT * FROM harness_delegations WHERE actor=? AND status='queued'"
                " ORDER BY created_at,delegation_id", (actor,),
            ).fetchall()
            # Conversation workers must never race the machine worker for a
            # background delegation. Old Harness rows have no target and keep
            # their original actor-scoped behaviour.
            row = next((candidate for candidate in rows if
                        (json.loads(candidate["request_json"]).get("config") or {})
                        .get("target") != "background_pool"), None)
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

    def claim_background(
        self, *, worker_id: str, pools: tuple[str, ...] = ("default",),
        lease_seconds: int = 30,
    ):
        """Lease one background-pool item across actors.

        Actor identity remains attached to the row and is returned to the
        worker; crossing actors here is intentional and confined to the
        private loopback worker endpoint, never the public MCP tools.
        """
        if not worker_id.strip() or not 5 <= lease_seconds <= 300:
            raise ValueError("worker_id and lease_seconds between 5 and 300 are required")
        selected = tuple(dict.fromkeys(str(pool).strip() for pool in pools if str(pool).strip()))
        if not selected:
            raise ValueError("at least one background pool is required")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM harness_delegations WHERE status='queued'"
                " ORDER BY created_at,delegation_id"
            ).fetchall()
            row = None
            for candidate in rows:
                request = json.loads(candidate["request_json"])
                config = request.get("config") or {}
                if (config.get("target") == "background_pool"
                        and str(config.get("pool", "default")) in selected):
                    row = candidate
                    break
            if row is None:
                db.commit()
                return None
            expires = _stamp(_now() + timedelta(seconds=lease_seconds))
            changed = db.execute(
                "UPDATE harness_delegations SET status='leased',lease_owner=?,"
                "lease_expires_at=?,updated_at=? WHERE delegation_id=? AND status='queued'",
                (worker_id, expires, _stamp(_now()), row["delegation_id"]),
            ).rowcount
            db.commit()
        return None if changed != 1 else self.get(row["delegation_id"])

    def renew_background(
        self, delegation_id: str, *, worker_id: str, lease_seconds: int = 30,
    ):
        item = self.get(delegation_id)
        return self.renew(
            delegation_id, actor=str(item["actor"]), worker_id=worker_id,
            lease_seconds=lease_seconds,
        )

    def complete_background(
        self, delegation_id: str, *, worker_id: str, result=None, error=None,
    ):
        item = self.get(delegation_id)
        return self.complete(
            delegation_id, actor=str(item["actor"]), worker_id=worker_id,
            result=result, error=error,
        )

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

    def checkpoint(
        self, delegation_id: str, *, actor: str, worker_id: str,
        checkpoint: Mapping[str, Any], expected_revision: int,
        lease_seconds: int = 30,
    ):
        """Persist an Agent-owned resume point while its lease is unambiguous."""

        if not isinstance(checkpoint, Mapping):
            raise ValueError("delegation checkpoint must be an object")
        if expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        if not 5 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be between 5 and 300")
        encoded = canonical_json(checkpoint)
        if len(encoded.encode("utf-8")) > 256 * 1024:
            raise ValueError("delegation checkpoint exceeds 262144 bytes")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db, actor=actor)
            row = db.execute(
                "SELECT status,lease_owner,checkpoint_json,checkpoint_revision "
                "FROM harness_delegations WHERE delegation_id=? AND actor=?",
                (delegation_id, actor),
            ).fetchone()
            if row is None:
                db.rollback()
                raise LookupError("delegation not found")
            expires = _stamp(_now() + timedelta(seconds=lease_seconds))
            # A retry whose first response was lost is harmless and stable.
            if (
                int(row["checkpoint_revision"]) == expected_revision + 1
                and row["checkpoint_json"] == encoded
                and row["status"] == "leased"
                and row["lease_owner"] == worker_id
            ):
                db.execute(
                    "UPDATE harness_delegations SET lease_expires_at=?,updated_at=?"
                    " WHERE delegation_id=? AND actor=? AND status='leased'"
                    " AND lease_owner=?",
                    (expires, _stamp(_now()), delegation_id, actor, worker_id),
                )
                db.commit()
                return self.get(delegation_id, actor=actor)
            changed = db.execute(
                "UPDATE harness_delegations SET checkpoint_json=?,"
                "checkpoint_revision=checkpoint_revision+1,lease_expires_at=?,"
                "updated_at=? WHERE delegation_id=? AND actor=? AND status='leased'"
                " AND lease_owner=? AND checkpoint_revision=?",
                (encoded, expires, _stamp(_now()), delegation_id, actor,
                 worker_id, expected_revision),
            ).rowcount
            db.commit()
        if changed != 1:
            raise ValueError(
                "delegation checkpoint revision or worker lease no longer matches"
            )
        return self.get(delegation_id, actor=actor)

    def complete(self, delegation_id: str, *, actor: str, worker_id: str, result=None, error=None):
        if (result is None) == (error is None):
            raise ValueError("exactly one of result or error is required")
        if result is not None and not isinstance(result, Mapping):
            raise ValueError("delegation result must be an object")
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

    def timeout(self, delegation_id: str, *, actor: str):
        """Close an unclaimed request, or make a leased outcome unknown."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status FROM harness_delegations WHERE delegation_id=? AND actor=?",
                (delegation_id, actor),
            ).fetchone()
            if row is not None and row["status"] == "queued":
                db.execute(
                    "UPDATE harness_delegations SET status='failed',error=?,updated_at=?"
                    " WHERE delegation_id=? AND actor=? AND status='queued'",
                    ("delegation was not claimed before its deadline", _stamp(_now()),
                     delegation_id, actor),
                )
            elif row is not None and row["status"] == "leased":
                db.execute(
                    "UPDATE harness_delegations SET status='unknown',error=?,"
                    "cancel_requested=1,updated_at=? WHERE delegation_id=? AND actor=?"
                    " AND status='leased'",
                    ("delegation exceeded its deadline; reconciliation required",
                     _stamp(_now()), delegation_id, actor),
                )
            db.commit()
        return self.get(delegation_id, actor=actor)

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
    def __init__(
        self, queue: DelegationQueue, *, poll_seconds: float = 0.1,
        project_workspace=None, project_root: Path | str | None = None,
    ) -> None:
        self.queue, self.poll_seconds = queue, poll_seconds
        self.project_workspace = project_workspace
        self.project_root = (
            None if project_root is None else Path(project_root).resolve()
        )

    def configure_workspace(self, *, project_workspace=None, project_root=None) -> None:
        self.project_workspace = project_workspace
        self.project_root = (
            None if project_root is None else Path(project_root).resolve()
        )

    def _workspace_descriptor(self, request):
        access = getattr(request, "workspace_access", None)
        if access is None:
            return None
        run_id = str(getattr(request, "run_id", "") or "shared")
        if access.get("isolation") == "none":
            if self.project_root is None:
                raise HandlerPermanentError("direct project workspace is unavailable")
            path, kind = self.project_root, "direct"
        else:
            if self.project_workspace is None:
                raise HandlerPermanentError("git worktree workspace is unavailable")
            path = self.project_workspace.acquire(
                run_id, files=access.get("files"),
            )
            kind = "git_worktree"
        provider = getattr(self.project_workspace, "provider", None)
        project_root = (
            self.project_root
            or getattr(provider, "project_root", None)
            or path
        )
        return {
            "kind": kind, "path": str(path),
            "project_root": str(project_root),
            "access": "read_write", "run_id": run_id,
        }

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
            "workspace": self._workspace_descriptor(request),
        }, delegation_id)

    def execute(self, prepared, context):
        payload = prepared.payload
        request = {"input": payload["input"], "config": payload["config"]}
        for key in ("protocol", "execution", "workspace"):
            if key in payload:
                request[key] = payload[key]
        item = self.queue.enqueue(
            payload["delegation_id"], actor=payload["actor"],
            request=request,
        )
        deadline = time.monotonic() + int(payload["config"].get("max_wall_seconds", 1800))
        while item["status"] in {"queued", "leased"} and time.monotonic() < deadline:
            time.sleep(self.poll_seconds)
            item = self.queue.get(payload["delegation_id"], actor=payload["actor"])
        if item["status"] in {"queued", "leased"}:
            item = self.queue.timeout(
                payload["delegation_id"], actor=payload["actor"],
            )
        if item["status"] == "succeeded":
            output = {"result": item["result"]}
            return RawHandlerResult(output, None, payload["delegation_id"], ExternalEffect.KNOWN_APPLIED)
        if item["status"] == "failed":
            raise HandlerPermanentError(item["error"] or "delegated Agent failed")
        raise UnknownExternalResultError(
            item["error"] or "Agent delegation outcome is unknown",
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


class AppDelegationHandler(HarnessSubagentHandler):
    """Delegate one Agent step to the App conversation that owns the Run."""

    def validate(self, manifest, config):
        issues = []
        if manifest.execution_safety is not ExecutionSafety.UNKNOWN_ON_LEASE_LOSS:
            issues.append(HandlerValidationIssue(
                ("execution_safety",), "App delegation requires unknown_on_lease_loss",
            ))
        # The background implementation is intentionally parked. Keep its
        # queue/worker code for a later opt-in release, but do not let a
        # published Workflow depend on a daemon that an App-only installation
        # does not have.
        if config.get("target") != "run_initiator":
            issues.append(HandlerValidationIssue(
                ("target",), "target must be run_initiator",
            ))
        if config.get("target") == "run_initiator" and "pool" in config:
            issues.append(HandlerValidationIssue(
                ("pool",), "pool is only valid for background_pool",
            ))
        effects = config.get("effects", "read")
        isolation = config.get("isolation_mode", "shared")
        if effects == "write" and isolation not in {"exclusive", "worktree"}:
            issues.append(HandlerValidationIssue(
                ("isolation_mode",),
                "write delegations require exclusive or worktree isolation",
            ))
        if config.get("max_concurrency", 1) != 1:
            issues.append(HandlerValidationIssue(
                ("max_concurrency",), "App delegation enforces max_concurrency=1",
            ))
        return HandlerValidationResult(tuple(issues))

    def prepare(self, request, context):
        if request.config.get("target") != "run_initiator":
            raise HandlerPermanentError(
                "app.delegate target must be run_initiator"
            )
        digest = hashlib.sha256(str(request.idempotency_key).encode()).hexdigest()
        delegation_id = f"app_delegation:{digest}"
        inputs = request.input
        source = request.config.get("_agent_step")
        if source is not None:
            # Run-local adaptation retains the published graph's ports. Only
            # the message delivered to the App uses the task envelope.
            inputs = {"task": {
                "input": inputs,
                "instructions": source["config"].get("prompt", ""),
                "original_handler": source["handler"],
                "original_config": source["config"],
            }}
        return PreparedExecution({
            "delegation_id": delegation_id, "actor": str(request.actor),
            "input": inputs, "config": request.config,
            "workspace": self._workspace_descriptor(request),
            "protocol": {"name": "orbit-app-delegation", "version": "1"},
            "execution": {
                "attempt_id": str(request.attempt_id),
                "idempotency_key": str(request.idempotency_key),
                "deadline": request.deadline.isoformat(),
                "node_id": getattr(request, "node_id", ""),
                "run_id": getattr(request, "run_id", ""),
                "output_ports": getattr(request, "output_ports", ()),
                "acceptance": getattr(request, "acceptance", None),
            },
        }, delegation_id)

    def execute(self, prepared, context):
        # Reuse the proven durable queue and polling semantics. A synthetic
        # provider marker makes diagnostics explicit without exposing a model
        # choice in the Workflow definition.
        payload = prepared.payload
        prepared = PreparedExecution({
            **payload, "config": {**payload["config"], "provider": "current-app"},
        }, prepared.execution_ref)
        return super().execute(prepared, context)
