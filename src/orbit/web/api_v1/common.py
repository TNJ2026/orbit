"""Shared constants and pure helpers for the `/api/v1` surface.

Everything here is free of request-scoped or service-scoped state: scope
names, the error envelope, authorisation, and small body validators. Route
modules import these; `ApiContext` (context.py) carries the stateful half.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

import anyio

from starlette.responses import JSONResponse, StreamingResponse


READ_SCOPE = "runtime.read"
WRITE_SCOPE = "runtime.write"
# Reads that expose more than run metadata get their own scope so a viewer
# token cannot pull artifact contents or raw planner responses.
SENSITIVE_SCOPE = "runtime.read.sensitive"
OPS_READ_SCOPE = "runtime.ops.read"
OPS_WRITE_SCOPE = "runtime.ops.write"


class ClosingStreamingResponse(StreamingResponse):
    """A stream response that closes its source even on client disconnect."""

    def __init__(self, source, iterator, **kwargs):
        self._source = source
        super().__init__(iterator, **kwargs)

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await anyio.to_thread.run_sync(self._source.close)


def _display_language(body: Mapping[str, Any]) -> str | None:
    """The language step names should be written in, as the caller states it.

    The Agent otherwise infers it from the instruction, so a Chinese UI and an
    English prompt produce English step names. A BCP-47 tag is a label, not an
    instruction: it is length-capped and stored, never executed.
    """

    value = body.get("display_language")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 35:
        raise ValueError("display_language must be a short language tag")
    return value.strip()


def _generation_agent(body: Mapping[str, Any]) -> str | None:
    """The Agent the author picked to write the DSL, by name only.

    A name is the whole of what a caller may contribute: the command behind it
    was fixed at composition from the discovery allowlist. Omitting it keeps
    this Runtime's default Agent.
    """

    agent = body.get("agent")
    if agent is None:
        return None
    if not isinstance(agent, str) or not agent.strip():
        raise ValueError("agent must be a non-empty string")
    return agent.strip()


def _required_version(body: Mapping[str, Any]) -> int:
    """Every write against an existing aggregate carries the version it saw.

    Without it a stale UI tab would silently overwrite a decision someone else
    already made.
    """

    expected = body.get("expected_version")
    if expected is None:
        raise ValueError("expected_version is required")
    return int(expected)


def error(code: str, message: str, status: int = 400, **details: Any) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "details": details}},
        status_code=status,
    )


class Authorizer:
    """Scope check for every request, read and write alike.

    Default-deny: an adapter with no authorizer configured refuses everything
    rather than falling back to "local means trusted".
    """

    def __init__(self, scopes_for: Callable[[str], Sequence[str]] | None = None) -> None:
        self._scopes_for = scopes_for

    def allows(self, actor: str, scope: str) -> bool:
        if self._scopes_for is None:
            return False
        return scope in set(self._scopes_for(actor))


def _retarget_handlers(
    document: Any, available: Mapping[str, str]
) -> list[dict[str, str]]:
    """Move each node's handler to the installed version, in place.

    Returns one record per node actually moved, so the caller can refuse a
    rebind that would change nothing rather than mint an identical version.
    """

    moved: list[dict[str, str]] = []
    nodes = document.get("nodes") if isinstance(document, Mapping) else None
    for node in nodes or ():
        handler = node.get("handler") if isinstance(node, Mapping) else None
        if not isinstance(handler, Mapping):
            continue
        name = handler.get("name")
        target = available.get(name)
        if target is not None and handler.get("version") != target:
            moved.append({
                "node_id": str(node.get("id")),
                "handler_name": str(name),
                "from": str(handler.get("version")),
                "to": str(target),
            })
            handler["version"] = target
    return moved


def authoring_timeout_seconds(operational_config) -> int:
    """How long an authoring job may take, wherever it was started from.

    Lives here rather than inline so the composition root can build one
    AuthoringJobService for every protocol without either of them having to
    restate the deadline. A job started over MCP and one started from the UI
    are the same job.
    """

    if not operational_config:
        return 300
    return int(operational_config.get("authoring_timeout_seconds", 600))
