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


class ArtifactNotVisible(LookupError):
    """The actor-visible catalog contains no such Artifact."""


def _metadata(row) -> dict[str, Any]:
    content_type = row["content_type"]
    return {
        "artifact_id": row["artifact_id"],
        "run_id": row["run_id"],
        "workflow_id": row["workflow_id"],
        "producer_type": row["producer_type"],
        "producer_id": row["producer_id"],
        "producer_node_run_id": row["producer_node_run_id"],
        "output_port_id": row["output_port_id"],
        "schema_id": row["schema_id"],
        "content_type": content_type,
        "checksum": row["checksum"],
        "size_bytes": int(row["size_bytes"]),
        "visibility": row["visibility"],
        "scope_id": row["scope_id"],
        "created_at": row["created_at"],
        "committed_at": row["committed_at"],
        "previewable": (
            int(row["size_bytes"]) <= PREVIEW_LIMIT_BYTES
            and (content_type.startswith("text/") or content_type == "application/json")
        ),
    }


class ArtifactReadModelService:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def list(
        self, actor: str, *, cursor: str | None = None, limit: int = 50,
        q: str = "", run_id: str = "", content_type: str = "",
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
                " OR LOWER(a.output_port_id) LIKE ? ESCAPE '\\'"
                " OR LOWER(a.schema_id) LIKE ? ESCAPE '\\'"
                " OR LOWER(a.workflow_id) LIKE ? ESCAPE '\\')"
            )
            params.extend((pattern, pattern, pattern, pattern))
        with connect_workflow_database(self.path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT DISTINCT a.* FROM artifacts a"
                " JOIN artifact_acl acl ON acl.artifact_id=a.artifact_id"
                f" WHERE {' AND '.join(clauses)}"
                " ORDER BY a.artifact_id LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [_metadata(row) for row in rows]
        next_cursor = (
            encode_cursor({"query": query, "artifact_id": items[-1]["artifact_id"]})
            if has_more else None
        )
        return items, next_cursor

    def authorized_record(self, actor: str, artifact_id: EntityId):
        with connect_workflow_database(self.path, read_only=True) as connection:
            row = connection.execute(
                "SELECT a.* FROM artifacts a JOIN artifact_acl acl"
                " ON acl.artifact_id=a.artifact_id"
                " WHERE a.artifact_id=? AND a.status='committed'"
                " AND acl.subject=? AND acl.permission='read'",
                (str(artifact_id), actor),
            ).fetchone()
        if row is None:
            raise ArtifactNotVisible("Artifact not found")
        return row

    def detail(self, actor: str, artifact_id: EntityId) -> dict[str, Any]:
        return _metadata(self.authorized_record(actor, artifact_id))

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
