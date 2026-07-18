#!/usr/bin/env python3
"""Repeatable local baseline for Step 3 persistence; numbers are not an SLA."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import tempfile
import time

from orbit.workflow.domain.envelopes import EventEnvelope
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.persistence import SnapshotRecord
from orbit.workflow.domain.upcasting import UpcasterRegistry
from orbit.workflow.domain.versions import AggregateVersion, DefinitionHash, Revision, SchemaVersion
from orbit.workflow.persistence import (
    EventVersionCatalog, SQLiteEventStore, SQLiteReadSession, SQLiteSnapshotStore,
    SQLiteUnitOfWork, UpcastingEventReader, check_database, rehydrate_run_view,
    snapshot_checksum,
)
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.migrations import migrate_workflow_database


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=10_000)
    parser.add_argument("--runs", type=int, default=10_000)
    args = parser.parse_args()
    if args.events < 100 or args.runs < 1:
        parser.error("events >= 100 and runs >= 1 are required")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "benchmark.db"
        with connect_workflow_database(path) as connection:
            migrate_workflow_database(connection)
            connection.execute(
                "INSERT INTO workflow_definitions VALUES ('workflow:bench', 'Bench', ?, 'benchmark')",
                (NOW.isoformat(),),
            )
            connection.execute(
                "INSERT INTO workflow_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("workflow:bench", 1, "sha256:" + "a" * 64, "1.0", "1.0", "1.0", "{}", "json", None, "sha256:" + "b" * 64, NOW.isoformat(), "benchmark"),
            )
            rows = [
                (f"run:r{i}", "workflow:bench", 1, "sha256:" + "a" * 64, "created", 0, f"run:r{i}", NOW.isoformat(), NOW.isoformat())
                for i in range(args.runs)
            ]
            connection.executemany("INSERT INTO workflow_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            connection.commit()

        run_id = EntityId("run", "r0")
        single_run_id = EntityId("run", "r1")
        single_latencies: list[float] = []
        for number in range(1, 101):
            item = EventEnvelope(
                EntityId("event", f"single{number}"), "run_progressed", Revision(1),
                single_run_id, Revision(number), single_run_id,
                EntityId("command", f"single{number}"), NOW, {"value": number},
            )
            tick = time.perf_counter()
            with SQLiteUnitOfWork(path) as uow:
                uow.events.append(
                    single_run_id, single_run_id, AggregateVersion(number - 1), (item,)
                )
                uow.connection.execute(
                    "UPDATE workflow_runs SET aggregate_version = ? WHERE run_id = ?",
                    (number, str(single_run_id)),
                )
                uow.commit()
            single_latencies.append((time.perf_counter() - tick) * 1000)
        latencies: list[float] = []
        started = time.perf_counter()
        for start in range(0, args.events, 100):
            count = min(100, args.events - start)
            values = tuple(
                EventEnvelope(
                    EntityId("event", f"e{number}"), "run_progressed", Revision(1),
                    run_id, Revision(number), run_id, EntityId("command", f"c{number}"),
                    NOW, {"value": number},
                )
                for number in range(start + 1, start + count + 1)
            )
            tick = time.perf_counter()
            with SQLiteUnitOfWork(path) as uow:
                uow.events.append(run_id, run_id, AggregateVersion(start), values)
                uow.connection.execute(
                    "UPDATE workflow_runs SET aggregate_version = ? WHERE run_id = ?",
                    (start + count, str(run_id)),
                )
                uow.commit()
            latencies.append((time.perf_counter() - tick) * 1000)
        append_seconds = time.perf_counter() - started

        replay_ms = {}
        with SQLiteReadSession(path) as connection:
            store = SQLiteEventStore(connection)
            for size in (100, 1_000, min(10_000, args.events)):
                tick = time.perf_counter()
                stream = store.read_stream(run_id, to_sequence=size, limit=10_000)
                assert len(stream) == size
                assert sum(item.envelope.payload["value"] for item in stream) == size * (size + 1) // 2
                replay_ms[str(size)] = round((time.perf_counter() - tick) * 1000, 3)
            tick = time.perf_counter()
            cursor = 0
            seen = 0
            while True:
                page = store.read_run(run_id, after_global_position=cursor, limit=1000)
                if not page:
                    break
                cursor = page[-1].global_position
                seen += len(page)
            page_read_ms = (time.perf_counter() - tick) * 1000
            assert seen == args.events

        registry = UpcasterRegistry()
        registry.seal()
        event_reader = UpcastingEventReader(
            EventVersionCatalog({"run_progressed": 1}), registry
        )
        snapshot_restore_ms = {}
        snapshot_write_ms = {}
        snapshot_cursors = tuple(
            sorted({max(100, args.events - distance) for distance in (5_000, 1_000, 100)})
        )
        for sequence, cursor in enumerate(snapshot_cursors, 1):
            placeholder = SnapshotRecord(
                EntityId("snapshot", f"bench{sequence}"), run_id, Revision(sequence),
                SchemaVersion("1.0"), SchemaVersion("1.0"), 100 + cursor,
                AggregateVersion(cursor),
                {"count": cursor, "sum": cursor * (cursor + 1) // 2},
                DefinitionHash("sha256:" + "0" * 64), NOW,
            )
            snapshot = replace(placeholder, checksum=snapshot_checksum(placeholder))
            tick = time.perf_counter()
            with SQLiteUnitOfWork(path) as uow:
                uow.snapshots.append(snapshot)
                uow.commit()
            snapshot_write_ms[str(cursor)] = round((time.perf_counter() - tick) * 1000, 3)
            with SQLiteReadSession(path) as connection:
                tick = time.perf_counter()
                report = rehydrate_run_view(
                    SQLiteEventStore(connection), SQLiteSnapshotStore(connection), run_id,
                    {"count": 0, "sum": 0},
                    lambda state, item: {
                        "count": state["count"] + 1,
                        "sum": state["sum"] + item.envelope.payload["value"],
                    },
                    event_reader, snapshot_schema_version=SchemaVersion("1.0"),
                    reducer_version=SchemaVersion("1.0"),
                )
                snapshot_restore_ms[str(cursor)] = round((time.perf_counter() - tick) * 1000, 3)
                assert report.state["count"] == args.events
                assert report.state["sum"] == args.events * (args.events + 1) // 2

        tick = time.perf_counter()
        integrity = check_database(path)
        integrity_ms = (time.perf_counter() - tick) * 1000
        assert integrity.ok, integrity.issues
        result = {
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "sqlite": sqlite3.sqlite_version,
                "cpu_count": os.cpu_count(),
            },
            "scale": {"runs": args.runs, "events": args.events, "batch_size": 100},
            "append": {
                "events_per_second": round(args.events / append_seconds, 1),
                "batch_ms_p50": round(statistics.median(latencies), 3),
                "batch_ms_p95": round(percentile(latencies, .95), 3),
                "batch_ms_p99": round(percentile(latencies, .99), 3),
                "single_ms_p50": round(statistics.median(single_latencies), 3),
                "single_ms_p95": round(percentile(single_latencies, .95), 3),
                "single_ms_p99": round(percentile(single_latencies, .99), 3),
            },
            "replay_ms": replay_ms,
            "run_paged_read_ms": round(page_read_ms, 3),
            "snapshot_write_ms_by_cursor": snapshot_write_ms,
            "snapshot_restore_ms_by_cursor": snapshot_restore_ms,
            "integrity_scan_ms": round(integrity_ms, 3),
            "database_bytes": path.stat().st_size,
            "wal_bytes": Path(str(path) + "-wal").stat().st_size if Path(str(path) + "-wal").exists() else 0,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
