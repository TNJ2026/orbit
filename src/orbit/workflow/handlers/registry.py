"""Sealed exact-version registry for executable Handler implementations."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

from ..catalogs.handlers import HandlerManifest, _version_tuple
from ..domain.handlers import NodeHandler
from ..domain.serialization import canonical_json


class HandlerNotAvailableError(LookupError):
    pass


class HandlerContractMismatchError(ValueError):
    pass


@dataclass(frozen=True)
class RegisteredHandler:
    manifest: HandlerManifest
    implementation: NodeHandler
    implementation_id: str
    implementation_fingerprint: str


class ExecutionRegistry:
    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], RegisteredHandler] = {}
        self._sealed = False
        self._fingerprint: str | None = None

    @property
    def sealed(self) -> bool:
        return self._sealed

    @property
    def fingerprint(self) -> str:
        if self._fingerprint is None:
            raise RuntimeError("ExecutionRegistry must be sealed before use")
        return self._fingerprint

    def register(
        self,
        manifest: HandlerManifest,
        implementation: NodeHandler,
        *,
        implementation_id: str,
    ) -> RegisteredHandler:
        if self._sealed:
            raise RuntimeError("ExecutionRegistry is sealed")
        if not implementation_id.strip():
            raise ValueError("implementation_id is required")
        if not isinstance(implementation, NodeHandler):
            raise TypeError("implementation does not satisfy NodeHandler")
        key = (manifest.name, manifest.version)
        if key in self._entries:
            raise ValueError(f"duplicate executable handler: {manifest.name}@{manifest.version}")
        digest = "sha256:" + hashlib.sha256(
            canonical_json(
                {
                    "manifest_fingerprint": manifest.fingerprint,
                    "implementation_id": implementation_id,
                }
            ).encode()
        ).hexdigest()
        entry = RegisteredHandler(manifest, implementation, implementation_id, digest)
        self._entries[key] = entry
        return entry

    def seal(self) -> str:
        if not self._sealed:
            payload = [
                {
                    "name": name,
                    "version": version,
                    "manifest_fingerprint": entry.manifest.fingerprint,
                    "implementation_fingerprint": entry.implementation_fingerprint,
                }
                for (name, version), entry in sorted(self._entries.items())
            ]
            self._fingerprint = "sha256:" + hashlib.sha256(
                canonical_json(payload).encode()
            ).hexdigest()
            self._sealed = True
        return self._fingerprint

    def resolve(
        self,
        name: str,
        exact_version: str,
        *,
        expected_manifest_fingerprint: str | None = None,
    ) -> RegisteredHandler:
        if not self._sealed:
            raise RuntimeError("ExecutionRegistry must be sealed before resolve")
        _version_tuple(exact_version)
        entry = self._entries.get((name, exact_version))
        if entry is None:
            raise HandlerNotAvailableError(f"handler not available: {name}@{exact_version}")
        if (
            expected_manifest_fingerprint is not None
            and entry.manifest.fingerprint != expected_manifest_fingerprint
        ):
            raise HandlerContractMismatchError(
                f"handler manifest mismatch: {name}@{exact_version}"
            )
        return entry

    def entries(self) -> tuple[RegisteredHandler, ...]:
        if not self._sealed:
            raise RuntimeError("ExecutionRegistry must be sealed before query")
        return tuple(self._entries[key] for key in sorted(self._entries))
