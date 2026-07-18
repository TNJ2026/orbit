"""Fail-closed capability issuance, delegation, revocation and Artifact ACL."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Iterable

from ..domain.ids import EntityId
from ..domain.serialization import canonical_json, definition_hash
from ..persistence.control import append_control_event, audit
from ..persistence.database import connect_workflow_database


class CapabilityDenied(PermissionError):pass


class CapabilityService:
    def __init__(self,path:Path|str)->None:self.path=Path(path)
    def issue(self,subject:str,scope:str,permissions:Iterable[str],*,actor:str,now:datetime,expires_at:datetime|None=None,parent:EntityId|None=None,run_id:EntityId|None=None)->EntityId:
        values=tuple(sorted(set(permissions)))
        if not subject or not scope or not values:raise ValueError("capability subject, scope and permissions required")
        digest=definition_hash({"subject":subject,"scope":scope,"permissions":values,"parent":None if parent is None else str(parent),"issued":now});capability_id=EntityId("capability",digest.value.removeprefix("sha256:"))
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            if parent is not None:
                row=db.execute("SELECT * FROM security_capabilities WHERE capability_id=?",(str(parent),)).fetchone()
                if row is None or row["status"]!="active":raise CapabilityDenied("parent capability unavailable")
                parent_permissions=set(json.loads(row["permissions_json"]));
                if not set(values)<=parent_permissions or not scope.startswith(row["scope"]):raise CapabilityDenied("delegation expands authority")
            db.execute("INSERT INTO security_capabilities VALUES (?,?,?,?,?,'active',?,?,NULL)",(str(capability_id),subject,scope,canonical_json(values),None if parent is None else str(parent),now.isoformat(),None if expires_at is None else expires_at.isoformat()))
            if run_id is not None:append_control_event(db,run_id=run_id,aggregate_id=capability_id,event_type="capability_issued",payload={"subject":subject,"scope":scope,"permissions":values},actor=actor,idempotency_key=digest.value,occurred_at=now)
            audit(db,run_id=run_id,actor=actor,action="capability.issue",target_id=str(capability_id),decision="allowed",details={"subject":subject,"scope":scope,"permissions":values},occurred_at=now);db.commit();return capability_id
    def revoke(self,capability_id:EntityId,*,actor:str,now:datetime,run_id:EntityId|None=None)->None:
        with connect_workflow_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE");cursor=db.execute("UPDATE security_capabilities SET status='revoked',revoked_at=? WHERE capability_id=? AND status='active'",(now.isoformat(),str(capability_id)))
            if cursor.rowcount and run_id is not None:append_control_event(db,run_id=run_id,aggregate_id=capability_id,event_type="capability_revoked",payload={"actor":actor},actor=actor,idempotency_key="revoke",occurred_at=now)
            db.execute("UPDATE security_capabilities SET status='revoked',revoked_at=? WHERE parent_capability_id=? AND status='active'",(now.isoformat(),str(capability_id)));db.commit()
    def authorize(self,subject:str,scope:str,permission:str,*,now:datetime)->EntityId:
        with connect_workflow_database(self.path,read_only=True) as db:
            rows=db.execute("SELECT * FROM security_capabilities WHERE subject=? AND status='active' ORDER BY capability_id",(subject,)).fetchall()
            for row in rows:
                if row["expires_at"] and datetime.fromisoformat(row["expires_at"])<=now:continue
                if scope.startswith(row["scope"]) and permission in json.loads(row["permissions_json"]):return EntityId.parse(row["capability_id"])
        raise CapabilityDenied("capability denied")
    def grant_artifact(self,artifact_id:EntityId,subject:str,permission:str,*,actor:str,now:datetime)->None:
        with connect_workflow_database(self.path) as db:db.execute("INSERT OR IGNORE INTO artifact_acl VALUES (?,?,?,?,?)",(str(artifact_id),subject,permission,actor,now.isoformat()))
    def authorize_artifact(self,artifact_id:EntityId,subject:str,permission:str)->None:
        with connect_workflow_database(self.path,read_only=True) as db:
            if db.execute("SELECT 1 FROM artifact_acl WHERE artifact_id=? AND subject=? AND permission=?",(str(artifact_id),subject,permission)).fetchone() is None:raise CapabilityDenied("Artifact ACL denied")

