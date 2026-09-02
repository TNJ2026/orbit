"""Machine-wide catalogs that never replace Workspace-owned runtime state."""

from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
import os
import re
from pathlib import Path
import threading
import uuid
from typing import Any, Mapping

from .workflow.dsl.schema import ID_PATTERN


DEFAULT_GLOBAL_ROOT = Path.home() / ".orbit" / "global"
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def _process_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _file_lock(path: Path, *, shared: bool = False):
    """Serialize a machine-wide JSON transaction across threads and processes.

    A reader takes the shared side, and takes nothing at all when the lock file
    does not exist yet. Reads never mutate: `_write` swaps the document in with
    `Path.replace`, so a reader sees the whole of one version or the whole of
    the previous one, never a torn document. What the lock is for is the
    read-modify-write in `put` and `delete`, where two processes each read the
    same catalog and each write back their own addition, and one is lost.

    Failing to *take* the lock is a storage failure like any other rather than
    a traceback: a read-only home directory used to escape as a raw OSError,
    past the 503 the adapters raise `WorkflowTemplateStorageError` for, and
    Windows abandons a contended lock after ten seconds instead of waiting.
    Only acquisition is translated — an OSError from the work done under the
    lock is that work's to report, and saying "cannot lock" about a full disk
    would send the reader to the wrong place.
    """

    lock_path = path.with_suffix(path.suffix + ".lock")
    if shared and not lock_path.exists():
        # Nobody can be holding a transaction open, and reading creates
        # nothing: `WorkflowTemplateStore().list()` on a machine that has never
        # published used to leave a directory and a lock file behind it.
        yield
        return

    def _refuse(exc: OSError) -> WorkflowTemplateStorageError:
        return WorkflowTemplateStorageError(
            f"cannot lock Workflow template catalog: {exc}"
        )

    with _process_lock(lock_path):
        try:
            if not shared:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("rb" if shared else "a+b")
        except OSError as exc:
            raise _refuse(exc) from exc
        try:
            try:
                if not shared:
                    handle.seek(0, 2)
                    if handle.tell() == 0:
                        handle.write(b"0"); handle.flush()
                    handle.seek(0)
                if os.name == "nt":
                    # `msvcrt.locking` has no shared mode, so on Windows a
                    # reader still waits behind writers. Kept where there is no
                    # alternative rather than on every path.
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(
                        handle.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX,
                    )
            except OSError as exc:
                raise _refuse(exc) from exc
            yield
        finally:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


class WorkflowTemplateError(ValueError):
    pass


class WorkflowTemplateStorageError(WorkflowTemplateError):
    """The durable catalog cannot be trusted, so no mutation may continue."""


class WorkflowTemplateStore:
    """Durable reusable DSL sources; publication remains a Runtime operation."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or DEFAULT_GLOBAL_ROOT / "workflow-templates.json").expanduser()

    def list(self) -> list[dict[str, Any]]:
        with _file_lock(self.path, shared=True):
            values, _receipts = self._read_state_unlocked()
        return sorted(values.values(), key=lambda item: (item["name"], item["template_id"]))

    def get(self, template_id: str) -> dict[str, Any]:
        with _file_lock(self.path, shared=True):
            item = self._read_state_unlocked()[0].get(template_id)
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
        if not isinstance(workflow_id, str) or not workflow_id.strip():
            raise WorkflowTemplateError("template source must declare metadata.id")
        workflow_id = workflow_id.strip()
        # The DSL's own rule, imported rather than restated: `metadata.id` is a
        # bare identifier and the compiler is what prefixes it. A source that
        # arrives already prefixed cannot be compiled — the pattern forbids the
        # colon — so accepting one only stores a template that every attempt to
        # instantiate will reject, which is a worse answer than refusing it here.
        if not re.match(ID_PATTERN, workflow_id):
            raise WorkflowTemplateError(
                f"template metadata.id must match {ID_PATTERN}: {workflow_id}"
            )
        workflow_id = f"workflow:{workflow_id}"
        canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        request_hash = "sha256:" + hashlib.sha256(json.dumps({
            "name": name.strip(), "source": source,
            "source_format": source_format, "expected_version": expected_version,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        with _file_lock(self.path):
            values, receipts = self._read_state_unlocked()
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
        with _file_lock(self.path):
            values, receipts = self._read_state_unlocked()
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
            # The create receipt holds the whole item, source text included.
            # Left behind, a deleted template was still in the file — readable
            # to anyone with the file, and replayable into a resurrection by
            # whoever still held that key — while `list` reported it gone.
            # A receipt for something the catalog no longer has cannot be
            # replayed into anything true, so it goes with it.
            for key in [
                key for key, receipt in receipts.items()
                if isinstance(receipt.get("result"), Mapping)
                and receipt["result"].get("template_id") == template_id
            ]:
                receipts.pop(key)
            if idempotency_key:
                receipts[idempotency_key] = {
                    "operation": "delete", "request_hash": request_hash,
                    "result": existed,
                }
            if existed or idempotency_key:
                self._write(values, receipts)
            return existed

    def _read_state_unlocked(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}, {}
        except OSError as exc:
            raise WorkflowTemplateStorageError(
                f"cannot read Workflow template catalog: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise WorkflowTemplateStorageError(
                "Workflow template catalog is corrupt; refusing to overwrite it"
            ) from exc
        if not isinstance(payload, Mapping):
            raise WorkflowTemplateStorageError(
                "Workflow template catalog root must be an object"
            )
        values = payload.get("templates", {}) if isinstance(payload, Mapping) else {}
        receipts = payload.get("receipts", {}) if isinstance(payload, Mapping) else {}
        if not isinstance(values, Mapping) or not isinstance(receipts, Mapping):
            raise WorkflowTemplateStorageError(
                "Workflow template catalog has invalid collections"
            )
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
        document = json.dumps({
            "schema_version": 1, "templates": values, "receipts": receipts,
        }, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        # A catalog that cannot be written is the same kind of news as one that
        # cannot be read, and the adapters answer 503 to it. Left raw, a
        # read-only home directory reached the caller as a traceback instead.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            temporary.write_text(document, encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            raise WorkflowTemplateStorageError(
                f"cannot write Workflow template catalog: {exc}"
            ) from exc
