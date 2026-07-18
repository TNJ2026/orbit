"""Pure, sequential Event upcasting contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from .envelopes import EventEnvelope
from .versions import Revision


Upcaster = Callable[[EventEnvelope], EventEnvelope]


class UpcasterRegistry:
    def __init__(self) -> None:
        self._upcasters: dict[tuple[str, int], Upcaster] = {}
        self._sealed = False

    @property
    def sealed(self) -> bool:
        return self._sealed

    def seal(self) -> None:
        """Prevent runtime mutation after application bootstrap."""

        self._sealed = True

    def register(self, event_type: str, from_version: int, upcaster: Upcaster) -> None:
        if self._sealed:
            raise RuntimeError("upcaster registry is sealed")
        key = (event_type, from_version)
        if key in self._upcasters:
            raise ValueError(f"duplicate upcaster: {event_type} v{from_version}")
        if from_version < 1:
            raise ValueError("from_version must be positive")
        self._upcasters[key] = upcaster

    def upcast(self, event: EventEnvelope, target_version: int) -> EventEnvelope:
        current = event
        while current.event_version.value < target_version:
            key = (current.event_type, current.event_version.value)
            try:
                upcaster = self._upcasters[key]
            except KeyError:
                raise ValueError(
                    f"missing upcaster for {current.event_type} "
                    f"v{current.event_version.value}"
                ) from None
            upgraded = upcaster(current)
            expected = current.event_version.value + 1
            if upgraded.event_version != Revision(expected):
                raise ValueError("upcaster must advance exactly one event version")
            if (
                upgraded.event_id != current.event_id
                or upgraded.aggregate_id != current.aggregate_id
                or upgraded.sequence != current.sequence
                or upgraded.correlation_id != current.correlation_id
                or upgraded.causation_id != current.causation_id
                or upgraded.occurred_at != current.occurred_at
            ):
                raise ValueError("upcaster cannot change event identity or causality")
            current = upgraded
        if current.event_version.value != target_version:
            raise ValueError("cannot downcast an event")
        return current


def with_payload(
    event: EventEnvelope, payload: dict, *, version: int
) -> EventEnvelope:
    """Helper for pure upcasters that only reshape payload data."""

    return replace(event, event_version=Revision(version), payload=payload)
