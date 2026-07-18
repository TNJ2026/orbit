"""Deterministic construction of Planner event facts."""

from __future__ import annotations

from ..domain.envelopes import EventEnvelope
from ..domain.ids import EntityId
from ..domain.planner import PLANNER_EVENT_VERSIONS
from ..domain.serialization import freeze_json
from ..domain.schemas import validate_contract
from ..domain.versions import Revision
from ..runtime.events import derived_id


def planner_event(*, attempt, ordinal, event_type, now, payload):
    if event_type not in PLANNER_EVENT_VERSIONS:
        raise ValueError(f"unregistered Planner event {event_type}")
    validate_contract(payload, f"planner-event/{event_type.replace('_', '-')}/1.0")
    return EventEnvelope(
        derived_id("event", attempt.attempt_id, attempt.aggregate_version.value, ordinal, event_type),
        event_type, Revision(1), attempt.attempt_id,
        Revision(attempt.aggregate_version.value + ordinal), attempt.run_id,
        derived_id("command", attempt.attempt_id, attempt.aggregate_version.value, event_type),
        now, freeze_json(payload),
    )
