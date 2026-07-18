"""Shared transactional helpers for the advanced command services."""

from __future__ import annotations

from datetime import datetime
import hashlib
from typing import Any

from ..domain.envelopes import EventEnvelope
from ..domain.ids import EntityId
from ..domain.serialization import canonical_json, definition_hash, freeze_json
from ..domain.versions import AggregateVersion, Revision
from .event_store import SQLiteEventStore


def append_control_event(connection, *, run_id: EntityId, aggregate_id: EntityId, event_type: str, payload: dict[str, Any], actor: str, idempotency_key: str, occurred_at: datetime) -> EventEnvelope:
    store = SQLiteEventStore(connection)
    head = store.stream_head(aggregate_id)
    seed = canonical_json({"aggregate": str(aggregate_id), "key": idempotency_key, "type": event_type})
    command_id = EntityId("command", hashlib.sha256(seed.encode()).hexdigest())
    event = EventEnvelope(
        EntityId("event", hashlib.sha256((seed + "|event").encode()).hexdigest()),
        event_type, Revision(1), aggregate_id, Revision(head.value + 1), run_id,
        command_id, occurred_at, freeze_json(payload),
    )
    store.append(run_id, aggregate_id, head, (event,))
    return event


def audit(connection, *, run_id: EntityId | None, actor: str, action: str, target_id: str, decision: str, details: Any, occurred_at: datetime) -> None:
    digest = definition_hash({"run": None if run_id is None else str(run_id), "actor": actor, "action": action, "target": target_id, "decision": decision, "details": details, "at": occurred_at})
    connection.execute(
        "INSERT OR IGNORE INTO audit_records(audit_id, run_id, actor, action, target_id, decision, details_json, correlation_id, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"audit:{digest.value.removeprefix('sha256:')}", None if run_id is None else str(run_id), actor, action, target_id, decision, canonical_json(details), None if run_id is None else str(run_id), occurred_at.isoformat()),
    )

