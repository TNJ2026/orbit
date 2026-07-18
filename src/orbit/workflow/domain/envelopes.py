"""Frozen command and event envelopes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Mapping

from .ids import EntityId, new_id
from .serialization import freeze_json
from .versions import AggregateVersion, Revision


_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


def _aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _event_type(value: str, field_name: str) -> None:
    if not _TYPE_RE.fullmatch(value):
        raise ValueError(f"invalid {field_name}: {value!r}")


@dataclass(frozen=True)
class CommandEnvelope:
    command_id: EntityId
    command_type: str
    aggregate_id: EntityId
    correlation_id: EntityId
    expected_version: AggregateVersion
    idempotency_key: str
    actor: str
    issued_at: datetime
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _event_type(self.command_type, "command_type")
        _aware(self.issued_at, "issued_at")
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        if not self.actor.strip():
            raise ValueError("actor is required")
        object.__setattr__(self, "payload", freeze_json(self.payload))

    @classmethod
    def create(
        cls,
        *,
        command_type: str,
        aggregate_id: EntityId,
        correlation_id: EntityId | None = None,
        expected_version: AggregateVersion,
        idempotency_key: str,
        actor: str,
        payload: Mapping[str, Any] | None = None,
        issued_at: datetime | None = None,
    ) -> CommandEnvelope:
        return cls(
            command_id=new_id("command"),
            command_type=command_type,
            aggregate_id=aggregate_id,
            correlation_id=correlation_id or aggregate_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            actor=actor,
            issued_at=issued_at or datetime.now(timezone.utc),
            payload=payload or {},
        )


@dataclass(frozen=True)
class EventEnvelope:
    event_id: EntityId
    event_type: str
    event_version: Revision
    aggregate_id: EntityId
    sequence: Revision
    correlation_id: EntityId
    causation_id: EntityId
    occurred_at: datetime
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _event_type(self.event_type, "event_type")
        _aware(self.occurred_at, "occurred_at")
        object.__setattr__(self, "payload", freeze_json(self.payload))

    @classmethod
    def from_command(
        cls,
        command: CommandEnvelope,
        *,
        event_type: str,
        sequence: Revision,
        payload: Mapping[str, Any] | None = None,
        event_version: Revision = Revision(1),
        occurred_at: datetime | None = None,
    ) -> EventEnvelope:
        return cls(
            event_id=new_id("event"),
            event_type=event_type,
            event_version=event_version,
            aggregate_id=command.aggregate_id,
            sequence=sequence,
            correlation_id=command.correlation_id,
            causation_id=command.command_id,
            occurred_at=occurred_at or datetime.now(timezone.utc),
            payload=payload or {},
        )
