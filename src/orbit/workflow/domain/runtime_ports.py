"""Structural ports for the deterministic Runtime Kernel boundary."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from .envelopes import CommandEnvelope
from .ids import EntityId
from .runtime import CommandResult


@runtime_checkable
class RuntimeKernelPort(Protocol):
    def handle(self, command: CommandEnvelope) -> CommandResult: ...


@runtime_checkable
class RuntimeLoggerPort(Protocol):
    def __call__(self, message: str, fields: Mapping[str, Any]) -> None: ...


@runtime_checkable
class SnapshotCoordinatorPort(Protocol):
    def consider(self, run_id: EntityId) -> EntityId | None: ...
