"""Derived telemetry that can never drive Runtime state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime,timezone
import json
from threading import Lock
from typing import Any,Callable,Mapping

from ..security.redaction import Redactor


@dataclass(frozen=True)
class TraceContext:
    correlation_id:str;span_id:str;parent_span_id:str|None=None

class StructuredLogger:
    def __init__(self,sink:Callable[[str],None],redactor:Redactor|None=None)->None:self.sink=sink;self.redactor=redactor or Redactor(())
    def emit(self,level:str,message:str,*,trace:TraceContext|None=None,fields:Mapping[str,Any]|None=None)->None:
        value={"timestamp":datetime.now(timezone.utc).isoformat(),"level":level,"message":message,"fields":dict(fields or {})}
        if trace:value["trace"]={"correlation_id":trace.correlation_id,"span_id":trace.span_id,"parent_span_id":trace.parent_span_id}
        try:self.sink(json.dumps(self.redactor.redact(value),sort_keys=True,separators=(",",":")))
        except Exception:pass

class MetricRegistry:
    """Low-cardinality in-process metrics; IDs are rejected as labels."""
    ALLOWED_LABELS=frozenset({"status","kind","category","provider","handler","operation"})
    def __init__(self)->None:self._values={};self._lock=Lock()
    def add(self,name:str,value:float=1,**labels:str)->None:
        if not set(labels)<=self.ALLOWED_LABELS:raise ValueError("high-cardinality metric label denied")
        key=(name,tuple(sorted(labels.items())))
        with self._lock:self._values[key]=self._values.get(key,0)+value
    def snapshot(self):
        with self._lock:return tuple((name,dict(labels),value) for (name,labels),value in sorted(self._values.items()))

