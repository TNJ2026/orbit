"""Explicit shared context for command-family handlers using one parent UoW."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domain.envelopes import CommandEnvelope


@dataclass(frozen=True)
class KernelContext:
    uow: Any
    command: CommandEnvelope
    events: Any

    def __post_init__(self) -> None:
        if getattr(self.uow, "connection", object()) is None:
            raise RuntimeError("KernelContext requires the active parent UnitOfWork")

