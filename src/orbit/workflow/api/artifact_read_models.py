"""Actor-scoped Artifact catalog and lineage projections.

Every public read joins ``artifact_acl`` before returning metadata.  Detail,
content and lineage callers use the same lookup so a missing Artifact and an
Artifact the actor cannot read are intentionally indistinguishable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..domain.ids import EntityId
from ..persistence.database import connect_workflow_database
from .dto import decode_cursor, encode_cursor


PREVIEW_LIMIT_BYTES = 64 * 1024
# Raster formats only, and never image/svg+xml: an SVG is a document that can
# carry script, and this content is served from the Runtime's own origin.
INLINE_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
IMAGE_PREVIEW_LIMIT_BYTES = 5 * 1024 * 1024
# A title lives in the opening lines; reading further would only widen what a
# catalog page pulls out of the Blob store.
TITLE_SCAN_BYTES = 8 * 1024
TITLE_MAX_CHARS = 120


class ArtifactNotVisible(LookupError):
    """The actor-visible catalog contains no such Artifact."""


def base_content_type(content_type: str) -> str:
    """The media type without its parameters, lowercased."""

    return content_type.split(";")[0].strip().lower()


def _is_document(content_type: str) -> bool:
    base = base_content_type(content_type)
    return base.startswith("text/") or base == "application/json"


def goal_title(text: str | None) -> str | None:
    """The one-line name of a goal, out of what may be a paragraph.

    A goal is written as a prompt, so its first non-empty line is the title a
    catalog row can carry. Everything after it belongs on the run page.
    """

    for line in (text or "").splitlines():
        stripped = " ".join(line.split())
        if stripped:
            return stripped[:TITLE_MAX_CHARS]
    return None


def document_title(content_type: str, content: bytes) -> str | None:
    """The name a reader would give this document, or None if it has none.

    Markdown headings, then the first non-empty line, then a JSON object's own
    ``title``/``name``. The catalog shows an Artifact by what it contains, so a
    document without a self-declared title honestly has none — the caller falls
    back to the port that produced it rather than inventing one here.
    """

    base = base_content_type(content_type)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("utf-8", "ignore")
    if base == "application/json":
        import json

        try:
            document = json.loads(text)
        except ValueError:
            return None
        if not isinstance(document, dict):
            return None
        for key in ("title", "name", "subject"):
            value = document.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:TITLE_MAX_CHARS]
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        # A fenced block or front matter opener is punctuation, not a title.
        if not stripped or stripped in {"---", "```"}:
            continue
        return stripped[:TITLE_MAX_CHARS]
    return None


def _metadata(row, *, title: str | None = None) -> dict[str, Any]:
    content_type = row["content_type"]
    base = base_content_type(content_type)
    size_bytes = int(row["size_bytes"])
    image_previewable = base in INLINE_IMAGE_TYPES and size_bytes <= IMAGE_PREVIEW_LIMIT_BYTES
    keys = row.keys()
    filename = row["filename"] if "filename" in keys else None
    display_name = filename or title or row["output_port_id"]
    return {
        "artifact_id": row["artifact_id"],
        "run_id": row["run_id"],
        # What the run was for, so a catalog row says which goal produced it.
        # One line: the rest of a goal prompt is the run page's to show.
        "goal": (
            goal_title(row["display_name"] or row["goal"])
            if "goal" in keys else None
        ),
        "workflow_id": row["workflow_id"],
        "producer_type": row["producer_type"],
        "producer_id": row["producer_id"],
        "producer_node_run_id": row["producer_node_run_id"],
        "output_port_id": row["output_port_id"],
        "filename": filename,
        "display_name": display_name,
        "display_name_source": (
            "filename" if filename else "content_title" if title else "output_port"
        ),
        "schema_id": row["schema_id"],
        "content_type": content_type,
        "checksum": row["checksum"],
        "size_bytes": size_bytes,
        # What this Artifact is, so the catalog can show it as itself: a
        # thumbnail for an image, a title for a document.
        "preview_kind": (
            "image" if base in INLINE_IMAGE_TYPES
            else "document" if _is_document(content_type) else "binary"
        ),
        "image_previewable": image_previewable,
        "title": title,
        "visibility": row["visibility"],
        "scope_id": row["scope_id"],
        "created_at": row["created_at"],
        "committed_at": row["committed_at"],
        "previewable": size_bytes <= PREVIEW_LIMIT_BYTES and _is_document(content_type),
    }


class ArtifactReadModelService:
    def __init__(self, path: Path | str, *, blob_reader=None) -> None:
        self.path = Path(path)
        # Titles are read from the Blob, so a caller that may not read content
        # gets a catalog without them: `with_titles` is the API layer's scope
        # check, and no reader at all means no titles anywhere.
        self.blob_reader = blob_reader

    def _title(self, row) -> str | None:
        if self.blob_reader is None:
            return None
        if not _is_document(row["content_type"]):
            return None
        if int(row["size_bytes"]) > PREVIEW_LIMIT_BYTES:
            return None
        try:
            content = self.blob_reader(row["blob_key"])
        except Exception:
            # A missing or corrupt Blob is the content endpoint's story to
            # tell; a catalog page must still list the Artifact.
            return None
        return document_title(row["content_type"], content[:TITLE_SCAN_BYTES])

    def list(
        self, actor: str, *, cursor: str | None = None, limit: int = 50,
        q: str = "", run_id: str = "", content_type: str = "",
        with_titles: bool = False,
    ) -> tuple[list[dict[str, Any]], str | None]:
        state = decode_cursor(cursor)
        q = q.strip().lower()
        run_id = run_id.strip()
        content_type = content_type.strip().lower()
        if len(q) > 200 or len(run_id) > 200 or len(content_type) > 200:
            raise ValueError("Artifact filters must be at most 200 characters")
        query = {"q": q, "run_id": run_id, "content_type": content_type}
        if state and state.get("query") != query:
            raise ValueError("cursor does not match this Artifact query")
        after = str(state.get("artifact_id", ""))
        clauses = [
            "a.status='committed'", "acl.subject=?", "acl.permission='read'",
            "a.artifact_id>?",
        ]
        params: list[Any] = [actor, after]
        if run_id:
            clauses.append("a.run_id=?")
            params.append(run_id)
        if content_type:
            clauses.append("LOWER(a.content_type)=?")
            params.append(content_type)
        if q:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            clauses.append(
                "(LOWER(a.artifact_id) LIKE ? ESCAPE '\\'"
                " OR LOWER(COALESCE(a.filename,'')) LIKE ? ESCAPE '\\'"
                " OR LOWER(a.output_port_id) LIKE ? ESCAPE '\\'"
                " OR LOWER(a.schema_id) LIKE ? ESCAPE '\\'"
                " OR LOWER(a.workflow_id) LIKE ? ESCAPE '\\'"
                " OR LOWER(COALESCE(wr.display_name, wr.goal, '')) LIKE ? ESCAPE '\\')"
            )
            params.extend((pattern, pattern, pattern, pattern, pattern, pattern))
        with connect_workflow_database(self.path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT DISTINCT a.*, wr.display_name, wr.goal FROM artifacts a"
                " JOIN artifact_acl acl ON acl.artifact_id=a.artifact_id"
                " LEFT JOIN workflow_runs wr ON wr.run_id=a.run_id"
                f" WHERE {' AND '.join(clauses)}"
                " ORDER BY a.artifact_id LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [
            _metadata(row, title=self._title(row) if with_titles else None)
            for row in rows
        ]
        next_cursor = (
            encode_cursor({"query": query, "artifact_id": items[-1]["artifact_id"]})
            if has_more else None
        )
        return items, next_cursor

    def authorized_record(self, actor: str, artifact_id: EntityId):
        with connect_workflow_database(self.path, read_only=True) as connection:
            row = connection.execute(
                "SELECT a.*, wr.display_name, wr.goal FROM artifacts a"
                " JOIN artifact_acl acl ON acl.artifact_id=a.artifact_id"
                " LEFT JOIN workflow_runs wr ON wr.run_id=a.run_id"
                " WHERE a.artifact_id=? AND a.status='committed'"
                " AND acl.subject=? AND acl.permission='read'",
                (str(artifact_id), actor),
            ).fetchone()
        if row is None:
            raise ArtifactNotVisible("Artifact not found")
        return row

    def detail(
        self, actor: str, artifact_id: EntityId, *, with_title: bool = False,
    ) -> dict[str, Any]:
        record = self.authorized_record(actor, artifact_id)
        return _metadata(record, title=self._title(record) if with_title else None)

    def lineage(self, actor: str, artifact_id: EntityId) -> dict[str, Any]:
        record = self.authorized_record(actor, artifact_id)
        with connect_workflow_database(self.path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT link_id, link_type, target_id, created_at"
                " FROM artifact_links WHERE artifact_id=?"
                " ORDER BY link_type, link_id",
                (str(artifact_id),),
            ).fetchall()
            linked_artifacts = tuple(
                row["target_id"] for row in rows
                if str(row["target_id"]).startswith("artifact:")
            )
            visible_targets: set[str] = set()
            if linked_artifacts:
                placeholders = ",".join("?" for _ in linked_artifacts)
                visible_targets = {
                    row["artifact_id"] for row in connection.execute(
                        "SELECT artifact_id FROM artifact_acl"
                        f" WHERE artifact_id IN ({placeholders})"
                        " AND subject=? AND permission='read'",
                        (*linked_artifacts, actor),
                    )
                }
        links = [
            {
                "link_id": row["link_id"], "type": row["link_type"],
                "source_id": str(artifact_id), "target_id": row["target_id"],
                "created_at": row["created_at"],
            }
            for row in rows
            if not str(row["target_id"]).startswith("artifact:")
            or row["target_id"] in visible_targets
        ]
        return {
            "artifact": _metadata(record),
            "producers": [item for item in links if item["type"] == "producer"],
            "consumers": [item for item in links if item["type"] == "consumer"],
            "derived_from": [item for item in links if item["type"] == "derived_from"],
        }
