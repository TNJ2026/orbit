from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import time
import unittest

from orbit.web.api_v1 import (
    OPS_READ_SCOPE, OPS_WRITE_SCOPE, READ_SCOPE, WRITE_SCOPE, Authorizer,
)
from orbit.web.app import create_app
from tests.test_web_composition import AsgiHarness


class RuntimeShutdownTests(unittest.TestCase):
    def _app(self, db_path, stopped):
        scopes = {
            "operator": (READ_SCOPE, WRITE_SCOPE, OPS_READ_SCOPE, OPS_WRITE_SCOPE),
            "viewer": (READ_SCOPE,),
        }
        return create_app(
            db_path,
            poll_seconds=0.01,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: scopes.get(actor, ())),
            operator_actors=("operator",),
            shutdown_request=lambda: stopped.append(True),
        )

    def test_capabilities_advertise_shutdown_only_to_operator(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            stopped = []
            with AsgiHarness(self._app(f"{root}/runtime.db", stopped)) as client:
                operator = client.get(
                    "/api/v1/capabilities", actor="operator",
                ).json()["data"]
                viewer = client.get(
                    "/api/v1/capabilities", actor="viewer",
                ).json()["data"]

                command = operator["runtime"]["allowed_commands"][0]
                self.assertEqual("runtime.shutdown", command["command"])
                self.assertEqual("/api/v1/runtime/shutdown", command["href"])
                self.assertEqual([], viewer["runtime"]["allowed_commands"])

    def test_shutdown_is_idempotent_and_runs_after_response(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            stopped = []
            with AsgiHarness(self._app(f"{root}/runtime.db", stopped)) as client:
                response = client.post(
                    "/api/v1/runtime/shutdown",
                    actor="operator",
                    key="stop-runtime-once",
                    body={"expected_version": 0},
                )
                self.assertEqual(200, response.status_code)
                self.assertEqual("stopping", response.json()["data"]["status"])
                self.assertEqual([], stopped)
                client._loop.run_until_complete(asyncio.sleep(0.06))
                self.assertEqual([True], stopped)

    def test_shutdown_requires_command_headers_and_operator_scope(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            stopped = []
            with AsgiHarness(self._app(f"{root}/runtime.db", stopped)) as client:
                missing_key = client.post(
                    "/api/v1/runtime/shutdown",
                    actor="operator",
                    body={"expected_version": 0},
                )
                denied = client.post(
                    "/api/v1/runtime/shutdown",
                    actor="viewer",
                    key="viewer-stop",
                    body={"expected_version": 0},
                )
                self.assertEqual(400, missing_key.status_code)
                self.assertEqual(403, denied.status_code)
                self.assertEqual([], stopped)


if __name__ == "__main__":
    unittest.main()


class ExecutionDoesNotBlockTheServerTests(unittest.TestCase):
    """A run executes inside the request that started it, and always did.

    That is not the problem — waiting for a workflow is a reasonable thing for
    a caller to do. The problem was that it ran *on the event loop*, so while
    an Agent worked this process answered nothing at all: not the UI polling
    for progress, not `/health`, not the socket the run's own events travel
    on. A local Runtime went dark for the length of every run it performed.
    """

    def slow_tool(self, seconds: float):
        from orbit.workflow.handlers.tools import ToolResult

        class Adapter:
            def execute(self, request, context):
                time.sleep(seconds)
                return ToolResult({"value": 1})

            def cancel(self, execution_ref, context):
                return None

            def recover(self, recovery_ref, context):
                return None

        from tests.test_workflow_langgraph_runtime import (
            LangGraphProductionWiringTests,
        )

        return LangGraphProductionWiringTests("run").tool_registration(Adapter())

    def publish_one_slow_node(self, database: Path, registration) -> str:
        import tests.test_workflow_langgraph_runtime as engine_tests
        from orbit.workflow.domain.definitions import (
            CompiledWorkflow, IRHandlerRef, IRNode,
        )
        from orbit.workflow.domain.serialization import definition_hash
        from orbit.workflow.persistence.workflow_versions import (
            SQLiteWorkflowVersionStore,
        )

        manifest = registration.manifest
        node = IRNode(
            "slow", "action",
            (engine_tests.port("value"),), (engine_tests.port("value"),),
            IRHandlerRef(manifest.name, manifest.version, manifest.fingerprint),
            {"tool_name": "example.read", "tool_version": "1.0.0"}, (), None,
        )
        ir = engine_tests.workflow(
            (node, engine_tests.node(
                "done", inputs=("value",), kind="terminal", handler=False,
            )),
            (engine_tests.edge("s_d", "slow", "done"),),
            entry=("slow",), terminals=("done",), result=("slow", "value"),
        )
        SQLiteWorkflowVersionStore(database).publish(
            CompiledWorkflow(
                ir, definition_hash(ir), "test", "sha256:" + "c" * 64,
            ),
            expected_latest_version=0, source_format="json",
            source_text="{}", actor="test", dsl_version="1.3",
        )
        return ir.workflow_id

    def test_the_runtime_answers_while_a_run_is_working(self) -> None:
        import tests.test_workflow_langgraph_runtime as engine_tests

        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        database = root / "runtime.db"
        registration = self.slow_tool(1.5)
        workflow_id = self.publish_one_slow_node(database, registration)
        app = create_app(
            database,
            handlers=[registration],
            schemas={"schema://object/1.0": {"type": "object"},
                     engine_tests.SCHEMA: {"type": "object"}},
            poll_seconds=0.01,
            authenticator=lambda request: "author",
            authorizer=Authorizer(lambda _actor: [READ_SCOPE, WRITE_SCOPE]),
            langgraph_state_directory=root / "langgraph",
        )

        running = {"value": True}

        with AsgiHarness(app) as client:
            async def watch():
                """The longest the loop went without coming back to us.

                Timing one request proves nothing: a blocked loop never
                reaches the coroutine that would time it, so the reading is
                taken after the run is over and looks healthy. What a blocked
                loop cannot hide is the gap between two things it was asked to
                do 50ms apart.
                """

                loop = asyncio.get_running_loop()
                widest, previous, answered = 0.0, loop.time(), None
                while running["value"]:
                    await asyncio.sleep(0.05)
                    now = loop.time()
                    widest = max(widest, now - previous)
                    previous = now
                    if answered is None:
                        answered = await client.arequest("GET", "/health/live")
                        previous = loop.time()
                return widest, answered

            async def scenario():
                # The watcher is ticking before the run begins, so the gap it
                # reports is the run's doing. Started the other way round, a
                # blocking run finishes before the watcher's first tick and
                # the measurement reads zero — the fault invisible, and the
                # test passing for the wrong reason.
                watcher = asyncio.ensure_future(watch())
                await asyncio.sleep(0.15)
                try:
                    return await client.arequest(
                        "POST", "/api/v1/langgraph-runs", actor="author",
                        headers={"idempotency-key": "slow-1"},
                        body={"workflow_id": workflow_id, "input": {"value": 1}},
                    ), watcher
                finally:
                    running["value"] = False

            started, watcher = client.gather(scenario())[0]
            widest, health = client.gather(watcher)[0]


        # Asserted first, because it is the reason for everything below: a
        # blocked loop also fails to issue the health request at all, and
        # "NoneType has no status_code" names the symptom rather than this.
        #
        # The Handler sleeps 1.5s. A loop that ran the run on itself shows up
        # as a single gap about that long; one that offloaded it comes back
        # every tick. The bound is far from both numbers, so this is not a
        # timing test.
        self.assertLess(
            widest, 0.5,
            f"the event loop stalled {widest:.2f}s while a run executed",
        )
        self.assertEqual(200, started.status_code, started.text)
        self.assertEqual("completed", started.json()["data"]["run"]["status"])
        self.assertIsNotNone(health, "no request was served while the run ran")
        self.assertEqual(200, health.status_code)
