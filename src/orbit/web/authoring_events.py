"""`/authoring/events` — the push side of client-written workflow generation.

Claiming is a request the client makes; this is the Runtime telling it there
is something to claim. Without it the client's poll interval *is* the latency,
and an idle Runtime pays for that interval all day for nothing.

A plain WebSocket rather than anything MCP-shaped, deliberately. MCP is
request/response from client to server, and the transport this Runtime serves
it over carries no server-initiated messages at all. A socket is understood by
every App and by tools that only watch a stream, so the notification path
stays open to clients that speak no MCP at the time they need to be woken.

Nothing here is authoritative. Every fact in an event can be re-read from
`claim_authoring_request`, which stays the only way work is actually handed
over — a dropped frame costs latency, never correctness.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Mapping

from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from ..workflow.authoring.external import normalise_client


# Bounded so a client that stops reading cannot grow this process's memory.
# Overflow drops the event: the client can still claim, so what it loses is
# promptness, not work.
MAX_QUEUED_EVENTS = 512
# Close codes in the private range. A client is told *why* it was refused,
# because "the socket closed" is the one diagnosis that fits every cause.
CLOSE_UNAUTHENTICATED = 4401
CLOSE_BAD_CLIENT_NAME = 4400
CLOSE_UNAVAILABLE = 4503


def authoring_event_routes(
    broker: Any,
    *,
    authenticator: Callable[[Any], str | None] | None = None,
    path: str = "/authoring/events",
) -> list[WebSocketRoute]:
    """One socket per connected App, filtered to the work it could take."""

    async def endpoint(websocket: WebSocket) -> None:
        # `authenticator` is typed against Request, but every authenticator
        # this Runtime has reads only what both share — the peer address, the
        # headers — and a WebSocket is the same HTTPConnection underneath.
        actor = None if authenticator is None else authenticator(websocket)
        if actor is not None and not str(actor).strip():
            actor = None
        if actor is None:
            await websocket.close(code=CLOSE_UNAUTHENTICATED)
            return
        if broker is None:
            await websocket.close(code=CLOSE_UNAVAILABLE)
            return
        try:
            client = normalise_client(websocket.query_params.get("client"))
        except ValueError as exc:
            await websocket.close(code=CLOSE_BAD_CLIENT_NAME, reason=str(exc)[:120])
            return
        if client is None:
            await websocket.close(
                code=CLOSE_BAD_CLIENT_NAME,
                reason="a client name is required: /authoring/events?client=<name>",
            )
            return

        await websocket.accept()
        loop = asyncio.get_running_loop()
        events: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUED_EVENTS)

        def deliver(event: Mapping[str, Any]) -> None:
            # Called from the Job's thread. Hopping to the loop is this side's
            # job precisely so the broker never has to know one exists.
            def enqueue() -> None:
                try:
                    events.put_nowait(dict(event))
                except asyncio.QueueFull:
                    pass

            try:
                loop.call_soon_threadsafe(enqueue)
            except RuntimeError:
                # The loop is closing; this socket is going with it.
                pass

        token = broker.subscribe(client, deliver)
        try:
            # Work parked before this socket existed is still work. Sending it
            # on connect is what makes reconnecting a recovery rather than a
            # fresh start with a blind spot.
            await websocket.send_json({
                "type": "ready", "client": client,
                "waiting": [
                    item for item in broker.pending()
                    if item["addressed_to"] in (None, client)
                ],
            })
            # Two things end this loop and only one of them is an event: the
            # other is the client going away, which a send-only socket would
            # not notice until the next event — possibly never.
            incoming = asyncio.ensure_future(websocket.receive())
            outgoing = asyncio.ensure_future(events.get())
            try:
                while True:
                    done, _pending = await asyncio.wait(
                        {incoming, outgoing},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if incoming in done:
                        message = incoming.result()
                        if message.get("type") == "websocket.disconnect":
                            return
                        # Clients have nothing to say here; anything they send
                        # is ignored rather than treated as a command.
                        incoming = asyncio.ensure_future(websocket.receive())
                    if outgoing in done:
                        await websocket.send_json(outgoing.result())
                        outgoing = asyncio.ensure_future(events.get())
            finally:
                for task in (incoming, outgoing):
                    task.cancel()
        except (WebSocketDisconnect, RuntimeError):
            # A disconnect mid-send is how a socket normally ends, not a fault.
            return
        finally:
            broker.unsubscribe(token)

    return [WebSocketRoute(path, endpoint, name="authoring_events")]
