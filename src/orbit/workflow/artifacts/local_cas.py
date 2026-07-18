"""Trusted-local, content-addressed blob backend."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import re
import tempfile
from contextlib import contextmanager
from threading import RLock
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
    import msvcrt

from .backend import BlobReceipt
from ..domain.versions import DefinitionHash


_KEY = re.compile(r"^sha256:([0-9a-f]{64})$")


class BlobIntegrityError(IOError):
    pass


class LocalCASBackend:
    def __init__(self, root: Path | str, *, fault_hook=None) -> None:
        self.root = Path(root).expanduser().absolute()
        self.staging = self.root / "staging"
        self.blobs = self.root / "blobs" / "sha256"
        self.fault_hook = fault_hook
        self._thread_lock = RLock()
        self.preflight()

    def _fault(self, point: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(point)

    def preflight(self) -> None:
        self.staging.mkdir(parents=True, exist_ok=True)
        self.blobs.mkdir(parents=True, exist_ok=True)
        root = self.root.resolve()
        for directory in (self.staging, self.blobs):
            resolved = directory.resolve()
            if root != resolved and root not in resolved.parents:
                raise ValueError("Artifact directory escapes configured root")
        if os.stat(self.staging).st_dev != os.stat(self.blobs).st_dev:
            raise ValueError("staging and final Blob directories must share a filesystem")
        probe = self.staging / ".write-probe"
        try:
            probe.touch(exist_ok=False)
        finally:
            probe.unlink(missing_ok=True)

    @contextmanager
    def mutation_lock(self):
        """Serialize CAS publish+stage and GC across threads and processes."""
        lock_path = self.root / ".mutation.lock"
        with self._thread_lock:
            with lock_path.open("a+b") as lock_file:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                else:  # pragma: no cover - Windows
                    lock_file.seek(0); lock_file.write(b"0"); lock_file.flush(); lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    else:  # pragma: no cover - Windows
                        lock_file.seek(0); msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)

    @staticmethod
    def _digest_key(blob_key: str) -> str:
        match = _KEY.fullmatch(blob_key)
        if match is None:
            raise ValueError("invalid content-addressed blob key")
        return match.group(1)

    def _path(self, blob_key: str) -> Path:
        digest = self._digest_key(blob_key)
        bucket = self.blobs / digest[:2]
        if bucket.exists():
            root = self.blobs.resolve(); resolved = bucket.resolve()
            if root != resolved and root not in resolved.parents:
                raise ValueError("Artifact bucket escapes configured root")
        return bucket / digest

    def write(self, content: bytes, *, max_size_bytes: int) -> BlobReceipt:
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise TypeError("Artifact content must be bytes")
        return self.write_stream(io.BytesIO(bytes(content)), max_size_bytes=max_size_bytes)

    def write_stream(self, source, *, max_size_bytes: int) -> BlobReceipt:
        if isinstance(max_size_bytes, bool) or max_size_bytes < 0:
            raise ValueError("max_size_bytes must be non-negative")
        digest, size = hashlib.sha256(), 0
        fd, raw_path = tempfile.mkstemp(prefix="artifact-", dir=self.staging)
        temporary = Path(raw_path)
        try:
            with os.fdopen(fd, "wb") as target:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise TypeError("Artifact stream must yield bytes")
                    size += len(chunk)
                    if size > max_size_bytes:
                        raise ValueError("Artifact exceeds output port size limit")
                    digest.update(chunk)
                    target.write(chunk)
                self._fault("artifact_temp_written")
                self._fault("before_artifact_fsync")
                target.flush(); os.fsync(target.fileno())
                self._fault("after_artifact_fsync")
            checksum = DefinitionHash(f"sha256:{digest.hexdigest()}")
            final = self._path(checksum.value)
            final.parent.mkdir(parents=True, exist_ok=True)
            self._fault("before_artifact_rename")
            if final.exists():
                temporary.unlink()
            else:
                os.replace(temporary, final)
            self._fault("after_artifact_rename")
            directory_fd = os.open(final.parent, os.O_RDONLY)
            try: os.fsync(directory_fd)
            finally: os.close(directory_fd)
            return BlobReceipt(checksum.value, checksum, size)
        finally:
            temporary.unlink(missing_ok=True)

    def read(self, blob_key: str, *, max_size_bytes: int | None = None) -> bytes:
        path = self._path(blob_key)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            raise BlobIntegrityError("Artifact Blob is missing") from None
        if max_size_bytes is not None and size > max_size_bytes:
            raise BlobIntegrityError("Artifact Blob exceeds authorized size")
        content = path.read_bytes()
        checksum = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if checksum != blob_key:
            raise BlobIntegrityError("Artifact Blob checksum mismatch")
        return content

    def verify(self, blob_key, checksum, size_bytes) -> bool:
        try:
            content = self.read(blob_key, max_size_bytes=size_bytes)
            return len(content) == size_bytes and checksum.value == blob_key
        except (BlobIntegrityError, OSError, ValueError):
            return False

    def delete(self, blob_key: str) -> bool:
        path = self._path(blob_key)
        try: path.unlink()
        except FileNotFoundError: return False
        return True

    def list_blob_keys(self) -> tuple[str, ...]:
        result = []
        for path in self.blobs.glob("[0-9a-f][0-9a-f]/*"):
            if path.is_file() and re.fullmatch(r"[0-9a-f]{64}", path.name):
                result.append(f"sha256:{path.name}")
        return tuple(sorted(result))
