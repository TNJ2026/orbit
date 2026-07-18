"""Dry-run-first repair plans; Apply delegates to audited command submitters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable,Mapping,Any


@dataclass(frozen=True)
class RepairAction:
    action_id:str;kind:str;entity_id:str;run_id:str;expected_version:int;reason:str;reversible:bool=False
@dataclass(frozen=True)
class RepairReport:
    dry_run:bool;actions:tuple[RepairAction,...];applied:tuple[str,...];failed:tuple[tuple[str,str],...]

class RepairManager:
    def __init__(self,submitters:Mapping[str,Callable[[RepairAction,str,datetime],Any]])->None:self.submitters=dict(submitters)
    def execute(self,actions,*,apply:bool=False,actor:str="system:repair",now:datetime)->RepairReport:
        values=tuple(actions)
        if not apply:return RepairReport(True,values,(),())
        applied=[];failed=[]
        for action in values:
            submit=self.submitters.get(action.kind)
            if submit is None:failed.append((action.action_id,"no audited command submitter"));continue
            try:submit(action,actor,now);applied.append(action.action_id)
            except Exception as exc:failed.append((action.action_id,type(exc).__name__))
        return RepairReport(False,values,tuple(applied),tuple(failed))

