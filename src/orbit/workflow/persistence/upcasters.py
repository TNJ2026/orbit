"""Read-time event version selection; stored rows remain unchanged."""

from __future__ import annotations

from collections.abc import Mapping

from ..domain.persistence import StoredEvent, UnsupportedEventVersionError
from ..domain.schemas import validate_contract
from ..domain.serialization import to_primitive
from ..domain.upcasting import UpcasterRegistry


class EventVersionCatalog:
    def __init__(self, versions: Mapping[str, int]) -> None:
        if not versions or any(version < 1 for version in versions.values()):
            raise ValueError("event version catalog requires positive versions")
        self._versions = dict(versions)

    def current_version(self, event_type: str) -> int:
        try:
            return self._versions[event_type]
        except KeyError:
            raise UnsupportedEventVersionError(
                f"unknown event type {event_type!r}"
            ) from None


class UpcastingEventReader:
    def __init__(
        self, catalog: EventVersionCatalog, registry: UpcasterRegistry
    ) -> None:
        if not registry.sealed:
            raise ValueError("upcaster registry must be sealed before replay")
        self.catalog = catalog
        self.registry = registry

    def read(self, stored: StoredEvent) -> StoredEvent:
        target = self.catalog.current_version(stored.envelope.event_type)
        current = stored.envelope.event_version.value
        if current > target:
            raise UnsupportedEventVersionError(
                f"future {stored.envelope.event_type} version {current}; current is {target}"
            )
        try:
            envelope = self.registry.upcast(stored.envelope, target)
        except ValueError as exc:
            raise UnsupportedEventVersionError(
                f"{stored.envelope.event_id} {stored.envelope.event_type} "
                f"v{current}->v{target}: {exc}"
            ) from None
        validate_contract(to_primitive(envelope), "event-envelope/1.0")
        return StoredEvent(stored.run_id, stored.global_position, envelope)
