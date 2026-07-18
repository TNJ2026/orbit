"""Artifact blob backend port."""

from dataclasses import dataclass
from typing import BinaryIO, Protocol

from ..domain.versions import DefinitionHash


@dataclass(frozen=True)
class BlobReceipt:
    blob_key: str
    checksum: DefinitionHash
    size_bytes: int


class BlobBackendPort(Protocol):
    def write(self, content: bytes, *, max_size_bytes: int) -> BlobReceipt: ...
    def write_stream(self, source: BinaryIO, *, max_size_bytes: int) -> BlobReceipt: ...
    def read(self, blob_key: str, *, max_size_bytes: int | None = None) -> bytes: ...
    def verify(self, blob_key: str, checksum: DefinitionHash, size_bytes: int) -> bool: ...
    def delete(self, blob_key: str) -> bool: ...
    def list_blob_keys(self) -> tuple[str, ...]: ...
