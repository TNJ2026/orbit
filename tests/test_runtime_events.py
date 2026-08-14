"""The resumable Runtime event WebSocket, over the engine's run log.

The socket's shape is unchanged — cursor in, metadata frames out, resume from
where you stopped. What changed is where the events come from: the store it
used to read belonged to the event-sourced engine, and when that engine was
deleted the socket went on accepting connections and delivering nothing.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from starlette.applications import Starlette

from orbit.web.runtime_events import (
    CLOSE_BAD_SUBSCRIPTION,
    _frame,
    CLOSE_FORBIDDEN,
    CLOSE_UNAUTHENTICATED,
    _cursor,
    runtime_event_routes,
)
from orbit.workflow.langgraph_runtime import build_service
from orbit.workflow.langgraph_runtime.service import LangGraphRunConflict

from tests.test_web_composition import (
    publish_human_workflow, publish_linear_workflow, transform_registration,
)


LOOPBACK = ("127.0.0.1", 51234)


def scope(query: str = "") -> dict:
    return {
        "type": "websocket", "asgi": {"version": "3.0"}, "path": "/events",
        "raw_path": b"/events", "query_string": query.encode(), "headers": [],
        "client": LOOPBACK, "server": ("127.0.0.1", 8848), "scheme": "ws",
        "subprotocols": [], "root_path": "",
    }


class Socket:
    """The ASGI app driven directly: no server, no port, no sleep."""

    def __init__(self, app, query: str = "") -> None:
        self.app = app
        self.query = query

    async def __aenter__(self) -> "Socket":
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.outbox: asyncio.Queue = asyncio.Queue()
        await self.inbox.put({"type": "websocket.connect"})
        self.task = asyncio.ensure_future(
            self.app(scope(self.query), self.inbox.get, self.outbox.put)
        )
        return self

    async def __aexit__(self, *exc) -> None:
        await self.inbox.put({"type": "websocket.disconnect", "code": 1000})
        try:
            await asyncio.wait_for(self.task, timeout=5)
        except asyncio.TimeoutError:
            self.task.cancel()

    async def next(self, timeout: float = 5.0) -> dict:
        return await asyncio.wait_for(self.outbox.get(), timeout=timeout)

    async def frame(self, timeout: float = 5.0) -> dict:
        return json.loads((await self.next(timeout=timeout))["text"])

    async def events(self, count: int, timeout: float = 5.0) -> list[dict]:
        """The next `count` runtime events, skipping checkpoints and beats."""

        collected: list[dict] = []
        while len(collected) < count:
            frame = await self.frame(timeout=timeout)
            if frame["type"] == "runtime_event":
                collected.append(frame)
        return collected


class RuntimeEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "runtime.db"
        publish_linear_workflow(self.path)
        publish_human_workflow(self.path)
        self.engine = build_service(
            self.path, [transform_registration()],
            state_directory=Path(self.temp.name) / "langgraph",
        )

    def build(self, *, authenticator=lambda _connection: "local", authorizer=None):
        return Starlette(routes=runtime_event_routes(
            self.engine, authenticator=authenticator, authorizer=authorizer,
            poll_seconds=0.01, heartbeat_seconds=0.05,
        ))

    def start(self, key: str, workflow_id: str = "workflow:linear"):
        return self.engine.start(
            workflow_id, {"value": 1}, idempotency_key=key, actor="local",
        )

    def run_scenario(self, coroutine) -> None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(coroutine())
        finally:
            loop.close()

    # -- the log ---------------------------------------------------------

    def test_a_run_records_every_state_it_passed_through(self) -> None:
        self.start("linear-1")
        recorded = [
            (item["event_type"], item["revision"])
            for item in self.engine.events_after(0)
        ]
        self.assertEqual(
            [("langgraph_run.running", 0), ("langgraph_run.completed", 1)],
            recorded,
        )

    def test_a_change_that_did_not_happen_leaves_no_event(self) -> None:
        """The append runs after the guard that decides the change took.

        It is also inside the same transaction, so a process that stops
        between the write and the commit takes both with it. What this pins is
        the half a test can reach: an event for a change the engine refused
        would send a consumer to re-read state that was never written.
        """

        run = self.start("human-1", "workflow:human")
        self.assertEqual("interrupted", run.status)
        before = self.engine.events_head()
        with self.assertRaises(LangGraphRunConflict):
            self.engine.cancel(
                run.run_id, expected_revision=run.revision + 99,
                idempotency_key="stale-cancel", actor="local",
            )
        self.assertEqual(before, self.engine.events_head())

    def test_positions_are_dense_and_never_reused(self) -> None:
        self.start("linear-1")
        self.start("linear-2")
        positions = [item["position"] for item in self.engine.events_after(0)]
        self.assertEqual(list(range(1, len(positions) + 1)), positions)

    def test_events_after_bounds_its_page(self) -> None:
        self.start("linear-1")
        self.start("linear-2")
        self.assertEqual(1, len(self.engine.events_after(0, limit=1)))
        with self.assertRaises(ValueError):
            self.engine.events_after(0, limit=0)

    # -- the socket ------------------------------------------------------

    def test_new_connection_starts_at_head_then_receives_new_events(self) -> None:
        self.start("before-connect")
        app = self.build()

        async def scenario() -> None:
            async with Socket(app) as socket:
                await socket.next()  # accept
                ready = await socket.frame()
                self.assertEqual("ready", ready["type"])
                self.assertFalse(ready["replaying"])
                # Nothing from before the subscription: a new subscriber is
                # told where the stream is, not replayed the whole history.
                self.start("after-connect")
                events = await socket.events(2)
                self.assertEqual(
                    ["langgraph_run.running", "langgraph_run.completed"],
                    [item["event_type"] for item in events],
                )
                self.assertEqual(2, events[0]["schema_version"])

        self.run_scenario(scenario)

    def test_a_cursor_replays_exactly_what_was_missed(self) -> None:
        self.start("first")
        app = self.build()
        missed = self.engine.events_head()
        self.start("while-away")

        async def scenario() -> None:
            async with Socket(app, f"cursor={_cursor(missed)}") as socket:
                await socket.next()
                ready = await socket.frame()
                self.assertTrue(ready["replaying"])
                events = await socket.events(2)
                self.assertEqual(
                    [missed + 1, missed + 2],
                    [_position_of(item["cursor"]) for item in events],
                )
                self.assertEqual(
                    ["langgraph_run.running", "langgraph_run.completed"],
                    [item["event_type"] for item in events],
                )

        self.run_scenario(scenario)

    def test_filters_narrow_the_stream_without_stalling_the_cursor(self) -> None:
        app = self.build()

        async def scenario() -> None:
            async with Socket(
                app, "types=langgraph_run.completed"
            ) as socket:
                await socket.next()
                await socket.frame()
                self.start("filtered")
                events = await socket.events(1)
                self.assertEqual(
                    "langgraph_run.completed", events[0]["event_type"]
                )
                # The filtered-out event still advanced the cursor, so a
                # resume does not deliver it again.
                self.assertEqual(
                    self.engine.events_head(),
                    _position_of(events[0]["cursor"]),
                )

        self.run_scenario(scenario)

    def test_a_run_filter_only_accepts_an_engine_run_id(self) -> None:
        app = self.build()

        async def scenario() -> None:
            async with Socket(app, "run_id=run:legacy") as socket:
                closed = await socket.next()
                self.assertEqual(CLOSE_BAD_SUBSCRIPTION, closed["code"])

        self.run_scenario(scenario)

    def test_heartbeat_carries_the_resume_cursor(self) -> None:
        app = self.build()

        async def scenario() -> None:
            async with Socket(app) as socket:
                await socket.next()
                await socket.frame()
                beat = await socket.frame()
                self.assertEqual("heartbeat", beat["type"])
                self.assertIn("cursor", beat)

        self.run_scenario(scenario)

    def test_invalid_cursor_and_missing_identity_are_refused(self) -> None:
        async def refused(app, query, expected) -> None:
            async with Socket(app, query) as socket:
                closed = await socket.next()
                self.assertEqual(expected, closed["code"])

        class Deny:
            @staticmethod
            def allows(_actor, _scope) -> bool:
                return False

        async def scenario() -> None:
            await refused(self.build(), "cursor=not-a-cursor", CLOSE_BAD_SUBSCRIPTION)
            await refused(
                self.build(authenticator=lambda _connection: None),
                "", CLOSE_UNAUTHENTICATED,
            )
            await refused(self.build(authorizer=Deny()), "", CLOSE_FORBIDDEN)

        self.run_scenario(scenario)


def _position_of(cursor: str) -> int:
    from orbit.workflow.api.dto import decode_cursor

    return int(decode_cursor(cursor)["position"])


class CompositionTests(unittest.TestCase):
    def test_the_socket_is_mounted_with_the_engine_behind_it(self) -> None:
        from orbit.web.app import create_app
        from tests.test_web_composition import SCHEMAS

        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        app = create_app(
            root / "runtime.db",
            handlers=[transform_registration()], schemas=SCHEMAS,
            poll_seconds=0.02,
            langgraph_state_directory=root / "langgraph",
        )
        self.assertIn(
            "/events", [getattr(route, "path", None) for route in app.routes]
        )

    def test_without_an_engine_the_socket_is_absent_rather_than_silent(self) -> None:
        """A socket that connects and never delivers cannot be told from a
        quiet Runtime, which is how it went unnoticed that nothing wrote to
        it. With no engine there is nothing to stream, so there is no route.
        """

        from orbit.web.app import create_app
        from tests.test_web_composition import SCHEMAS

        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        app = create_app(
            Path(temp.name) / "runtime.db",
            handlers=[transform_registration()], schemas=SCHEMAS,
            poll_seconds=0.02,
        )
        self.assertNotIn(
            "/events", [getattr(route, "path", None) for route in app.routes]
        )


class EventIdentityTests(unittest.TestCase):
    """Why the id is the position and not the run and its revision."""

    def test_two_changes_at_one_revision_keep_distinct_ids(self) -> None:
        """A resume that answers the last interrupt does not bump the revision.

        The run goes `interrupted@N` then `running@N`. An id built from the
        run and its revision would be the same string for both, and the Agent
        App inbox dedupes on it with `INSERT OR IGNORE` — so the second would
        be dropped and nobody would learn the run had resumed.
        """

        from tests.test_workflow_langgraph_runtime import (
            LangGraphHandlerRegistry, binding, edge, node, workflow,
        )
        from orbit.workflow.domain.definitions import IRPolicy
        from orbit.workflow.domain.serialization import definition_hash
        from orbit.workflow.persistence.workflow_versions import (
            SQLiteWorkflowVersionStore,
        )
        from orbit.workflow.domain.definitions import CompiledWorkflow
        from orbit.workflow.langgraph_runtime.service import (
            LangGraphWorkflowService,
        )

        fan = node("fan", inputs=("value",), outputs=("value",), route_mode="parallel")
        left = node(
            "left", inputs=("value",), outputs=("value",), kind="human", handler=False,
        )
        right = node(
            "right", inputs=("value",), outputs=("value",), kind="human", handler=False,
        )
        ir = workflow(
            (fan, left, right),
            (edge("to_left", "fan", "left"), edge("to_right", "fan", "right")),
            entry=("fan",), terminals=("left", "right"),
            result=("left", "value"),
            policies=(IRPolicy(
                "complete", "completion", {"required_terminal_count": 2},
            ),),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteWorkflowVersionStore(root / "workflows.db")
            store.publish(
                CompiledWorkflow(
                    ir, definition_hash(ir), "test", "sha256:" + "c" * 64,
                ),
                expected_latest_version=0, source_format="json",
                source_text="{}", actor="test:author", dsl_version="1.3",
            )
            engine = LangGraphWorkflowService(
                store,
                LangGraphHandlerRegistry([
                    binding("fan", lambda values, config, context: dict(values)),
                ]),
                run_db_path=root / "runs.sqlite3",
                checkpoint_db_path=root / "checkpoints.sqlite3",
            )
            started = engine.start(
                ir.workflow_id, {"value": "start"},
                idempotency_key="parallel-start", actor="local",
            )
            self.assertEqual(2, len(started.interrupts))
            first = engine.resume(
                started.run_id, "left answer",
                expected_revision=started.revision,
                idempotency_key="answer-left",
                interrupt_id=started.interrupts[0]["id"], actor="local",
            )
            engine.resume(
                started.run_id, "right answer",
                expected_revision=first.revision,
                idempotency_key="answer-right",
                interrupt_id=started.interrupts[1]["id"], actor="local",
            )
            events = engine.events_after(0)
            # Through the real frame builder, so a change of mind about what
            # `event_id` is made of is caught here.
            frames = [_frame(item) for item in events]
            identities = [item["event_id"] for item in frames]
            self.assertEqual(len(identities), len(set(identities)))
            # The case the id has to survive, and the reason the run and its
            # revision are not enough: one revision, two changes.
            pairs = [(item["run_id"], item["sequence"]) for item in frames]
            self.assertGreater(len(pairs), len(set(pairs)))


if __name__ == "__main__":
    unittest.main()
