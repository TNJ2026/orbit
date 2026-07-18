"""Fixed-version Subflow links with explicit propagation and artifact scope."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..domain.ids import EntityId
from ..domain.serialization import canonical_json, definition_hash
from ..domain.subflow import MAX_SUBFLOW_DEPTH, PropagationPolicy, SubflowStatus
from ..domain.versions import Revision
from ..persistence.control import append_control_event, audit
from ..persistence.database import connect_workflow_database


class SubflowService:
    def __init__(self,path:Path|str)->None:self.path=Path(path)

    def link(self,parent_run_id:EntityId,child_run_id:EntityId,*,workflow_id:EntityId,workflow_version:Revision,input_mapping,output_mapping,artifact_scope=(),propagation:PropagationPolicy=PropagationPolicy(),recursion_depth:int=1,actor:str,now:datetime)->EntityId:
        if not 1<=recursion_depth<=MAX_SUBFLOW_DEPTH:raise ValueError("Subflow recursion limit exceeded")
        digest=definition_hash({"parent":str(parent_run_id),"child":str(child_run_id),"workflow":str(workflow_id),"version":workflow_version.value});link_id=EntityId("subflow_link",digest.value.removeprefix("sha256:"))
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            child=db.execute("SELECT workflow_id,workflow_version,correlation_id FROM workflow_runs WHERE run_id=?",(str(child_run_id),)).fetchone()
            if child is None or child["workflow_id"]!=str(workflow_id) or child["workflow_version"]!=workflow_version.value:raise ValueError("child Run is not bound to requested WorkflowVersion")
            for artifact_id in artifact_scope:
                row=db.execute("SELECT run_id FROM artifacts WHERE artifact_id=?",(str(artifact_id),)).fetchone()
                if row is None or row[0]!=str(parent_run_id):raise PermissionError("artifact is outside parent Run scope")
            prior=db.execute("SELECT link_id FROM subflow_links WHERE child_run_id=?",(str(child_run_id),)).fetchone()
            if prior is not None:return EntityId.parse(prior[0])
            append_control_event(db,run_id=parent_run_id,aggregate_id=link_id,event_type="subflow_link_created",payload={"child_run_id":str(child_run_id),"workflow_id":str(workflow_id),"workflow_version":workflow_version.value,"recursion_depth":recursion_depth},actor=actor,idempotency_key=digest.value,occurred_at=now)
            db.execute("INSERT INTO subflow_links VALUES (?,?,?,NULL,?,?,'running',?,?,?,?,?,?,0,?,?)",(str(link_id),str(parent_run_id),str(child_run_id),str(workflow_id),workflow_version.value,str(parent_run_id),canonical_json(propagation),canonical_json(input_mapping),canonical_json(output_mapping),canonical_json([str(item) for item in artifact_scope]),recursion_depth,now.isoformat(),now.isoformat()))
            audit(db,run_id=parent_run_id,actor=actor,action="subflow.link",target_id=str(link_id),decision="allowed",details={"child":str(child_run_id)},occurred_at=now);db.commit();return link_id

    def propagate(self,link_id:EntityId,target:SubflowStatus,*,actor:str,now:datetime)->None:
        if target not in {SubflowStatus.SUCCEEDED,SubflowStatus.FAILED,SubflowStatus.CANCELLED,SubflowStatus.UNKNOWN}:raise ValueError("invalid Subflow terminal")
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE");row=db.execute("SELECT * FROM subflow_links WHERE link_id=?",(str(link_id),)).fetchone()
            if row is None:raise ValueError("Subflow link not found")
            if row["status"]==target.value:return
            if row["status"] not in {"starting","running"}:raise ValueError("Subflow link already terminal")
            append_control_event(db,run_id=EntityId.parse(row["parent_run_id"]),aggregate_id=link_id,event_type="subflow_link_transitioned",payload={"from":row["status"],"to":target.value,"child_run_id":row["child_run_id"]},actor=actor,idempotency_key=f"propagate:{target.value}",occurred_at=now)
            db.execute("UPDATE subflow_links SET status=?,aggregate_version=aggregate_version+1,updated_at=? WHERE link_id=?",(target.value,now.isoformat(),str(link_id)))
            policy=__import__('json').loads(row["propagation_policy_json"])
            if target is SubflowStatus.FAILED and policy.get("child_failure")=="fail_parent":db.execute("UPDATE workflow_runs SET status='failed',aggregate_version=aggregate_version+1,updated_at=? WHERE run_id=? AND status NOT IN ('succeeded','failed','cancelled')",(now.isoformat(),row["parent_run_id"]))
            db.commit()

