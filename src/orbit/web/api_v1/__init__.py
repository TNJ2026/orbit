"""`/api/v1` — the only HTTP surface that may change Runtime state.

Reads are paged and versioned; writes go through one command boundary that
enforces authentication, authorisation, an idempotency key and an expected
version. Actions are advertised through `allowed_commands` rather than being
inferred by the client, so the server stays the only authority on what an
actor may do.

The surface is split by domain: `common` holds scope constants and pure
helpers, `context` owns the services and the cross-cutting request helpers
(`ApiContext`), and each route module projects one domain onto HTTP.
`build_api_v1` is the assembly point and keeps the historical signature.
"""

from __future__ import annotations

from starlette.routing import Route

from ...workflow.api.artifact_read_models import PREVIEW_LIMIT_BYTES

from . import artifacts, drafts, human, ops, plans, recovery, runs, workflows
from .common import (
    OPS_READ_SCOPE, OPS_WRITE_SCOPE, READ_SCOPE, SENSITIVE_SCOPE, WRITE_SCOPE,
    Authorizer, authoring_timeout_seconds, error,
)
from .context import ApiContext

__all__ = [
    "OPS_READ_SCOPE", "OPS_WRITE_SCOPE", "READ_SCOPE", "SENSITIVE_SCOPE",
    "WRITE_SCOPE", "PREVIEW_LIMIT_BYTES",
    "ApiContext", "Authorizer", "authoring_timeout_seconds", "build_api_v1",
    "error",
]


def build_api_v1(db_path, durable_service, **kwargs) -> list[Route]:
    """Routes for `/api/v1`, ready to mount on the composition root.

    Keyword arguments are forwarded verbatim to `ApiContext` — see
    `context.ApiContext.__init__` for the full list.
    """

    ctx = ApiContext(db_path, durable_service, **kwargs)
    return [
        *runs.build_routes(ctx),
        *plans.build_routes(ctx),
        *artifacts.build_routes(ctx),
        *human.build_routes(ctx),
        *recovery.build_routes(ctx),
        *ops.build_routes(ctx),
        *workflows.build_routes(ctx),
        *drafts.build_routes(ctx),
    ]
