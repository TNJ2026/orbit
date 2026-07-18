"""One Event/projection-derived model shared by Overview, Timeline, Graph, Data and Errors."""

from __future__ import annotations

import json
from pathlib import Path

from ..domain.ids import EntityId
from ..persistence.database import connect_workflow_database


class RunViewService:
    def __init__(self,path:Path|str)->None:self.path=Path(path)
    def get(self,run_id:EntityId,*,after_event:int=0,event_limit:int=200,plan_version:int|None=None)->dict:
        if not 1<=event_limit<=1000:raise ValueError("event_limit must be 1..1000")
        with connect_workflow_database(self.path,read_only=True) as db:
            run=db.execute("SELECT * FROM workflow_runs WHERE run_id=?",(str(run_id),)).fetchone()
            if run is None:raise ValueError("Run not found")
            if plan_version is None:plan_version=db.execute("SELECT MAX(plan_version) FROM execution_plans WHERE run_id=?",(str(run_id),)).fetchone()[0]
            plan=db.execute("SELECT * FROM execution_plans WHERE run_id=? AND plan_version=?",(str(run_id),plan_version)).fetchone()
            events=[{"position":row["global_position"],"event_id":row["event_id"],"aggregate_id":row["aggregate_id"],"type":row["event_type"],"sequence":row["aggregate_sequence"],"occurred_at":row["occurred_at"],"payload":json.loads(row["payload_json"])} for row in db.execute("SELECT * FROM run_events WHERE run_id=? AND global_position>? ORDER BY global_position LIMIT ?",(str(run_id),after_event,event_limit))]
            nodes=[dict(row) for row in db.execute("SELECT node_run_id,node_id,source_plan_version,status,aggregate_version,created_at,updated_at FROM node_runs WHERE run_id=? ORDER BY source_plan_version,node_id,node_run_id",(str(run_id),))]
            errors=[event for event in events if event["type"].endswith(("failed","rejected","unknown")) or event["payload"].get("code")]
            data={"values":[dict(row) for row in db.execute('SELECT value_id,port_id AS name,schema_id,checksum,size_bytes,created_at FROM "values" WHERE run_id=? ORDER BY value_id',(str(run_id),))],"artifacts":[dict(row) for row in db.execute("SELECT artifact_id,schema_id,content_type,checksum,size_bytes,visibility,status FROM artifacts WHERE run_id=? ORDER BY artifact_id",(str(run_id),))]}
            return {"overview":{"run_id":str(run_id),"workflow_id":run["workflow_id"],"workflow_version":run["workflow_version"],"status":run["status"],"version":run["aggregate_version"],"correlation_id":run["correlation_id"],"created_at":run["created_at"],"updated_at":run["updated_at"]},"timeline":{"events":events,"next_cursor":None if len(events)<event_limit else events[-1]["position"]},"graph":{"plan_version":plan_version,"definition_hash":None if plan is None else plan["definition_hash"],"plan":None if plan is None else json.loads(plan["canonical_plan_json"]),"nodes":nodes},"data":data,"errors":errors}
