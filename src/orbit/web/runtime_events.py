"""Durable Runtime event notifications over WebSocket.

The socket carries event metadata, not authoritative state or command URLs.
Clients resume with the opaque cursor in each frame, then re-read the relevant
HTTP or MCP resource before acting. This keeps authorization and
``allowed_commands`` in the application services that already own them.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable

from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from ..workflow.api.dto import CursorError, decode_cursor, encode_cursor
from ..workflow.persistence.database import connect_workflow_database
from ..workflow.persistence.event_store import SQLiteEventStore


CLOSE_BAD_SUBSCRIPTION = 4400
CLOSE_UNAUTHENTICATED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_UNAVAILABLE = 4503
READ_SCOPE = "runtime.read"
CURSOR_KIND = "runtime-events-v1"
DEFAULT_POLL_SECONDS = 0.25
DEFAULT_HEARTBEAT_SECONDS = 15.0
MAX_EVENT_TYPES = 64
MAX_BATCH_SIZE = 200
_EVENT_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")


def _cursor(position: int) -> str:
    return encode_cursor({"kind": CURSOR_KIND, "position": position})


def _position(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    value = decode_cursor(raw)
    if set(value) != {"kind", "position"} or value.get("kind") != CURSOR_KIND:
        raise CursorError("cursor is not a Runtime event cursor")
    position = value.get("position")
    if isinstance(position, bool) or not isinstance(position, int) or position < 0:
        raise CursorError("cursor is not a Runtime event cursor")
    return position


def _event_types(values: Iterable[str]) -> frozenset[str]:
    result: list[str] = []
    for value in values:
        result.extend(item.strip() for item in value.split(",") if item.strip())
    if len(result) > MAX_EVENT_TYPES or any(
        _EVENT_TYPE.fullmatch(item) is None for item in result
    ):
        raise ValueError("event types must be valid names and no more than 64")
    return frozenset(result)


class RuntimeEventReader:
    """Short-lived read connections make polling safe across worker threads."""

    def __init__(self, db_path: Path | str) -> None:
        self.path = Path(db_path)

    def head(self) -> int:
        with connect_workflow_database(self.path, read_only=True) as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(global_position), 0) FROM run_events"
            ).fetchone()
            return int(row[0])

    def after(self, position: int, *, limit: int = MAX_BATCH_SIZE):
        with connect_workflow_database(self.path, read_only=True) as connection:
            return SQLiteEventStore(connection).read_all(
                after_global_position=position, limit=limit
            )


def _frame(stored) -> dict[str, Any]:
    event = stored.envelope
    return {
        "type": "runtime_event",
        "schema_version": 1,
        "event_id": str(event.event_id),
        "event_type": event.event_type,
        "run_id": str(stored.run_id),
        "aggregate_id": str(event.aggregate_id),
        "sequence": event.sequence.value,
        "occurred_at": event.occurred_at.isoformat(),
        "correlation_id": str(event.correlation_id),
        "causation_id": str(event.causation_id),
        "cursor": _cursor(stored.global_position),
    }


def runtime_event_routes(
    db_path: Path | str,
    *,
    authenticator: Callable[[Any], str | None] | None = None,
    authorizer: Any = None,
    path: str = "/events",
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
) -> list[WebSocketRoute]:
    """Expose resumable metadata notifications for durable run events."""

    reader = RuntimeEventReader(db_path)

    async def endpoint(websocket: WebSocket) -> None:
        actor = None if authenticator is None else authenticator(websocket)
        if actor is not None and not str(actor).strip():
            actor = None
        if actor is None:
            await websocket.close(code=CLOSE_UNAUTHENTICATED)
            return
        if authorizer is not None and not authorizer.allows(actor, READ_SCOPE):
            await websocket.close(code=CLOSE_FORBIDDEN)
            return
        try:
            requested_position = _position(websocket.query_params.get("cursor"))
            selected_types = _event_types(websocket.query_params.getlist("types"))
            run_id = websocket.query_params.get("run_id")
            if run_id is not None:
                run_id = run_id.strip()
                if not run_id.startswith("run:") or len(run_id) > 256:
                    raise ValueError("run_id must be a Runtime run id")
            head = await asyncio.to_thread(reader.head)
            if requested_position is not None and requested_position > head:
                raise ValueError("cursor is beyond the Runtime event stream")
        except (CursorError, ValueError) as exc:
            await websocket.close(code=CLOSE_BAD_SUBSCRIPTION, reason=str(exc)[:120])
            return
        except Exception:
            await websocket.close(
                code=CLOSE_UNAVAILABLE, reason="Runtime event store is unavailable"
            )
            return

        position = head if requested_position is None else requested_position
        await websocket.accept()
        await websocket.send_json({
            "type": "ready",
            "schema_version": 1,
            "cursor": _cursor(position),
            "replaying": requested_position is not None and position < head,
            "filters": {
                "event_types": sorted(selected_types),
                "run_id": run_id,
            },
        })

        incoming = asyncio.ensure_future(websocket.receive())
        last_sent = time.monotonic()
        try:
            while True:
                records = await asyncio.to_thread(reader.after, position)
                if records:
                    for stored in records:
                        position = stored.global_position
                        if selected_types and stored.envelope.event_type not in selected_types:
                            continue
                        if run_id is not None and str(stored.run_id) != run_id:
                            continue
                        await websocket.send_json(_frame(stored))
                        last_sent = time.monotonic()
                    await websocket.send_json({
                        "type": "checkpoint",
                        "schema_version": 1,
                        "cursor": _cursor(position),
                    })
                    last_sent = time.monotonic()
                    continue

                elapsed = time.monotonic() - last_sent
                if elapsed >= heartbeat_seconds:
                    await websocket.send_json({
                        "type": "heartbeat",
                        "schema_version": 1,
                        "cursor": _cursor(position),
                    })
                    last_sent = time.monotonic()

                done, _ = await asyncio.wait(
                    {incoming}, timeout=poll_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if incoming in done:
                    message = incoming.result()
                    if message.get("type") == "websocket.disconnect":
                        return
                    incoming = asyncio.ensure_future(websocket.receive())
        except (WebSocketDisconnect, RuntimeError):
            return
        finally:
            incoming.cancel()

    return [WebSocketRoute(path, endpoint, name="runtime_events")]
