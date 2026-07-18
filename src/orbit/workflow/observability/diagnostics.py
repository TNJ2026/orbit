"""Evidence-backed why-waiting and lineage diagnostics."""

from __future__ import annotations

import json
from pathlib import Path

from ..domain.ids import EntityId
from ..persistence.database import connect_workflow_database


class DiagnosticsService:
    def __init__(self,path:Path|str)->None:self.path=Path(path)
    def why(self,run_id:EntityId)->dict:
        with connect_workflow_database(self.path,read_only=True) as db:
            run=db.execute("SELECT * FROM workflow_runs WHERE run_id=?",(str(run_id),)).fetchone()
            if run is None:raise ValueError("Run not found")
            responsibilities=[]
            queries=(
                ("human","SELECT task_id AS id,status,kind AS detail FROM human_tasks WHERE run_id=? AND status IN ('waiting','claimed')"),
                ("job","SELECT job_id AS id,status,job_kind AS detail FROM jobs WHERE run_id=? AND status IN ('ready','leased','running','retry_wait')"),
                ("timer","SELECT timer_id AS id,status,purpose AS detail FROM durable_timers WHERE run_id=? AND status IN ('scheduled','leased')"),
                ("planner","SELECT attempt_id AS id,status,provider_id AS detail FROM planner_attempts WHERE run_id=? AND status IN ('requested','running','response_received','unknown')"),
                ("foreach","SELECT group_id AS id,status,failure_policy AS detail FROM foreach_groups WHERE run_id=? AND status IN ('pending','running')"),
                ("subflow","SELECT link_id AS id,status,child_run_id AS detail FROM subflow_links WHERE parent_run_id=? AND status IN ('starting','running','unknown')"),
            )
            for kind,sql in queries:
                for row in db.execute(sql,(str(run_id),)):responsibilities.append({"kind":kind,"id":row["id"],"status":row["status"],"detail":row["detail"]})
            budget=db.execute("SELECT * FROM budget_accounts WHERE run_id=?",(str(run_id),)).fetchone()
            return {"run_id":str(run_id),"status":run["status"],"aggregate_version":run["aggregate_version"],"responsibilities":responsibilities,"budget":None if budget is None else {"total":budget["total_microunits"],"reserved":budget["reserved_microunits"],"consumed":budget["consumed_microunits"],"remaining":budget["total_microunits"]-budget["reserved_microunits"]-budget["consumed_microunits"]}}
    def artifact_lineage(self,artifact_id:EntityId)->dict:
        with connect_workflow_database(self.path,read_only=True) as db:
            artifact=db.execute("SELECT * FROM artifacts WHERE artifact_id=?",(str(artifact_id),)).fetchone()
            if artifact is None:raise ValueError("Artifact not found")
            links=[dict(row) for row in db.execute("SELECT * FROM artifact_links WHERE artifact_id=? ORDER BY link_id",(str(artifact_id),))]
            return {"artifact_id":str(artifact_id),"checksum":artifact["checksum"],"producer_id":artifact["producer_id"],"visibility":artifact["visibility"],"links":links}

