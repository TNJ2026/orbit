"""Identity for a locally served, single-operator Runtime.

`orbit serve` binds to loopback and serves one person: the one at the keyboard.
That person is `local`, and they hold every scope. This module exists so that
assumption is stated in one reviewable place instead of being spread through
the adapters as "no authenticator means trusted".

The check is on the connection, not on a header: a request that did not arrive
over loopback gets no identity at all, so exposing the port — deliberately or
by a misconfigured proxy — yields 401s rather than an open runtime.
"""

from __future__ import annotations

from typing import Sequence

from starlette.requests import Request

from .api_v1 import (
    OPS_READ_SCOPE, OPS_WRITE_SCOPE, READ_SCOPE, SENSITIVE_SCOPE, WRITE_SCOPE,
    Authorizer,
)


LOCAL_ACTOR = "local"
LOCAL_SCOPES: tuple[str, ...] = (
    READ_SCOPE, WRITE_SCOPE, SENSITIVE_SCOPE, OPS_READ_SCOPE, OPS_WRITE_SCOPE,
)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
SCOPED_ACTOR_HEADER = "x-orbit-actor"
# Where a Session-scoped actor may arrive. `/mcp` is the MCP transport served
# directly; `/internal/v1/agent-tools` is the same tool backend reached through
# the Hub, which forwards the header it was handed. They are one surface with
# two doors, and `orbit serve` under a Hub mounts only the second — so naming
# just `/mcp` here meant the header was never read at all. Nothing else is on
# this list: the browser UI and `/api/v1` are the one loopback operator.
SCOPED_ACTOR_PATHS = frozenset({"/mcp", "/internal/v1/agent-tools"})


def loopback_authenticator(request: Request) -> str | None:
    client = request.client
    if client is None or client.host not in LOOPBACK_HOSTS:
        return None
    return LOCAL_ACTOR


def loopback_scoped_mcp_authenticator(
    request: Request, *, trusted_prefix: str,
) -> str | None:
    """Resolve a per-client MCP actor without trusting remote connections.

    Orbit's local deployment already treats every loopback process as the one
    operator.  The header refines that operator into Session slots; it grants
    no scopes a loopback caller did not already have.  It is accepted only on
    the MCP doors in `SCOPED_ACTOR_PATHS`, only under the configured prefix,
    and with the same bounded actor alphabet as the stdio transport.
    """

    actor = loopback_authenticator(request)
    if actor is None or request.url.path not in SCOPED_ACTOR_PATHS:
        return actor
    candidate = request.headers.get(SCOPED_ACTOR_HEADER)
    if candidate is None:
        return actor
    if (
        not candidate.startswith(trusted_prefix)
        or len(candidate) <= len(trusted_prefix)
        or len(candidate) > 200
        or not candidate.strip()
        or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789:_-" for character in candidate)
    ):
        return None
    return candidate


def local_authorizer(
    trusted_actor: str = LOCAL_ACTOR, *, trusted_prefix: str | None = None,
) -> Authorizer:
    if not trusted_actor.strip():
        raise ValueError("trusted actor cannot be empty")

    def scopes_for(actor: str) -> Sequence[str]:
        trusted = actor == trusted_actor or (
            trusted_prefix is not None and actor.startswith(trusted_prefix)
        )
        return LOCAL_SCOPES if trusted else ()

    return Authorizer(scopes_for)
