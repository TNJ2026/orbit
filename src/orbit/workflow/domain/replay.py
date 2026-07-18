"""Pure event replay contract.

The reducer receives only prior state and a recorded event.  This module has no
clock, randomness, persistence, planner, handler, tool, HTTP, or artifact APIs.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TypeVar

from .envelopes import EventEnvelope


StateT = TypeVar("StateT")
Reducer = Callable[[StateT, EventEnvelope], StateT]


def replay_events(
    initial_state: StateT,
    events: Iterable[EventEnvelope],
    reducer: Reducer[StateT],
) -> StateT:
    """Replay a strictly ordered event stream without performing side effects."""

    state = initial_state
    previous_sequence = 0
    aggregate = None
    for event in events:
        if event.sequence.value <= previous_sequence:
            raise ValueError("event sequence must be strictly increasing")
        if aggregate is None:
            aggregate = event.aggregate_id
        elif event.aggregate_id != aggregate:
            raise ValueError("one replay stream cannot mix aggregates")
        state = reducer(state, event)
        previous_sequence = event.sequence.value
    return state
