"""Append-only SQLite Event Store with aggregate and global ordering."""

from __future__ import annotations

from datetime import datetime
import json
import sqlite3
from typing import Iterable

from ..domain.envelopes import EventEnvelope
from ..domain.ids import EntityId
from ..domain.persistence import (
    ConcurrencyConflictError,
    DuplicateEventIdError,
    EventSequenceError,
    StoredEvent,
)
from ..domain.schemas import validate_contract
from ..domain.serialization import canonical_json, to_primitive
from ..domain.versions import AggregateVersion, Revision


MAX_EVENTS_PER_APPEND = 100
MAX_EVENT_PAYLOAD_BYTES = 1024 * 1024


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _stored_event(row: sqlite3.Row) -> StoredEvent:
    envelope = EventEnvelope(
        event_id=EntityId.parse(row["event_id"]),
        event_type=row["event_type"],
        event_version=Revision(row["event_version"]),
        aggregate_id=EntityId.parse(row["aggregate_id"]),
        sequence=Revision(row["aggregate_sequence"]),
        correlation_id=EntityId.parse(row["correlation_id"]),
        causation_id=EntityId.parse(row["causation_id"]),
        occurred_at=_datetime(row["occurred_at"]),
        payload=json.loads(row["payload_json"]),
    )
    return StoredEvent(
        run_id=EntityId.parse(row["run_id"]),
        global_position=row["global_position"],
        envelope=envelope,
    )


class SQLiteEventStore:
    def __init__(self, connection: sqlite3.Connection, *, fault_hook=None) -> None:
        self.connection = connection
        self.fault_hook = fault_hook

    def _fault(self, point: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(point)

    def stream_head(self, aggregate_id: EntityId) -> AggregateVersion:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(aggregate_sequence), 0) FROM run_events WHERE aggregate_id = ?",
            (str(aggregate_id),),
        ).fetchone()
        return AggregateVersion(int(row[0]))

    def append(
        self,
        run_id: EntityId,
        aggregate_id: EntityId,
        expected_version: AggregateVersion,
        events: Iterable[EventEnvelope],
    ) -> tuple[StoredEvent, ...]:
        if not self.connection.in_transaction:
            raise RuntimeError("Event append requires an active UnitOfWork")
        values = tuple(events)
        if not values:
            raise ValueError("event append requires at least one event")
        if len(values) > MAX_EVENTS_PER_APPEND:
            raise ValueError(f"event append exceeds limit {MAX_EVENTS_PER_APPEND}")
        actual = self.stream_head(aggregate_id)
        if actual != expected_version:
            raise ConcurrencyConflictError(
                aggregate_id, expected_version.value, actual.value
            )
        for offset, event in enumerate(values, start=1):
            if event.aggregate_id != aggregate_id:
                raise EventSequenceError("event aggregate does not match append stream")
            expected_sequence = expected_version.value + offset
            if event.sequence.value != expected_sequence:
                raise EventSequenceError(
                    f"expected event sequence {expected_sequence}, got {event.sequence.value}"
                )
            validate_contract(to_primitive(event), "event-envelope/1.0")
            if len(canonical_json(event.payload).encode("utf-8")) > MAX_EVENT_PAYLOAD_BYTES:
                raise ValueError(
                    f"event payload exceeds {MAX_EVENT_PAYLOAD_BYTES} bytes"
                )

        stored: list[StoredEvent] = []
        for event in values:
            self._fault("before_event_insert")
            try:
                cursor = self.connection.execute(
                    """
                    INSERT INTO run_events(
                        event_id, run_id, aggregate_id, aggregate_sequence,
                        event_type, event_version, correlation_id, causation_id,
                        occurred_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(event.event_id), str(run_id), str(event.aggregate_id),
                        event.sequence.value, event.event_type, event.event_version.value,
                        str(event.correlation_id), str(event.causation_id),
                        to_primitive(event.occurred_at), canonical_json(event.payload),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                duplicate = self.connection.execute(
                    "SELECT 1 FROM run_events WHERE event_id = ?",
                    (str(event.event_id),),
                ).fetchone()
                if duplicate is not None:
                    raise DuplicateEventIdError(str(event.event_id)) from None
                raise
            stored.append(StoredEvent(run_id, int(cursor.lastrowid), event))
            self._fault("after_event_insert")
        return tuple(stored)

    def read_stream(
        self,
        aggregate_id: EntityId,
        *,
        after_sequence: int = 0,
        to_sequence: int | None = None,
        limit: int = 1000,
    ) -> tuple[StoredEvent, ...]:
        if limit < 1 or limit > 10_000:
            raise ValueError("limit must be between 1 and 10000")
        sql = """
            SELECT * FROM run_events
            WHERE aggregate_id = ? AND aggregate_sequence > ?
        """
        parameters: list[object] = [str(aggregate_id), after_sequence]
        if to_sequence is not None:
            sql += " AND aggregate_sequence <= ?"
            parameters.append(to_sequence)
        sql += " ORDER BY aggregate_sequence LIMIT ?"
        parameters.append(limit)
        return tuple(
            _stored_event(row)
            for row in self.connection.execute(sql, parameters).fetchall()
        )

    def read_run(
        self,
        run_id: EntityId,
        *,
        after_global_position: int = 0,
        limit: int = 1000,
    ) -> tuple[StoredEvent, ...]:
        return self._read_global(
            "run_id = ?", (str(run_id),), after_global_position, limit
        )

    def read_all(
        self,
        *,
        after_global_position: int = 0,
        limit: int = 1000,
    ) -> tuple[StoredEvent, ...]:
        return self._read_global("1 = 1", (), after_global_position, limit)

    def _read_global(
        self,
        predicate: str,
        predicate_parameters: tuple[object, ...],
        after_global_position: int,
        limit: int,
    ) -> tuple[StoredEvent, ...]:
        if limit < 1 or limit > 10_000:
            raise ValueError("limit must be between 1 and 10000")
        rows = self.connection.execute(
            f"""
            SELECT * FROM run_events
            WHERE {predicate} AND global_position > ?
            ORDER BY global_position LIMIT ?
            """,
            (*predicate_parameters, after_global_position, limit),
        ).fetchall()
        return tuple(_stored_event(row) for row in rows)
