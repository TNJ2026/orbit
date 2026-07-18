"""Command/Event contract attached to every frozen state transition."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .states import allowed_transitions, machine_name


@dataclass(frozen=True)
class TransitionContract:
    machine: str
    current: str
    target: str
    command_type: str
    event_type: str
    precondition: str
    idempotency_scope: str


def transition_contract(current: Enum, target: Enum) -> TransitionContract:
    if target not in allowed_transitions(current):
        raise ValueError(f"no transition contract for {current.value} -> {target.value}")
    machine = machine_name(current)
    return TransitionContract(
        machine=machine,
        current=str(current.value),
        target=str(target.value),
        command_type=f"transition_{machine}",
        event_type=f"{machine}_transitioned",
        precondition=f"current_status == {current.value} and expected_version matches",
        idempotency_scope="aggregate_id + idempotency_key",
    )
