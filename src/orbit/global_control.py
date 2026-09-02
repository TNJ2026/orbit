"""Machine-wide catalogs that never replace Workspace-owned runtime state."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import uuid
from typing import Any, Mapping

import yaml


DEFAULT_GLOBAL_ROOT = Path.home() / ".orbit" / "global"


class WorkflowTemplateError(ValueError):
    pass


class WorkflowTemplateStore:
    """Durable reusable DSL sources; publication remains a Runtime operation."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or DEFAULT_GLOBAL_ROOT / "workflow-templates.json").expanduser()
        self._lock = threading.Lock()

    def list(self) -> list[dict[str, Any]]:
        return sorted(self._read().values(), key=lambda item: (item["name"], item["template_id"]))

    def get(self, template_id: str) -> dict[str, Any]:
        item = self._read().get(template_id)
        if item is None:
            raise WorkflowTemplateError(f"unknown Workflow template: {template_id}")
        return item

    def put(
        self, *, name: str, source: str, source_format: str = "json",
        expected_version: int = 0, idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if expected_version != 0 or isinstance(expected_version, bool):
            raise WorkflowTemplateError("new template expected_version must be 0")
        if not name.strip():
            raise WorkflowTemplateError("template name is required")
        if source_format != "json":
            raise WorkflowTemplateError("only JSON Workflow templates are supported")
        try:
            document = json.loads(source)
        except json.JSONDecodeError as exc:
            raise WorkflowTemplateError("template source must be JSON") from exc
        metadata = document.get("metadata") if isinstance(document, Mapping) else None
        workflow_id = metadata.get("id") if isinstance(metadata, Mapping) else None
        if not isinstance(workflow_id, str) or not workflow_id.startswith("workflow:"):
            raise WorkflowTemplateError("template source must declare metadata.id")
        canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        request_hash = "sha256:" + hashlib.sha256(json.dumps({
            "name": name.strip(), "source": source,
            "source_format": source_format, "expected_version": expected_version,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            values, receipts = self._read_state()
            if idempotency_key:
                receipt = receipts.get(idempotency_key)
                if receipt:
                    if receipt.get("request_hash") != request_hash:
                        raise WorkflowTemplateError(
                            "idempotency key was already used for another request"
                        )
                    existing = receipt.get("result")
                    if not isinstance(existing, Mapping):
                        raise WorkflowTemplateError("idempotency receipt is inconsistent")
                    return dict(existing)
            template_id = "template:" + uuid.uuid4().hex
            item = {
                "template_id": template_id,
                "name": name.strip(),
                "workflow_id": workflow_id,
                "source_format": source_format,
                "source": source,
                "source_hash": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
                "version": 1,
                "created_at": now,
            }
            values[template_id] = item
            if idempotency_key:
                receipts[idempotency_key] = {
                    "operation": "create", "request_hash": request_hash,
                    "result": item,
                }
            self._write(values, receipts)
        return dict(item)

    def delete(
        self, template_id: str, *, expected_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> bool:
        request_hash = "sha256:" + hashlib.sha256(json.dumps({
            "template_id": template_id, "expected_version": expected_version,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self._lock:
            values, receipts = self._read_state()
            if idempotency_key:
                receipt = receipts.get(idempotency_key)
                if receipt:
                    if receipt.get("request_hash") != request_hash:
                        raise WorkflowTemplateError(
                            "idempotency key was already used for another request"
                        )
                    return bool(receipt.get("result"))
            item = values.get(template_id)
            if item is not None and expected_version is not None:
                if expected_version != item.get("version"):
                    raise WorkflowTemplateError(
                        f"template version conflict: expected {expected_version}, "
                        f"actual {item.get('version')}"
                    )
            existed = values.pop(template_id, None) is not None
            if idempotency_key:
                receipts[idempotency_key] = {
                    "operation": "delete", "request_hash": request_hash,
                    "result": existed,
                }
            if existed or idempotency_key:
                self._write(values, receipts)
            return existed

    def _read(self) -> dict[str, dict[str, Any]]:
        return self._read_state()[0]

    def _read_state(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}, {}
        values = payload.get("templates", {}) if isinstance(payload, Mapping) else {}
        receipts = payload.get("receipts", {}) if isinstance(payload, Mapping) else {}
        return ({
            str(key): dict(value) for key, value in values.items()
            if isinstance(key, str) and isinstance(value, Mapping)
        }, {
            str(key): dict(value) for key, value in receipts.items()
            if isinstance(key, str) and isinstance(value, Mapping)
        })

    def _write(
        self, values: Mapping[str, Mapping[str, Any]],
        receipts: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps({
            "schema_version": 1, "templates": values, "receipts": receipts,
        }, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.path)


def import_legacy_workflow_library(
    library_path: Path | str, store: WorkflowTemplateStore,
) -> int:
    """Turn old host-wide published sources into templates without deleting them.

    Canonical IR is deliberately not reverse-compiled. Definitions that never
    retained author source remain in the read-only legacy database for manual
    recovery; only source that can be recompiled by a target Runtime is shared.
    """

    path = Path(library_path).expanduser()
    if not path.exists():
        return 0
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        rows = connection.execute("""
            SELECT d.name, v.workflow_id, v.definition_hash,
                   v.source_format, v.source_text
            FROM workflow_versions v
            JOIN workflow_definitions d ON d.workflow_id=v.workflow_id
            JOIN (
                SELECT workflow_id, MAX(version) AS version
                FROM workflow_versions GROUP BY workflow_id
            ) latest ON latest.workflow_id=v.workflow_id AND latest.version=v.version
            LEFT JOIN archived_workflows a ON a.workflow_id=v.workflow_id
            WHERE a.workflow_id IS NULL AND v.source_text IS NOT NULL
            ORDER BY v.workflow_id
        """).fetchall()
        connection.close()
    except sqlite3.Error:
        return 0

    imported = 0
    for row in rows:
        source = str(row["source_text"])
        try:
            document = (
                json.loads(source) if row["source_format"] in {"json", "ui"}
                else yaml.safe_load(source)
            )
            normalized = json.dumps(document, ensure_ascii=False)
            before = len(store.list())
            store.put(
                name=str(row["name"]), source=normalized,
                expected_version=0,
                idempotency_key=f"legacy-library:{row['definition_hash']}",
            )
            imported += int(len(store.list()) > before)
        except (ValueError, TypeError, WorkflowTemplateError, yaml.YAMLError):
            continue
    return imported
