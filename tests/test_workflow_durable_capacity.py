from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
import unittest

from orbit.workflow.domain.durable_execution import (
    DurableTimerRecord, ExecutionSafety, JobRecord, TimerPurpose,
)
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.states import JobStatus, TimerStatus
from orbit.workflow.domain.versions import AggregateVersion, SchemaVersion
from orbit.workflow.persistence.memory import MemoryRuntimeDatabase


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


class DurableCapacityTests(unittest.TestCase):
    def test_ten_thousand_jobs_and_timers_use_bounded_scan_pages(self):
        database = MemoryRuntimeDatabase()
        run_id = EntityId("run", "capacity")
        started = time.monotonic()
        for index in range(10_000):
            job_id = EntityId("job", f"j{index}")
            database.jobs.create(JobRecord(
                job_id, run_id, EntityId("node_run", f"n{index}"), None,
                "node_execution", ExecutionSafety.REPLAY_SAFE, JobStatus.READY,
                index % 5, NOW, 0, 3, AggregateVersion(0), NOW, NOW,
            ))
            database.timers.create(DurableTimerRecord(
                EntityId("timer", f"t{index}"), run_id, TimerPurpose.JOB_BACKOFF,
                f"backoff-{index}", "job", job_id, SchemaVersion("1.0"), {},
                TimerStatus.SCHEDULED, NOW + timedelta(microseconds=index),
                None, None, None, 0, None, AggregateVersion(0), NOW, NOW,
            ))
        jobs = database.jobs.list_claimable(NOW + timedelta(seconds=1), limit=100)
        timers = database.timers.list_due(NOW + timedelta(seconds=1), limit=100)
        elapsed = time.monotonic() - started
        self.assertEqual(100, len(jobs))
        self.assertEqual(100, len(timers))
        self.assertLess(elapsed, 5.0)


if __name__ == "__main__": unittest.main()
