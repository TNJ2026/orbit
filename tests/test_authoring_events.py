"""`/authoring/events` — the socket that removes the poll interval.

Driven at the ASGI level rather than through an HTTP client, like every other
web test here. What is worth pinning is the part a client depends on: it is
told about work it could take, it is told about work parked before it arrived,
and it is refused in a way that says why.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest

from starlette.applications import Starlette

from orbit.web.authoring_events import (
    CLOSE_BAD_CLIENT_NAME, CLOSE_UNAUTHENTICATED, authoring_event_routes,
)
from orbit.workflow.authoring import ExternalAuthoringBroker


LOOPBACK = ("127.0.0.1", 51234)


def scope(query: str = "client=cursor") -> dict:
    return {
        "type": "websocket", "asgi": {"version": "3.0"}, "path": "/authoring/events",
        "raw_path": b"/authoring/events", "query_string": query.encode(),
        "headers": [], "client": LOOPBACK, "server": ("127.0.0.1", 8848),
        "scheme": "ws", "subprotocols": [], "root_path": "",
    }


class Socket:
    """One ASGI websocket connection, driven from a test."""

    def __init__(self, app, query: str = "client=cursor") -> None:
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
        """The next message, as the object the client would decode."""

        return json.loads((await self.next(timeout=timeout))["text"])


class AuthoringEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = ExternalAuthoringBroker()

    def build(self, *, authenticator=lambda _connection: "local"):
        return Starlette(
            routes=authoring_event_routes(self.broker, authenticator=authenticator)
        )

    def park(self, target: str | None = "cursor"):
        """Park a prompt from another thread, as a Job's own thread would."""

        generate = (
            self.broker if target is None else self.broker.generator_for(target)
        )
        thread = threading.Thread(target=lambda: generate("write me a workflow"), daemon=True)
        thread.start()
        deadline = time.monotonic() + 10.0
        while not self.broker.pending() and time.monotonic() < deadline:
            time.sleep(0.01)
        return thread

    def answer(self, client: str = "cursor") -> None:
        request = self.broker.claim(actor="local", client=client)
        if request is not None:
            self.broker.respond(request["request_id"], "{}", actor=client)

    def run_scenario(self, coroutine) -> None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(coroutine())
        finally:
            loop.close()

    def test_a_connected_app_is_told_when_work_is_parked(self) -> None:
        app = self.build()
        parked: list = []

        async def scenario() -> None:
            async with Socket(app) as socket:
                self.assertEqual("websocket.accept", (await socket.next())["type"])
                ready = await socket.frame()
                self.assertEqual("ready", ready["type"])
                self.assertEqual("cursor", ready["client"])
                self.assertEqual([], ready["waiting"])

                # Parking happens on a Job's thread; the event has to cross
                # into the loop this socket lives on.
                parked.append(
                    await asyncio.get_running_loop().run_in_executor(None, self.park)
                )
                message = await socket.frame()
                self.assertEqual("request_parked", message["type"])
                self.assertEqual("cursor", message["addressed_to"])
                self.assertEqual(
                    self.broker.pending()[0]["request_id"], message["request_id"],
                )

        self.run_scenario(scenario)
        self.answer()
        parked[0].join(timeout=10.0)

    def test_work_parked_before_the_socket_existed_is_not_a_blind_spot(self) -> None:
        thread = self.park()
        app = self.build()

        async def scenario() -> None:
            async with Socket(app) as socket:
                await socket.next()
                ready = await socket.frame()
                # Reconnecting is recovery, not a fresh start: the request
                # that was already waiting arrives with the greeting.
                self.assertEqual("ready", ready["type"])
                self.assertEqual(
                    [self.broker.pending()[0]["request_id"]],
                    [item["request_id"] for item in ready["waiting"]],
                )

        self.run_scenario(scenario)
        self.answer()
        thread.join(timeout=10.0)

    def test_a_socket_is_only_told_about_work_it_could_take(self) -> None:
        app = self.build()
        parked: list = []

        async def scenario() -> None:
            async with Socket(app, "client=zed") as socket:
                await socket.next()
                await socket.next()
                parked.append(
                    await asyncio.get_running_loop().run_in_executor(None, self.park)
                )
                # Addressed to cursor, so zed hears nothing at all.
                with self.assertRaises(asyncio.TimeoutError):
                    await socket.next(timeout=0.5)

        self.run_scenario(scenario)
        self.answer()
        parked[0].join(timeout=10.0)

    def test_a_caller_without_an_identity_is_refused(self) -> None:
        app = self.build(authenticator=lambda _connection: None)

        async def scenario() -> None:
            async with Socket(app) as socket:
                closed = await socket.next()
                self.assertEqual("websocket.close", closed["type"])
                self.assertEqual(CLOSE_UNAUTHENTICATED, closed["code"])

        self.run_scenario(scenario)

    def test_a_socket_that_names_nothing_is_told_what_it_is_missing(self) -> None:
        app = self.build()

        async def scenario() -> None:
            async with Socket(app, "") as socket:
                closed = await socket.next()
                self.assertEqual("websocket.close", closed["type"])
                self.assertEqual(CLOSE_BAD_CLIENT_NAME, closed["code"])
                self.assertIn("client name is required", closed["reason"])

        self.run_scenario(scenario)

    def test_a_name_that_could_not_be_an_address_is_refused(self) -> None:
        app = self.build()

        async def scenario() -> None:
            async with Socket(app, "client=has%20space") as socket:
                closed = await socket.next()
                self.assertEqual(CLOSE_BAD_CLIENT_NAME, closed["code"])

        self.run_scenario(scenario)

    def test_a_closed_socket_stops_counting_as_a_connected_app(self) -> None:
        app = self.build()

        async def scenario() -> None:
            async with Socket(app) as socket:
                await socket.next()
                await socket.next()
                self.assertEqual(["cursor"], self.broker.clients())

        self.run_scenario(scenario)
        # The App went away, so it stops being somewhere an author can send
        # work — without waiting for any timeout to notice.
        self.assertEqual([], self.broker.clients())


class CompositionTests(unittest.TestCase):
    def test_the_runtime_serves_the_socket_beside_its_other_routes(self) -> None:
        import tempfile
        from pathlib import Path

        from orbit.web.app import create_app
        from tests.test_api_v1 import SCHEMAS, transform_registration

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        broker = ExternalAuthoringBroker()
        app = create_app(
            Path(temp.name) / "runtime.db",
            handlers=[transform_registration()], schemas=SCHEMAS,
            worker_count=1, poll_seconds=0.02,
            workflow_generators=broker.generators(),
            workflow_generator=broker,
            authoring_broker=broker,
        )
        self.assertIn(
            "/authoring/events", [getattr(r, "path", None) for r in app.routes],
        )


if __name__ == "__main__":
    unittest.main()
