"""Deterministic Runtime Event construction and payload validation."""

from __future__ import annotations

import hashlib
from typing import Any

from ..domain.envelopes import CommandEnvelope, EventEnvelope
from ..domain.ids import EntityId
from ..domain.serialization import freeze_json
from ..domain.runtime import validate_runtime_event_payload
from ..domain.durable_execution import DURABLE_EVENT_VERSIONS
from ..domain.schemas import validate_contract
from ..domain.versions import Revision


def derived_id(kind: str, *parts: object) -> EntityId:
    raw = "|".join(str(item) for item in parts)
    return EntityId(kind, hashlib.sha256(raw.encode("utf-8")).hexdigest())


def runtime_event(
    command: CommandEnvelope,
    *,
    ordinal: int,
    aggregate_id: EntityId,
    sequence: int,
    event_type: str,
    payload: dict[str, Any],
) -> EventEnvelope:
    namespace = "durable-event" if event_type in DURABLE_EVENT_VERSIONS else "runtime-event"
    validate_contract(payload, f"{namespace}/{event_type.replace('_', '-')}/1.0")
    if namespace == "runtime-event":
        validate_runtime_event_payload(event_type, payload)
    return EventEnvelope(
        derived_id("event", command.command_id, ordinal), event_type, Revision(1),
        aggregate_id, Revision(sequence), command.correlation_id, command.command_id,
        command.issued_at, freeze_json(payload),
    )
