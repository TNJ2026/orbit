"""Deterministic aggregate and run-view rehydration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from ..domain.ids import EntityId
from ..domain.persistence import EventSequenceError, StoredEvent
from ..domain.replay import replay_events
from ..domain.versions import SchemaVersion
from .snapshots import SQLiteSnapshotStore
from .upcasters import UpcastingEventReader


State = TypeVar("State")


class ReducerRegistry:
    """Bootstrap-time registry; reducer selection cannot change during replay."""

    def __init__(self) -> None:
        self._reducers: dict[tuple[str, str], Callable] = {}
        self._sealed = False

    def register(self, schema_version: SchemaVersion, reducer_version: SchemaVersion, reducer: Callable) -> None:
        if self._sealed:
            raise RuntimeError("reducer registry is sealed")
        key = (schema_version.value, reducer_version.value)
        if key in self._reducers:
            raise ValueError(f"duplicate reducer {key}")
        self._reducers[key] = reducer

    def seal(self) -> None:
        self._sealed = True

    def get(self, schema_version: SchemaVersion, reducer_version: SchemaVersion) -> Callable:
        if not self._sealed:
            raise RuntimeError("reducer registry must be sealed")
        try:
            return self._reducers[(schema_version.value, reducer_version.value)]
        except KeyError:
            raise ValueError("no reducer for requested schema/reducer version") from None


@dataclass(frozen=True)
class RehydrationReport(Generic[State]):
    state: State
    snapshot_id: EntityId | None
    event_count: int
    upcast_count: int
    final_global_position: int
    snapshot_diagnostics: tuple[str, ...] = ()


def rehydrate_aggregate(
    event_store,
    aggregate_id: EntityId,
    initial_state: State,
    reducer: Callable[[State, object], State],
    reader: UpcastingEventReader,
) -> State:
    after = 0
    envelopes = []
    while True:
        page = event_store.read_stream(aggregate_id, after_sequence=after, limit=1000)
        if not page:
            break
        envelopes.extend(reader.read(item).envelope for item in page)
        after = page[-1].envelope.sequence.value
    return replay_events(initial_state, envelopes, reducer)


def rehydrate_run_view(
    event_store,
    snapshot_store: SQLiteSnapshotStore,
    run_id: EntityId,
    initial_state: State,
    reducer: Callable[[State, StoredEvent], State],
    reader: UpcastingEventReader,
    *,
    snapshot_schema_version: SchemaVersion,
    reducer_version: SchemaVersion,
) -> RehydrationReport[State]:
    loaded = snapshot_store.load_latest_compatible(
        run_id,
        snapshot_schema_version=snapshot_schema_version,
        reducer_version=reducer_version,
    )
    snapshot = loaded.snapshot
    state = initial_state if snapshot is None else snapshot.state
    cursor = 0 if snapshot is None else snapshot.last_global_position
    snapshot_id = None if snapshot is None else snapshot.snapshot_id
    count = 0
    upcast_count = 0
    while True:
        page = event_store.read_run(
            run_id, after_global_position=cursor, limit=1000
        )
        if not page:
            break
        for raw in page:
            if raw.run_id != run_id:
                raise EventSequenceError("run replay received an event from another run")
            if raw.global_position <= cursor:
                raise EventSequenceError("run event global position is not increasing")
            current = reader.read(raw)
            if current.envelope.event_version != raw.envelope.event_version:
                upcast_count += 1
            state = reducer(state, current)
            cursor = raw.global_position
            count += 1
    return RehydrationReport(
        state, snapshot_id, count, upcast_count, cursor, loaded.ignored
    )
