"""Atomic Policy -> PlanPatch CAS -> immutable ExecutionPlanVersion commit."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Iterable, Mapping

from ..domain.execution_plan import GraphExecutionPlan, execution_plan_from_primitive
from ..domain.envelopes import CommandEnvelope
from ..domain.ids import EntityId
from ..domain.plan_patch import AgenticRegion, DynamicDagLimits, PatchOperationKind, PlanPatch
from ..domain.policy import PolicyDecision, PolicyRule
from ..domain.serialization import canonical_json, definition_hash, to_primitive
from ..domain.states import NodeRunStatus
from ..domain.versions import AggregateVersion
from ..planner.plan_compiler import compile_patch, validate_patch
from ..policy.validator import PolicyValidator
from ..persistence.control import append_control_event, audit
from ..persistence.database import connect_workflow_database


class PlanConflictError(RuntimeError): pass
class PolicyRejectedError(PermissionError): pass


class PlanService:
    def __init__(self, path: Path | str, *, rules: Iterable[PolicyRule], limits: DynamicDagLimits = DynamicDagLimits(), runtime_service=None) -> None:
        self.path = Path(path); self.policy = PolicyValidator(rules); self.limits = limits; self.runtime_service=runtime_service

    @staticmethod
    def required_capabilities(patch: PlanPatch) -> tuple[str, ...]:
        values: set[str] = set()
        for operation in patch.operations:
            if operation.kind in {PatchOperationKind.ADD_NODE, PatchOperationKind.REPLACE_PENDING_NODE} and operation.value:
                config = operation.value.get("config", {})
                values.update(config.get("capabilities", ()))
        return tuple(sorted(values))

    def validate(self, patch: PlanPatch, region: AgenticRegion, *, approvals: Iterable[str] = ()) -> tuple[GraphExecutionPlan, PolicyDecision]:
        with connect_workflow_database(self.path, read_only=True) as db:
            base = self._load_plan(db, patch.run_id, patch.base_plan_version.value)
            statuses = {row["node_id"]: NodeRunStatus(row["status"]) for row in db.execute("SELECT node_id,status FROM node_runs WHERE run_id=?", (str(patch.run_id),))}
            validate_patch(base, patch, region, statuses, self.limits)
            decision = self.policy.validate(run_id=patch.run_id, patch_id=patch.patch_id, capabilities=self.required_capabilities(patch), approvals=approvals, context={"base_plan_hash": definition_hash(base).value})
            return compile_patch(base, patch, region, statuses, self.limits), decision

    def commit(self, patch: PlanPatch, region: AgenticRegion, *, actor: str, now: datetime) -> GraphExecutionPlan:
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM plan_patches WHERE patch_id=?", (str(patch.patch_id),)).fetchone()
            if existing is not None:
                if existing["content_hash"] != patch.content_hash.value: raise ValueError("PlanPatch identity conflict")
                if existing["status"] == "committed": return self._load_plan(db, patch.run_id, existing["result_plan_version"])
            proposal = db.execute("SELECT * FROM planner_proposals WHERE proposal_id=?", (str(patch.proposal_id),)).fetchone()
            if proposal is None or proposal["status"] not in {"protocol_accepted", "consumed"}: raise ValueError("Patch requires protocol-accepted Proposal")
            latest = db.execute("SELECT MAX(plan_version) FROM execution_plans WHERE run_id=?", (str(patch.run_id),)).fetchone()[0]
            if latest != patch.base_plan_version.value: raise PlanConflictError(f"base PlanVersion {patch.base_plan_version.value} is stale; latest={latest}")
            base = self._load_plan(db, patch.run_id, latest)
            statuses = {row["node_id"]: NodeRunStatus(row["status"]) for row in db.execute("SELECT node_id,status FROM node_runs WHERE run_id=?", (str(patch.run_id),))}
            approvals = tuple(row[0] for row in db.execute("SELECT capability_scope FROM human_tasks WHERE run_id=? AND kind='approval' AND status='completed' AND capability_scope IS NOT NULL", (str(patch.run_id),)))
            plan = compile_patch(base, patch, region, statuses, self.limits)
            decision = self.policy.validate(run_id=patch.run_id, patch_id=patch.patch_id, capabilities=self.required_capabilities(patch), approvals=approvals, context={"base_plan_hash": definition_hash(base).value})
            if existing is None:
                db.execute("INSERT INTO plan_patches VALUES (?,?,?,?,NULL,'validated',?,?,?,?,?,?)", (str(patch.patch_id),str(patch.proposal_id),str(patch.run_id),patch.base_plan_version.value,patch.reason,canonical_json(to_primitive(patch)),patch.content_hash.value,0,now.isoformat(),now.isoformat()))
            db.execute("INSERT OR IGNORE INTO policy_decisions VALUES (?,?,?,?,?,?,?,?,?,?)", (str(decision.decision_id),str(patch.run_id),str(patch.patch_id),decision.input_hash.value,decision.rule_set_version,int(decision.allowed),int(decision.requires_approval),canonical_json(decision.results),canonical_json(decision.reasons),now.isoformat()))
            if not decision.allowed:
                db.execute("UPDATE plan_patches SET status='rejected',aggregate_version=aggregate_version+1,updated_at=? WHERE patch_id=?", (now.isoformat(),str(patch.patch_id)))
                append_control_event(db,run_id=patch.run_id,aggregate_id=patch.patch_id,event_type="plan_patch_rejected",payload={"reasons":decision.reasons,"requires_approval":decision.requires_approval},actor=actor,idempotency_key=patch.content_hash.value,occurred_at=now)
                audit(db,run_id=patch.run_id,actor=actor,action="plan.commit",target_id=str(patch.patch_id),decision="denied",details={"reasons":decision.reasons},occurred_at=now); db.commit()
                raise PolicyRejectedError("; ".join(decision.reasons))
            event = append_control_event(db,run_id=patch.run_id,aggregate_id=patch.patch_id,event_type="plan_patch_committed",payload={"proposal_id":str(patch.proposal_id),"base_plan_version":latest,"result_plan_version":plan.plan_version.value,"plan_hash":definition_hash(plan).value},actor=actor,idempotency_key=patch.content_hash.value,occurred_at=now)
            db.execute("""INSERT INTO execution_plans(plan_id,run_id,plan_version,workflow_id,workflow_version,plan_schema_version,canonical_plan_json,definition_hash,created_event_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",(str(plan.plan_id),str(plan.run_id),plan.plan_version.value,str(plan.workflow_id),plan.workflow_version.value,plan.schema_version.value,canonical_json(plan),definition_hash(plan).value,str(event.event_id),now.isoformat()))
            db.execute("UPDATE plan_patches SET status='committed',result_plan_version=?,aggregate_version=aggregate_version+1,updated_at=? WHERE patch_id=?",(plan.plan_version.value,now.isoformat(),str(patch.patch_id)))
            db.execute("UPDATE planner_proposals SET status='consumed' WHERE proposal_id=? AND status='protocol_accepted'",(str(patch.proposal_id),))
            audit(db,run_id=patch.run_id,actor=actor,action="plan.commit",target_id=str(patch.patch_id),decision="allowed",details={"plan_version":plan.plan_version.value},occurred_at=now)
            db.commit()
        if self.runtime_service is not None:
            self.activate(plan.run_id, plan.plan_version.value, actor=actor, now=now)
        return plan

    def activate(self, run_id: EntityId, plan_version: int, *, actor: str, now: datetime):
        """Submit the normal Kernel graph command; recovery may safely repeat it."""
        if self.runtime_service is None: raise RuntimeError("Runtime service is not configured")
        with connect_workflow_database(self.path, read_only=True) as db:
            row=db.execute("SELECT aggregate_version FROM workflow_runs WHERE run_id=?",(str(run_id),)).fetchone()
            if row is None:raise ValueError("Run not found")
            version=AggregateVersion(row[0])
        return self.runtime_service.submit(CommandEnvelope.create(command_type="advance_graph",aggregate_id=run_id,correlation_id=run_id,expected_version=version,idempotency_key=f"activate-plan:{plan_version}:{version.value}",actor=actor,payload={"plan_version":plan_version},issued_at=now))

    @staticmethod
    def _load_plan(db, run_id: EntityId, version: int) -> GraphExecutionPlan:
        row=db.execute("SELECT canonical_plan_json FROM execution_plans WHERE run_id=? AND plan_version=?",(str(run_id),version)).fetchone()
        if row is None: raise ValueError("ExecutionPlanVersion not found")
        plan=execution_plan_from_primitive(json.loads(row[0]))
        if not isinstance(plan,GraphExecutionPlan): raise ValueError("dynamic patch requires GraphExecutionPlan 1.2")
        return plan


def validate_completion(db, run_id: EntityId) -> tuple[bool, tuple[str, ...]]:
    """Planner finish is accepted only when deterministic responsibilities are empty."""
    reasons=[]
    for table,statuses,label in (
        ("node_runs",("pending","ready","running","waiting"),"active nodes"),
        ("jobs",("ready","leased","running","retry_wait"),"active jobs"),
        ("durable_timers",("scheduled","leased"),"active timers"),
        ("human_tasks",("waiting","claimed"),"human responsibility"),
    ):
        placeholders=",".join("?" for _ in statuses); row=db.execute(f"SELECT 1 FROM {table} WHERE run_id=? AND status IN ({placeholders}) LIMIT 1",(str(run_id),*statuses)).fetchone()
        if row is not None: reasons.append(label)
    return not reasons,tuple(reasons)
