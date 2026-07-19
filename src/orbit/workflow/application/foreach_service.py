"""Recoverable, bounded Foreach scheduler and deterministic aggregate."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Iterable

from ..domain.foreach import MAX_FOREACH_ITEMS, ForeachFailurePolicy, derive_group_id, derive_item_id, stable_aggregate
from ..domain.ids import EntityId
from ..domain.serialization import canonical_json, definition_hash
from ..domain.versions import Revision
from ..persistence.control import append_control_event, audit
from ..persistence.database import connect_workflow_database


class ForeachService:
    def __init__(self,path:Path|str)->None:self.path=Path(path)

    def create_group(self,run_id:EntityId,node_id:str,items:Iterable[Any],*,keys:Iterable[str]|None=None,plan_version:Revision,failure_policy:ForeachFailurePolicy=ForeachFailurePolicy.FAIL_FAST,concurrency_limit:int=8,actor:str,now:datetime)->EntityId:
        values=tuple(items)
        if len(values)>MAX_FOREACH_ITEMS:raise ValueError("Foreach item limit exceeded")
        key_values=tuple(str(i) for i in range(len(values))) if keys is None else tuple(keys)
        if len(key_values)!=len(values) or len(set(key_values))!=len(key_values):raise ValueError("Foreach keys must be unique and match items")
        if concurrency_limit<1:raise ValueError("Foreach concurrency must be positive")
        checksum=definition_hash(values).value; group_id=derive_group_id(run_id,node_id,checksum,plan_version)
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            prior=db.execute("SELECT source_checksum FROM foreach_groups WHERE group_id=?",(str(group_id),)).fetchone()
            if prior is not None:
                if prior[0]!=checksum:raise ValueError("Foreach identity conflict")
                return group_id
            append_control_event(db,run_id=run_id,aggregate_id=group_id,event_type="foreach_group_created",payload={"item_count":len(values),"source_checksum":checksum,"plan_version":plan_version.value},actor=actor,idempotency_key=checksum,occurred_at=now)
            db.execute("INSERT INTO foreach_groups VALUES (?,?,NULL,?,?,'running',?,?,?,NULL,NULL,1,?,?)",(str(group_id),str(run_id),checksum,plan_version.value,failure_policy.value,concurrency_limit,len(values),now.isoformat(),now.isoformat()))
            rows=[]
            for index,(key,value) in enumerate(zip(key_values,values)):
                item_id=derive_item_id(group_id,key,index,checksum,plan_version)
                rows.append((str(item_id),str(group_id),str(run_id),key,index,"pending",canonical_json(value),None,None,0,0,now.isoformat(),now.isoformat()))
            db.executemany(
                """INSERT INTO foreach_items(
                       item_id,group_id,run_id,item_key,item_index,status,
                       input_json,output_json,error_json,retry_count,
                       aggregate_version,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            audit(db,run_id=run_id,actor=actor,action="foreach.create",target_id=str(group_id),decision="allowed",details={"items":len(values)},occurred_at=now);db.commit();return group_id

    def claim_ready(self,group_id:EntityId,*,limit:int,actor:str,now:datetime)->tuple[EntityId,...]:
        if limit<1 or limit>1000:raise ValueError("invalid claim limit")
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE");group=db.execute("SELECT * FROM foreach_groups WHERE group_id=?",(str(group_id),)).fetchone()
            if group is None or group["status"]!="running":return ()
            active=db.execute("SELECT COUNT(*) FROM foreach_items WHERE group_id=? AND status='running'",(str(group_id),)).fetchone()[0]
            capacity=max(0,min(limit,group["concurrency_limit"]-active));rows=db.execute("SELECT * FROM foreach_items WHERE group_id=? AND status IN ('pending','ready') ORDER BY item_index LIMIT ?",(str(group_id),capacity)).fetchall();ids=[]
            for row in rows:
                item_id=EntityId.parse(row["item_id"]);append_control_event(db,run_id=EntityId.parse(row["run_id"]),aggregate_id=item_id,event_type="foreach_item_transitioned",payload={"from":row["status"],"to":"running","group_id":str(group_id)},actor=actor,idempotency_key=f"claim:{row['aggregate_version']}",occurred_at=now)
                db.execute("UPDATE foreach_items SET status='running',aggregate_version=aggregate_version+1,updated_at=? WHERE item_id=? AND aggregate_version=?",(now.isoformat(),str(item_id),row["aggregate_version"]));ids.append(item_id)
            db.commit();return tuple(ids)

    def complete_item(self,item_id:EntityId,*,output:Any=None,error:Any=None,unknown:bool=False,actor:str,now:datetime)->None:
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE");row=db.execute("SELECT i.*,g.failure_policy FROM foreach_items i JOIN foreach_groups g ON g.group_id=i.group_id WHERE item_id=?",(str(item_id),)).fetchone()
            if row is None:raise ValueError("Foreach item not found")
            if row["status"] in {"succeeded","failed","cancelled","unknown"}:return
            target="unknown" if unknown else "failed" if error is not None else "succeeded";run_id=EntityId.parse(row["run_id"])
            append_control_event(db,run_id=run_id,aggregate_id=item_id,event_type="foreach_item_transitioned",payload={"from":row["status"],"to":target,"group_id":row["group_id"]},actor=actor,idempotency_key=f"complete:{row['aggregate_version']}:{target}",occurred_at=now)
            db.execute("UPDATE foreach_items SET status=?,output_json=?,error_json=?,aggregate_version=aggregate_version+1,updated_at=? WHERE item_id=?",(target,None if output is None else canonical_json(output),None if error is None else canonical_json(error),now.isoformat(),str(item_id)))
            if target=="failed" and row["failure_policy"]=="fail_fast":db.execute("UPDATE foreach_items SET status='cancelled',aggregate_version=aggregate_version+1,updated_at=? WHERE group_id=? AND status IN ('pending','ready')",(now.isoformat(),row["group_id"]))
            db.commit()

    def aggregate(self,group_id:EntityId,*,actor:str,now:datetime)->dict[str,Any]:
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE");group=db.execute("SELECT * FROM foreach_groups WHERE group_id=?",(str(group_id),)).fetchone()
            if group is None:raise ValueError("Foreach group not found")
            rows=db.execute("SELECT item_index,item_key,status,output_json,error_json FROM foreach_items WHERE group_id=? ORDER BY item_index",(str(group_id),)).fetchall()
            if any(row["status"] in {"pending","ready","running"} for row in rows):raise ValueError("Foreach group still active")
            value=stable_aggregate(tuple((row["item_index"],row["item_key"],row["status"],None if row["output_json"] is None else json.loads(row["output_json"]),None if row["error_json"] is None else json.loads(row["error_json"])) for row in rows));checksum=definition_hash(value).value
            status="partial" if value["partial"] and group["failure_policy"]=="partial_success" else "failed" if any(row["status"] in {"failed","unknown"} for row in rows) else "completed"
            append_control_event(db,run_id=EntityId.parse(group["run_id"]),aggregate_id=group_id,event_type="foreach_aggregated",payload={"status":status,"checksum":checksum},actor=actor,idempotency_key=checksum,occurred_at=now)
            db.execute("UPDATE foreach_groups SET status=?,aggregate_json=?,aggregate_checksum=?,aggregate_version=aggregate_version+1,updated_at=? WHERE group_id=?",(status,canonical_json(value),checksum,now.isoformat(),str(group_id)));db.commit();return dict(value)
