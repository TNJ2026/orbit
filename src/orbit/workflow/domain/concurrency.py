"""Frozen optimistic-concurrency and duplicate-command semantics."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from .envelopes import CommandEnvelope
from .ids import EntityId
from .serialization import definition_hash
from .versions import AggregateVersion, DefinitionHash


class CommandDisposition(str, Enum):
    APPLY = "apply"
    REPLAY_PRIOR_RESULT = "replay_prior_result"


class ConcurrencyConflictError(ValueError):
    pass


class IdempotencyConflictError(ValueError):
    pass


@dataclass(frozen=True)
class ProcessedCommand:
    idempotency_key: str
    fingerprint: DefinitionHash
    event_ids: tuple[EntityId, ...]


@dataclass(frozen=True)
class CommandDecision:
    disposition: CommandDisposition
    prior_event_ids: tuple[EntityId, ...] = ()


def command_fingerprint(command: CommandEnvelope) -> DefinitionHash:
    return definition_hash(
        {
            "command_type": command.command_type,
            "aggregate_id": str(command.aggregate_id),
            "correlation_id": str(command.correlation_id),
            "expected_version": command.expected_version.value,
            "actor": command.actor,
            "payload": command.payload,
        }
    )


def evaluate_command(
    current_version: AggregateVersion,
    command: CommandEnvelope,
    processed: Mapping[str, ProcessedCommand],
) -> CommandDecision:
    """Apply, replay, or reject a command without performing persistence."""

    fingerprint = command_fingerprint(command)
    prior = processed.get(command.idempotency_key)
    if prior is not None:
        if prior.fingerprint != fingerprint:
            raise IdempotencyConflictError(
                "idempotency key was already used for a different command"
            )
        return CommandDecision(
            CommandDisposition.REPLAY_PRIOR_RESULT, prior.event_ids
        )
    if command.expected_version != current_version:
        raise ConcurrencyConflictError(
            f"expected aggregate version {command.expected_version.value}, "
            f"found {current_version.value}"
        )
    return CommandDecision(CommandDisposition.APPLY)
