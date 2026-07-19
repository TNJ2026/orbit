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


def loopback_authenticator(request: Request) -> str | None:
    client = request.client
    if client is None or client.host not in LOOPBACK_HOSTS:
        return None
    return LOCAL_ACTOR


def local_authorizer() -> Authorizer:
    def scopes_for(actor: str) -> Sequence[str]:
        return LOCAL_SCOPES if actor == LOCAL_ACTOR else ()

    return Authorizer(scopes_for)
