"""Authorized deterministic PlanningContext construction."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..domain.ids import EntityId
from ..domain.planner import PLANNER_SCHEMA_VERSION, PlanningContext
from ..domain.serialization import to_primitive
from ..domain.versions import Revision


_DATA_FIELDS = {
    "port_id", "schema_id", "transport", "checksum", "size_bytes",
    "artifact_id", "visibility", "scope_id",
}
_EVENT_FIELDS = {"event_id", "event_type", "aggregate_id", "sequence", "occurred_at", "summary"}


def build_planning_context(
    *,
    run_id: EntityId,
    plan_version: Revision,
    goal: str,
    graph_summary: Mapping[str, Any],
    available_data: Iterable[Mapping[str, Any]] = (),
    available_capabilities: Iterable[str] = (),
    remaining_limits: Mapping[str, int] | None = None,
    recent_events: Iterable[Mapping[str, Any]] = (),
    max_recent_events: int = 50,
) -> PlanningContext:
    if max_recent_events < 0 or max_recent_events > 200:
        raise ValueError("max_recent_events must be between 0 and 200")
    data = []
    for item in available_data:
        extra = set(item) - _DATA_FIELDS
        if extra:
            raise ValueError(f"unauthorized PlanningContext data fields: {sorted(extra)}")
        data.append({key: to_primitive(item[key]) for key in sorted(item)})
    events = []
    for item in tuple(recent_events)[-max_recent_events:]:
        extra = set(item) - _EVENT_FIELDS
        if extra:
            raise ValueError(f"unauthorized PlanningContext event fields: {sorted(extra)}")
        events.append({key: to_primitive(item[key]) for key in sorted(item)})
    summary = {
        key: to_primitive(graph_summary[key])
        for key in ("status", "plan_version", "nodes", "tokens", "joins", "waiting_reason")
        if key in graph_summary
    }
    return PlanningContext(
        PLANNER_SCHEMA_VERSION, run_id, plan_version, goal, summary,
        tuple(sorted(data, key=lambda item: (item.get("port_id", ""), item.get("artifact_id", "")))),
        tuple(available_capabilities), remaining_limits or {}, tuple(events),
    )
