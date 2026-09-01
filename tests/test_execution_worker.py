from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from orbit.web.builtin_handlers import builtin_handlers
from orbit.workflow.langgraph_runtime.compiler import (
    LangGraphExecutionContext, LangGraphUnknownExternalResult,
)
from orbit.workflow.langgraph_runtime.execution_worker import (
    start_execution_worker, start_execution_worker_pool,
)


class ExecutionWorkerTests(unittest.TestCase):
    def test_pool_distributes_distinct_attempts_across_workers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry, pool = start_execution_worker_pool(
                builtin_handlers(), state_directory=Path(temporary), worker_count=2,
            )
            self.addCleanup(pool.stop)
            handler = registry._entries["transform"]  # noqa: SLF001
            for index in (1, 2):
                handler.invoke(
                    {"index": index}, {},
                    LangGraphExecutionContext(
                        "workflow:test", "transform", "langgraph_run:test",
                        f"langgraph_attempt:test:transform:{index}",
                    ),
                )

            self.assertEqual(2, len(set(pool.pids)))
            self.assertEqual(
                2,
                len({worker.pid for worker in pool._attempt_workers.values()}),  # noqa: SLF001
            )

    def test_handler_execution_runs_in_a_separate_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry, worker = start_execution_worker(
                builtin_handlers(), state_directory=Path(temporary),
            )
            self.addCleanup(worker.stop)
            handler = registry._entries["transform"]  # noqa: SLF001

            output = handler.invoke(
                {"answer": 42}, {"operation": "identity"},
                LangGraphExecutionContext(
                    "workflow:test", "transform", "langgraph_run:test",
                    "langgraph_attempt:test:transform:1",
                ),
            )

            self.assertEqual({"answer": 42}, output)
            self.assertNotEqual(os.getpid(), worker.pid)
            self.assertTrue(worker.alive)

    def test_worker_loss_is_reported_as_an_unknown_external_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry, worker = start_execution_worker(
                builtin_handlers(), state_directory=Path(temporary),
            )
            handler = registry._entries["transform"]  # noqa: SLF001
            self.assertTrue(worker.stop())

            with self.assertRaisesRegex(
                LangGraphUnknownExternalResult, "Worker is unavailable",
            ):
                handler.invoke(
                    {"answer": 42}, {},
                    LangGraphExecutionContext(
                        "workflow:test", "transform", "langgraph_run:test",
                        "langgraph_attempt:test:transform:1",
                    ),
                )


if __name__ == "__main__":
    unittest.main()
