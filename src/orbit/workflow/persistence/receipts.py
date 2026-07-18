"""Persistent command idempotency receipts."""

from __future__ import annotations

from datetime import datetime
import json
import sqlite3

from ..domain.concurrency import (
    CommandDecision,
    CommandDisposition,
    command_fingerprint,
)
from ..domain.envelopes import CommandEnvelope
from ..domain.ids import EntityId
from ..domain.persistence import CommandReceipt, IdempotencyConflictError
from ..domain.serialization import canonical_json
from ..domain.versions import AggregateVersion, DefinitionHash


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class SQLiteCommandReceiptStore:
    def __init__(self, connection: sqlite3.Connection, *, fault_hook=None) -> None:
        self.connection = connection
        self.fault_hook = fault_hook

    def _fault(self, point: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(point)

    def get(self, aggregate_id: EntityId, idempotency_key: str) -> CommandReceipt | None:
        row = self.connection.execute(
            """
            SELECT * FROM command_receipts
            WHERE aggregate_id = ? AND idempotency_key = ?
            """,
            (str(aggregate_id), idempotency_key),
        ).fetchone()
        if row is None:
            return None
        return CommandReceipt(
            run_id=EntityId.parse(row["run_id"]),
            aggregate_id=EntityId.parse(row["aggregate_id"]),
            idempotency_key=row["idempotency_key"],
            command_fingerprint=DefinitionHash(row["command_fingerprint"]),
            command_id=EntityId.parse(row["command_id"]),
            expected_version=AggregateVersion(row["expected_version"]),
            result_event_ids=tuple(
                EntityId.parse(value) for value in json.loads(row["result_event_ids_json"])
            ),
            committed_at=_datetime(row["committed_at"]),
        )

    def decide(self, command: CommandEnvelope) -> CommandDecision | None:
        receipt = self.get(command.aggregate_id, command.idempotency_key)
        if receipt is None:
            return None
        if receipt.command_fingerprint != command_fingerprint(command):
            raise IdempotencyConflictError(
                "idempotency key was already used for a different command"
            )
        return CommandDecision(
            CommandDisposition.REPLAY_PRIOR_RESULT,
            receipt.result_event_ids,
        )

    def record(
        self,
        run_id: EntityId,
        command: CommandEnvelope,
        result_event_ids: tuple[EntityId, ...],
        committed_at: datetime,
    ) -> CommandReceipt:
        if not self.connection.in_transaction:
            raise RuntimeError("Receipt write requires an active UnitOfWork")
        if not result_event_ids:
            raise ValueError("receipt requires at least one result event id")
        event_rows = self.connection.execute(
            f"""
            SELECT event_id, causation_id FROM run_events
            WHERE event_id IN ({','.join('?' for _ in result_event_ids)})
            """,
            tuple(str(item) for item in result_event_ids),
        ).fetchall()
        if len(event_rows) != len(result_event_ids):
            raise ValueError("receipt references missing events")
        if any(row["causation_id"] != str(command.command_id) for row in event_rows):
            raise ValueError("receipt events must be caused by the command")
        receipt = CommandReceipt(
            run_id=run_id,
            aggregate_id=command.aggregate_id,
            idempotency_key=command.idempotency_key,
            command_fingerprint=command_fingerprint(command),
            command_id=command.command_id,
            expected_version=command.expected_version,
            result_event_ids=result_event_ids,
            committed_at=committed_at,
        )
        self._fault("before_receipt_insert")
        try:
            self.connection.execute(
                """
                INSERT INTO command_receipts(
                    run_id, aggregate_id, idempotency_key, command_fingerprint,
                    command_id, expected_version, result_event_ids_json, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(receipt.run_id), str(receipt.aggregate_id),
                    receipt.idempotency_key, receipt.command_fingerprint.value,
                    str(receipt.command_id), receipt.expected_version.value,
                    canonical_json([str(item) for item in receipt.result_event_ids]),
                    receipt.committed_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                ),
            )
        except sqlite3.IntegrityError:
            prior = self.get(command.aggregate_id, command.idempotency_key)
            if prior is not None and prior.command_fingerprint != receipt.command_fingerprint:
                raise IdempotencyConflictError(
                    "idempotency key was concurrently used for a different command"
                ) from None
            raise
        self._fault("after_receipt_insert")
        return receipt
