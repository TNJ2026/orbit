"""The resumable Runtime event WebSocket."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from starlette.applications import Starlette

from orbit.web.runtime_events import (
    CLOSE_BAD_SUBSCRIPTION,
    CLOSE_FORBIDDEN,
    CLOSE_UNAUTHENTICATED,
    _cursor,
    runtime_event_routes,
)
from orbit.workflow.domain.envelopes import EventEnvelope
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.versions import AggregateVersion, Revision
from orbit.workflow.persistence import SQLiteUnitOfWork
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.migrations import migrate_workflow_database


NOW = datetime(2026, 7, 29, tzinfo=timezone.utc)
LOOPBACK = ("127.0.0.1", 51234)


def scope(query: str = "") -> dict:
    return {
        "type": "websocket", "asgi": {"version": "3.0"}, "path": "/events",
        "raw_path": b"/events", "query_string": query.encode(), "headers": [],
        "client": LOOPBACK, "server": ("127.0.0.1", 8848), "scheme": "ws",
        "subprotocols": [], "root_path": "",
    }


class Socket:
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


class RuntimeEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "runtime.db"
        with connect_workflow_database(self.path) as connection:
            migrate_workflow_database(connection)
            connection.execute(
                "INSERT INTO workflow_definitions VALUES (?, ?, ?, ?)",
                ("workflow:flow", "Flow", NOW.isoformat(), "test"),
            )
            connection.execute(
                """
                INSERT INTO workflow_versions VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    "workflow:flow", 1, "sha256:" + "a" * 64, "1.0", "1.0",
                    "1.0", "{}", "json", None, "sha256:" + "b" * 64,
                    NOW.isoformat(), "test",
                ),
            )
            connection.execute(
                """
                INSERT INTO workflow_runs (
                    run_id, workflow_id, workflow_version, definition_hash,
                    status, aggregate_version, correlation_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "run:r1", "workflow:flow", 1, "sha256:" + "a" * 64,
                    "created", 0, "run:r1", NOW.isoformat(), NOW.isoformat(),
                ),
            )
            connection.commit()

    def build(self, *, authenticator=lambda _connection: "local"):
        return Starlette(routes=runtime_event_routes(
            self.path, authenticator=authenticator, poll_seconds=0.01,
            heartbeat_seconds=0.05,
        ))

    def append(self, event_type: str, sequence: int) -> None:
        run_id = EntityId("run", "r1")
        event = EventEnvelope(
            EntityId("event", f"e{sequence}"), event_type, Revision(1), run_id,
            Revision(sequence), run_id, EntityId("command", f"c{sequence}"),
            NOW, {"secret": "not sent over the socket"},
        )
        with SQLiteUnitOfWork(self.path) as uow:
            uow.events.append(
                run_id, run_id, AggregateVersion(sequence - 1), (event,)
            )
            uow.commit()

    def run_scenario(self, coroutine) -> None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(coroutine())
        finally:
            loop.close()

    def test_new_connection_starts_at_head_then_receives_new_events(self) -> None:
        self.append("run_started", 1)
        app = self.build()

        async def scenario() -> None:
            async with Socket(app) as socket:
                self.assertEqual("websocket.accept", (await socket.next())["type"])
                ready = await socket.frame()
                self.assertEqual("ready", ready["type"])
                self.assertFalse(ready["replaying"])
                self.append("run_succeeded", 2)
                event = await socket.frame()
                self.assertEqual("runtime_event", event["type"])
                self.assertEqual("run_succeeded", event["event_type"])
                self.assertEqual("run:r1", event["run_id"])
                self.assertNotIn("payload", event)
                checkpoint = await socket.frame()
                self.assertEqual("checkpoint", checkpoint["type"])
                self.assertEqual(event["cursor"], checkpoint["cursor"])

        self.run_scenario(scenario)

    def test_cursor_replays_and_filters_without_stalling_progress(self) -> None:
        self.append("run_started", 1)
        app = self.build()

        async def scenario() -> None:
            query = f"cursor={_cursor(0)}&types=run_succeeded"
            async with Socket(app, query) as socket:
                await socket.next()
                ready = await socket.frame()
                self.assertTrue(ready["replaying"])
                checkpoint = await socket.frame()
                self.assertEqual("checkpoint", checkpoint["type"])
                self.append("run_succeeded", 2)
                event = await socket.frame()
                self.assertEqual("run_succeeded", event["event_type"])

        self.run_scenario(scenario)

    def test_heartbeat_carries_the_resume_cursor(self) -> None:
        app = self.build()

        async def scenario() -> None:
            async with Socket(app) as socket:
                await socket.next()
                ready = await socket.frame()
                heartbeat = await socket.frame()
                self.assertEqual("heartbeat", heartbeat["type"])
                self.assertEqual(ready["cursor"], heartbeat["cursor"])

        self.run_scenario(scenario)

    def test_invalid_cursor_and_missing_identity_are_refused(self) -> None:
        async def scenario() -> None:
            async with Socket(self.build(), "cursor=not-a-cursor") as socket:
                closed = await socket.next()
                self.assertEqual(CLOSE_BAD_SUBSCRIPTION, closed["code"])
            async with Socket(self.build(authenticator=lambda _connection: None)) as socket:
                closed = await socket.next()
                self.assertEqual(CLOSE_UNAUTHENTICATED, closed["code"])

            class Deny:
                def allows(self, _actor, _scope):
                    return False

            app = Starlette(routes=runtime_event_routes(
                self.path, authenticator=lambda _connection: "local", authorizer=Deny(),
            ))
            async with Socket(app) as socket:
                closed = await socket.next()
                self.assertEqual(CLOSE_FORBIDDEN, closed["code"])

        self.run_scenario(scenario)


class CompositionTests(unittest.TestCase):
    def test_runtime_mounts_the_generic_event_socket(self) -> None:
        from orbit.web.app import create_app
        from tests.test_api_v1 import SCHEMAS, transform_registration

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        app = create_app(
            Path(temp.name) / "runtime.db",
            handlers=[transform_registration()], schemas=SCHEMAS,
            worker_count=1, poll_seconds=0.02,
        )
        self.assertIn("/events", [getattr(route, "path", None) for route in app.routes])
