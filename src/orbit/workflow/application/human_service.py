"""Command-only HumanTask lifecycle, quorum and durable waiting semantics."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import secrets
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator

from ..domain.human import HumanTaskKind, HumanTaskStatus, QuorumKind, submission_token_hash
from ..domain.ids import EntityId
from ..domain.serialization import canonical_json, definition_hash
from ..persistence.control import append_control_event, audit
from ..persistence.database import connect_workflow_database


class HumanTaskService:
    def __init__(self, path: Path | str) -> None: self.path = Path(path)

    def create(
        self, run_id: EntityId, kind: HumanTaskKind, payload: Mapping[str, Any], *,
        actor: str, now: datetime, capability_scope: str | None = None,
        assignee: str | None = None, role: str | None = None,
        participants: Iterable[str] = (), quorum: QuorumKind = QuorumKind.ANY,
        quorum_count: int = 1, form_schema: Mapping[str, Any] | None = None,
        deadline_at: datetime | None = None, reminder_interval_seconds: int | None = None,
        escalation_policy: Mapping[str, Any] | None = None,
    ) -> tuple[EntityId, str]:
        values = tuple(sorted(set(participants)))
        if quorum is QuorumKind.ALL and values: quorum_count = len(values)
        if quorum_count < 1 or (values and quorum_count > len(values)): raise ValueError("invalid quorum")
        if form_schema is not None: Draft202012Validator.check_schema(form_schema)
        request_hash = definition_hash({"run": str(run_id), "kind": kind.value, "payload": payload, "scope": capability_scope, "participants": values, "quorum": quorum.value, "count": quorum_count})
        task_id = EntityId("human_task", request_hash.value.removeprefix("sha256:"))
        token = secrets.token_urlsafe(32); token_hash = submission_token_hash(token)
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT task_id FROM human_tasks WHERE run_id=? AND request_hash=?", (str(run_id), request_hash.value)).fetchone()
            if prior is not None:
                raise ValueError("HumanTask already exists; submission token is never reissued")
            append_control_event(db, run_id=run_id, aggregate_id=task_id, event_type="human_task_created", payload={"kind": kind.value, "request_hash": request_hash.value, "capability_scope": capability_scope}, actor=actor, idempotency_key=request_hash.value, occurred_at=now)
            db.execute(
                """INSERT INTO human_tasks(task_id,run_id,node_run_id,kind,status,request_hash,capability_scope,submission_token_hash,actor,payload_json,result_json,deadline_at,aggregate_version,created_at,updated_at,assignee,role,form_schema_json,quorum_kind,quorum_count,reminder_interval_seconds,escalation_policy_json,claimed_by,revision) VALUES (?,?,NULL,?,'waiting',?,?,?,?,?,NULL,?,1,?,?,?,?,?,?,?,?,?,NULL,1)""",
                (str(task_id), str(run_id), kind.value, request_hash.value, capability_scope, token_hash, actor, canonical_json(payload), None if deadline_at is None else deadline_at.isoformat(), now.isoformat(), now.isoformat(), assignee, role, None if form_schema is None else canonical_json(form_schema), quorum.value, quorum_count, reminder_interval_seconds, None if escalation_policy is None else canonical_json(escalation_policy)),
            )
            for participant in values:
                db.execute("INSERT INTO human_task_participants(task_id,actor,revision) VALUES (?,?,1)", (str(task_id), participant))
            db.execute("UPDATE workflow_runs SET status='waiting', aggregate_version=aggregate_version+1, updated_at=? WHERE run_id=? AND status='running'", (now.isoformat(), str(run_id)))
            audit(db, run_id=run_id, actor=actor, action="human.create", target_id=str(task_id), decision="allowed", details={"kind": kind.value}, occurred_at=now)
            db.commit(); return task_id, token

    def create_node_approval(
        self, run_id: EntityId, node_run_id: EntityId, capability: str,
        plan_version: int, *, actor: str, now: datetime,
        deadline_at: datetime | None = None,
    ) -> tuple[EntityId, str]:
        if node_run_id.kind != "node_run" or not capability.strip():
            raise ValueError("node approval scope is invalid")
        return self.create(
            run_id,
            HumanTaskKind.APPROVAL,
            {
                "node_run_id": str(node_run_id),
                "capability": capability,
                "plan_version": plan_version,
            },
            actor=actor,
            now=now,
            capability_scope=capability,
            deadline_at=deadline_at,
        )

    def claim(self, task_id: EntityId, *, actor: str, expected_version: int, now: datetime) -> None:
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get(db, task_id); self._authorize(row, actor, allow_role=True)
            if row["status"] not in {"waiting", "claimed"}: raise ValueError("HumanTask cannot be claimed")
            if row["aggregate_version"] != expected_version: raise ValueError("HumanTask version conflict")
            append_control_event(db, run_id=EntityId.parse(row["run_id"]), aggregate_id=task_id, event_type="human_task_claimed", payload={"actor": actor}, actor=actor, idempotency_key=f"claim:{expected_version}:{actor}", occurred_at=now)
            db.execute("UPDATE human_tasks SET status='claimed',claimed_by=?,aggregate_version=aggregate_version+1,updated_at=? WHERE task_id=? AND aggregate_version=?", (actor, now.isoformat(), str(task_id), expected_version)); db.commit()

    def submit(self, task_id: EntityId, token: str, decision: str, value: Any, *, actor: str, expected_version: int, now: datetime) -> HumanTaskStatus:
        if decision not in {"approve", "reject", "provide_input", "withdraw"}: raise ValueError("invalid HumanTask decision")
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE"); row = self._get(db, task_id)
            if row["aggregate_version"] != expected_version: raise ValueError("HumanTask version conflict")
            if row["status"] not in {"waiting", "claimed"}: raise ValueError("HumanTask is terminal")
            if submission_token_hash(token) != row["submission_token_hash"]: raise PermissionError("invalid submission token")
            self._authorize(row, actor, allow_role=True)
            if row["form_schema_json"] and decision == "provide_input": Draft202012Validator(json.loads(row["form_schema_json"])).validate(value)
            participant = db.execute("SELECT * FROM human_task_participants WHERE task_id=? AND actor=?", (str(task_id), actor)).fetchone()
            if participant is not None:
                if participant["decision"] is not None and decision != "withdraw": raise ValueError("participant already submitted")
                db.execute("UPDATE human_task_participants SET decision=?,value_json=?,submitted_at=?,revision=revision+1 WHERE task_id=? AND actor=?", (decision, canonical_json(value), now.isoformat(), str(task_id), actor))
            completed, rejected = self._quorum(db, row, decision, participant is None)
            status = HumanTaskStatus.REJECTED if rejected else HumanTaskStatus.COMPLETED if completed else HumanTaskStatus.WAITING
            append_control_event(db, run_id=EntityId.parse(row["run_id"]), aggregate_id=task_id, event_type="human_task_submitted", payload={"actor": actor, "decision": decision, "status": status.value, "value": value}, actor=actor, idempotency_key=f"submit:{expected_version}:{actor}:{decision}", occurred_at=now)
            next_token_hash = "used" if status in {HumanTaskStatus.COMPLETED, HumanTaskStatus.REJECTED} else row["submission_token_hash"]
            db.execute("UPDATE human_tasks SET status=?,actor=?,result_json=?,submission_token_hash=?,aggregate_version=aggregate_version+1,updated_at=? WHERE task_id=? AND aggregate_version=?", (status.value, actor, canonical_json({"decision": decision, "value": value}), next_token_hash, now.isoformat(), str(task_id), expected_version))
            if status in {HumanTaskStatus.COMPLETED, HumanTaskStatus.REJECTED}:
                waiting = db.execute("SELECT 1 FROM human_tasks WHERE run_id=? AND task_id<>? AND status IN ('waiting','claimed')", (row["run_id"], str(task_id))).fetchone()
                if waiting is None: db.execute("UPDATE workflow_runs SET status='running',aggregate_version=aggregate_version+1,updated_at=? WHERE run_id=? AND status='waiting'", (now.isoformat(), row["run_id"]))
            audit(db, run_id=EntityId.parse(row["run_id"]), actor=actor, action="human.submit", target_id=str(task_id), decision=decision, details={"status": status.value}, occurred_at=now)
            db.commit(); return status

    def expire_due(self, now: datetime, *, limit: int = 100) -> tuple[EntityId, ...]:
        with connect_workflow_database(self.path) as db:
            rows = db.execute("SELECT * FROM human_tasks WHERE status IN ('waiting','claimed') AND deadline_at IS NOT NULL AND deadline_at<=? ORDER BY deadline_at,task_id LIMIT ?", (now.isoformat(), limit)).fetchall()
            ids = []
            for row in rows:
                task_id = EntityId.parse(row["task_id"]); db.execute("BEGIN IMMEDIATE") if not db.in_transaction else None
                append_control_event(db, run_id=EntityId.parse(row["run_id"]), aggregate_id=task_id, event_type="human_task_escalated", payload={"reason": "deadline", "policy": json.loads(row["escalation_policy_json"] or "{}")}, actor="system:recovery", idempotency_key="deadline", occurred_at=now)
                db.execute("UPDATE human_tasks SET status='expired',aggregate_version=aggregate_version+1,updated_at=? WHERE task_id=? AND status IN ('waiting','claimed')", (now.isoformat(), str(task_id))); ids.append(task_id)
            db.commit(); return tuple(ids)

    def expire(
        self, task_id: EntityId, *, expected_version: int, actor: str,
        now: datetime,
    ) -> bool:
        """Expire exactly one task through its Event/Expected Version boundary."""
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get(db, task_id)
            if row["status"] == "expired":
                return False
            if row["status"] not in {"waiting", "claimed"}:
                raise ValueError("HumanTask cannot expire from its current state")
            if row["aggregate_version"] != expected_version:
                raise ValueError("HumanTask version conflict")
            if row["deadline_at"] is None or datetime.fromisoformat(row["deadline_at"]) > now:
                raise ValueError("HumanTask deadline has not elapsed")
            append_control_event(
                db,
                run_id=EntityId.parse(row["run_id"]),
                aggregate_id=task_id,
                event_type="human_task_escalated",
                payload={"reason": "deadline", "actor": actor},
                actor=actor,
                idempotency_key=f"expire:{expected_version}",
                occurred_at=now,
            )
            updated = db.execute(
                """UPDATE human_tasks
                   SET status='expired', aggregate_version=aggregate_version+1,
                       updated_at=?
                   WHERE task_id=? AND aggregate_version=?
                     AND status IN ('waiting','claimed')""",
                (now.isoformat(), str(task_id), expected_version),
            )
            if updated.rowcount != 1:
                raise ValueError("HumanTask expire conflict")
            audit(
                db,
                run_id=EntityId.parse(row["run_id"]),
                actor=actor,
                action="human.expire",
                target_id=str(task_id),
                decision="allowed",
                details={"expected_version": expected_version},
                occurred_at=now,
            )
            db.commit()
            return True

    @staticmethod
    def _get(db, task_id):
        row = db.execute("SELECT * FROM human_tasks WHERE task_id=?", (str(task_id),)).fetchone()
        if row is None: raise ValueError("HumanTask not found")
        return row
    @staticmethod
    def _authorize(row, actor, *, allow_role):
        if row["assignee"] and actor != row["assignee"] and actor != row["claimed_by"]: raise PermissionError("actor is not assigned")
    @staticmethod
    def _quorum(db, row, decision, direct):
        if direct: return decision in {"approve", "provide_input"}, decision == "reject"
        votes = [item[0] for item in db.execute("SELECT decision FROM human_task_participants WHERE task_id=? AND decision IS NOT NULL AND decision!='withdraw'", (row["task_id"],)).fetchall()]
        if "reject" in votes: return False, True
        approvals = sum(value in {"approve", "provide_input"} for value in votes)
        return approvals >= row["quorum_count"], False
