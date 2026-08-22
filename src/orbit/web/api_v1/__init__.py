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

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from starlette.requests import Request
from starlette.routing import Route

from ...workflow.api.routes import RateLimiter

from . import drafts, langgraph_runs, ops, workflows
from .common import (
    OPS_READ_SCOPE, OPS_WRITE_SCOPE, READ_SCOPE, SENSITIVE_SCOPE, WRITE_SCOPE,
    Authorizer, authoring_timeout_seconds, error,
)
from .context import ApiContext

__all__ = [
    "OPS_READ_SCOPE", "OPS_WRITE_SCOPE", "READ_SCOPE", "SENSITIVE_SCOPE",
    "WRITE_SCOPE",
    "ApiContext", "Authorizer", "authoring_timeout_seconds", "build_api_v1",
    "error",
]


def build_api_v1(
    db_path: Path | str,
    *,
    execution_registry=None,
    workflow_db_path: Path | str | None = None,
    authenticator: Callable[[Request], str | None] | None = None,
    authorizer: Authorizer | None = None,
    rate_limiter: RateLimiter | None = None,
    unlimited_actors: Sequence[str] = (),
    token_exempt_actors: Sequence[str] = (),
    operator_actors: Sequence[str] = (),
    audit: Callable[[str, str, Mapping[str, Any]], None] | None = None,
    fault_hook: Callable[[str], None] | None = None,
    clock: Callable[[], datetime] | None = None,
    agent_catalog: Sequence[Mapping[str, Any]] = (),
    capabilities: Mapping[str, Mapping[str, Any]] | None = None,
    schema_catalog=None,
    artifact_backend=None,
    operational_config: Mapping[str, Any] | None = None,
    authoring_service=None,
    workflow_publisher=None,
    draft_service=None,
    authoring_jobs=None,
    shutdown_request: Callable[[], None] | None = None,
    langgraph_service=None,
    mcp_sessions=None,
    agent_fallback=None,
) -> list[Route]:
    """Routes for `/api/v1`, ready to mount on the composition root.

    The explicit signature is kept in sync with ``ApiContext`` so callers,
    documentation generators and static analysis can discover the supported
    composition options without inspecting the implementation.
    """

    ctx = ApiContext(
        db_path,
        execution_registry=execution_registry,
        workflow_db_path=workflow_db_path,
        authenticator=authenticator,
        authorizer=authorizer,
        rate_limiter=rate_limiter,
        unlimited_actors=unlimited_actors,
        token_exempt_actors=token_exempt_actors,
        operator_actors=operator_actors,
        audit=audit,
        fault_hook=fault_hook,
        clock=clock,
        agent_catalog=agent_catalog,
        capabilities=capabilities,
        schema_catalog=schema_catalog,
        artifact_backend=artifact_backend,
        operational_config=operational_config,
        authoring_service=authoring_service,
        workflow_publisher=workflow_publisher,
        draft_service=draft_service,
        authoring_jobs=authoring_jobs,
        shutdown_request=shutdown_request,
        langgraph_service=langgraph_service,
        mcp_sessions=mcp_sessions,
        agent_fallback=agent_fallback,
    )
    # Both modes get the same surface. `workflow_ui_mode` selects how many
    # Agents an author chooses between, not which features exist: a Runtime
    # that offered generation in one mode and refused it in the other made the
    # capability report a promise the routes did not keep.
    routes = [
        *ops.build_routes(ctx),
        *workflows.build_routes(ctx),
        *drafts.build_routes(ctx),
    ]
    if langgraph_service is not None:
        routes.extend(langgraph_runs.build_routes(
            ctx, langgraph_service,
        ))
    return routes
