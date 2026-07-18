"""Shared, sealed read-time upcasting pipeline for Runtime events."""

from __future__ import annotations

from ..domain.runtime import RUNTIME_EVENT_VERSIONS
from ..domain.durable_execution import DURABLE_EVENT_VERSIONS
from ..domain.planner import PLANNER_EVENT_VERSIONS
from ..domain.advanced_events import ADVANCED_EVENT_VERSIONS
from ..domain.upcasting import UpcasterRegistry
from ..persistence.upcasters import EventVersionCatalog, UpcastingEventReader


def runtime_event_reader() -> UpcastingEventReader:
    """Build the single Runtime replay policy used by recovery and snapshots."""
    registry = UpcasterRegistry()
    registry.seal()
    return UpcastingEventReader(
        EventVersionCatalog({
            **RUNTIME_EVENT_VERSIONS, **DURABLE_EVENT_VERSIONS,
            **PLANNER_EVENT_VERSIONS, **ADVANCED_EVENT_VERSIONS,
        }), registry
    )
