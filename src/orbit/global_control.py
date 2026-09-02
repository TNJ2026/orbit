"""Machine-wide catalogs that never replace Workspace-owned runtime state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import hashlib
import json
import os
import re
from pathlib import Path
import threading
import uuid
from typing import Any, Callable, Mapping

from .workflow.dsl.schema import ID_PATTERN


DEFAULT_GLOBAL_ROOT = Path.home() / ".orbit" / "global"
# How long a receipt can still be replayed.
#
# A receipt makes a *retry* idempotent, and a retry belongs to the request it
# repeats: a client that never saw a response tries again in seconds, or gives
# up. Days later the same key is not a retry, it is a new request that happens
# to reuse a string. Kept generously long against clock skew and a client that
# queues offline work, and finite because nothing else here ever shrinks —
# every write added a row and none was ever removed.
RECEIPT_RETENTION = timedelta(days=7)
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

    def __init__(
        self, path: Path | str | None = None, *, clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path or DEFAULT_GLOBAL_ROOT / "workflow-templates.json").expanduser()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

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
        now = self._stamp()
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
                    "result": item, "at": now,
                }
            self._write(values, self._within_retention(receipts))
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
            # The create receipt holds the whole item, source text included, so
            # a deleted template was still in the file — every byte of it —
            # while `list` reported it gone.
            #
            # What it must not become is *absent*. An idempotency key with no
            # receipt is a key that was never used, so the client retrying that
            # create after a timeout would be given a new template_id and put
            # the deletion straight back. The receipt stays as a tombstone: the
            # source is dropped, the outcome is kept, and a replay reports what
            # happened instead of happening again.
            for receipt in receipts.values():
                result = receipt.get("result")
                if (
                    not isinstance(result, Mapping)
                    or result.get("template_id") != template_id
                ):
                    continue
                receipt["result"] = {
                    **{key: value for key, value in result.items() if key != "source"},
                    "deleted": True,
                }
            if idempotency_key:
                receipts[idempotency_key] = {
                    "operation": "delete", "request_hash": request_hash,
                    "result": existed, "at": self._stamp(),
                }
            if existed or idempotency_key:
                self._write(values, self._within_retention(receipts))
            return existed

    def _stamp(self) -> str:
        return self.clock().astimezone(timezone.utc).isoformat()

    def _within_retention(
        self, receipts: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """The receipts still worth replaying, swept as the file is rewritten.

        Here rather than on a timer because this is the only moment the whole
        document is already in hand under the lock that owns it: a sweep costs
        nothing extra, and a catalog nobody writes to is a catalog that is not
        growing either.

        A receipt from before this had a date is stamped now rather than
        dropped — it may still be somebody's retry — so the first write after
        an upgrade starts every old row's clock instead of ending it.
        """

        cutoff = (self.clock().astimezone(timezone.utc) - RECEIPT_RETENTION).isoformat()
        kept: dict[str, dict[str, Any]] = {}
        for key, receipt in receipts.items():
            at = receipt.get("at")
            if not isinstance(at, str):
                kept[key] = {**receipt, "at": self._stamp()}
            elif at > cutoff:
                kept[key] = receipt
        return kept

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
