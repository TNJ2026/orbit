"""`/api/v1` Artifact reads — catalog, detail, lineage and content."""

from __future__ import annotations

from urllib.parse import quote

import anyio

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ...workflow.api.dto import CursorError, envelope
from ...workflow.api.artifact_read_models import (
    ArtifactNotVisible, IMAGE_PREVIEW_LIMIT_BYTES, INLINE_IMAGE_TYPES,
    PREVIEW_LIMIT_BYTES, base_content_type,
)
from ...workflow.domain.ids import EntityId
from ...workflow.domain.versions import DefinitionHash
from ...workflow.artifacts.local_cas import BlobIntegrityError

from .common import (
    READ_SCOPE, SENSITIVE_SCOPE, ClosingStreamingResponse, error,
)


def build_routes(ctx) -> list[Route]:
    async def artifact_list(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            unknown = set(request.query_params) - {
                "cursor", "limit", "q", "run_id", "content_type",
            }
            if unknown:
                raise ValueError(f"unknown Artifact query parameter: {sorted(unknown)[0]}")
            cursor, limit = ctx.read_params(request)
            # A document title is content. An actor who may not read content
            # gets the same catalog without it, never a 403 for the page.
            items, next_cursor = ctx.artifact_reads.list(
                actor, cursor=cursor, limit=limit,
                q=request.query_params.get("q", ""),
                run_id=request.query_params.get("run_id", ""),
                content_type=request.query_params.get("content_type", ""),
                with_titles=ctx.guard.allows(actor, SENSITIVE_SCOPE),
            )
        except CursorError as exc:
            return error("invalid_cursor", str(exc))
        except ValueError as exc:
            return error("invalid_request", str(exc))
        ctx.audit_artifact_read(
            actor, "artifact.list", "artifact_catalog", "allowed",
            details={"returned": len(items)},
        )
        return JSONResponse(envelope({"artifacts": items}, next_cursor=next_cursor))

    def _artifact_id(request: Request) -> EntityId:
        value = EntityId.parse(request.path_params["artifact_id"])
        if value.kind != "artifact":
            raise ValueError("Artifact not found")
        return value

    async def artifact_detail(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            payload = ctx.artifact_reads.detail(
                actor, _artifact_id(request),
                with_title=ctx.guard.allows(actor, SENSITIVE_SCOPE),
            )
        except (ArtifactNotVisible, ValueError):
            ctx.audit_artifact_read(
                actor, "artifact.metadata.read",
                request.path_params["artifact_id"], "denied",
            )
            # Same body for nonexistent, uncommitted and unauthorized ids.
            return error("artifact_not_found", "Artifact not found", 404)
        ctx.audit_artifact_read(
            actor, "artifact.metadata.read", payload["artifact_id"], "allowed",
            run_id=payload["run_id"],
        )
        return JSONResponse(envelope(payload))

    async def artifact_lineage(request: Request) -> JSONResponse:
        actor = ctx.authenticate(request, READ_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            payload = ctx.artifact_reads.lineage(actor, _artifact_id(request))
        except (ArtifactNotVisible, ValueError):
            ctx.audit_artifact_read(
                actor, "artifact.lineage.read",
                request.path_params["artifact_id"], "denied",
            )
            return error("artifact_not_found", "Artifact not found", 404)
        ctx.audit_artifact_read(
            actor, "artifact.lineage.read", payload["artifact"]["artifact_id"],
            "allowed", run_id=payload["artifact"]["run_id"],
        )
        return JSONResponse(envelope(payload))

    async def artifact_content(request: Request) -> Response:
        actor = ctx.authenticate(request, SENSITIVE_SCOPE)
        if isinstance(actor, JSONResponse):
            return actor
        if ctx.artifact_backend is None:
            return error("artifact_store_unavailable", "Artifact store is unavailable", 503)
        download = request.query_params.get("download") == "true"
        if set(request.query_params) - {"download"}:
            return error("invalid_request", "unknown Artifact content parameter")
        try:
            record = ctx.artifact_reads.authorized_record(actor, _artifact_id(request))
        except (ArtifactNotVisible, ValueError):
            ctx.audit_artifact_read(
                actor, "artifact.content.read",
                request.path_params["artifact_id"], "denied",
                details={"download": download},
            )
            return error("artifact_not_found", "Artifact not found", 404)
        base_type = base_content_type(record["content_type"])
        # An image is previewed as itself, so the catalog can show a thumbnail
        # instead of a filename. Raster types only, and each kind keeps its own
        # ceiling: an image that is worth showing is bigger than a text preview.
        inline_image = base_type in INLINE_IMAGE_TYPES
        preview_limit = IMAGE_PREVIEW_LIMIT_BYTES if inline_image else PREVIEW_LIMIT_BYTES
        if not download:
            if not (
                inline_image
                or base_type.startswith("text/") or base_type == "application/json"
            ):
                return error("preview_unsupported", "Artifact is not previewable", 415)
            if int(record["size_bytes"]) > preview_limit:
                return error(
                    "preview_too_large", "Artifact exceeds the preview limit", 413,
                    size_bytes=int(record["size_bytes"]), limit_bytes=preview_limit,
                )
        try:
            ctx.audit_artifact_read(
                actor, "artifact.content.read", record["artifact_id"], "allowed",
                run_id=record["run_id"], details={"download": download},
            )
            if download:
                if not hasattr(ctx.artifact_backend, "open_verified_stream"):
                    return error(
                        "artifact_stream_unavailable",
                        "Artifact backend does not support validated streaming", 503,
                    )
                source = await anyio.to_thread.run_sync(
                    ctx.artifact_backend.open_verified_stream,
                    record["blob_key"], DefinitionHash(record["checksum"]),
                    int(record["size_bytes"]),
                )
                def chunks():
                    try:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            yield chunk
                    finally:
                        source.close()

                filename = quote(
                    record["filename"] or f"{record['artifact_id']}.bin", safe="",
                )
                return ClosingStreamingResponse(
                    source, chunks(), media_type=record["content_type"],
                    headers={
                        "X-Content-Type-Options": "nosniff",
                        "Content-Length": str(record["size_bytes"]),
                        "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
                    },
                )
            content = ctx.artifact_backend.read(
                record["blob_key"], max_size_bytes=preview_limit
            )
        except BlobIntegrityError as exc:
            if "missing" in str(exc).lower():
                return error("blob_missing", "Artifact Blob is missing", 410)
            return error("artifact_integrity_failed", "Artifact integrity check failed", 409)
        if len(content) != int(record["size_bytes"]):
            return error("artifact_integrity_failed", "Artifact integrity check failed", 409)
        headers = {
            "X-Content-Type-Options": "nosniff",
            # Bytes served from the Runtime's own origin execute nothing: the
            # thumbnail an operator sees is a picture, never a document.
            "Content-Security-Policy": "default-src 'none'; sandbox",
        }
        if inline_image:
            # Authorization can change while immutable bytes do not. Never let
            # a browser cache bypass the scope and ACL checks above after
            # access has been revoked.
            headers["Cache-Control"] = "no-store"
        return Response(content, media_type=record["content_type"], headers=headers)

    return [
        Route("/api/v1/artifacts", artifact_list, methods=["GET"]),
        Route("/api/v1/artifacts/{artifact_id}", artifact_detail, methods=["GET"]),
        Route("/api/v1/artifacts/{artifact_id}/lineage", artifact_lineage, methods=["GET"]),
        Route("/api/v1/artifacts/{artifact_id}/content", artifact_content, methods=["GET"]),
    ]
