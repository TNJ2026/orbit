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
        key = manifest.name
        if key in self._entries:
            raise ValueError(f"duplicate executable handler: {manifest.name}")
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
                    "manifest_fingerprint": entry.manifest.fingerprint,
                    "implementation_fingerprint": entry.implementation_fingerprint,
                }
                for name, entry in sorted(self._entries.items())
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
        # The version is still required to be a well-formed exact one, because
        # a caller passing a constraint has confused a published binding with a
        # DSL declaration. It no longer selects anything: identity is the name
        # plus the contract fingerprint, and the build number belongs to
        # neither. The Agent-shaped exception this used to carry is gone with
        # it — every Handler kind now survives a release the same way.
        _version_tuple(exact_version)
        entry = self._entries.get(name)
        if entry is None:
            raise HandlerNotAvailableError(f"handler not available: {name}")
        if expected_manifest_fingerprint is not None and (
            expected_manifest_fingerprint not in {
                entry.manifest.fingerprint, entry.manifest.legacy_fingerprint,
            }
        ):
            raise HandlerContractMismatchError(
                f"handler manifest mismatch: {name}"
            )
        return entry

    def entries(self) -> tuple[RegisteredHandler, ...]:
        if not self._sealed:
            raise RuntimeError("ExecutionRegistry must be sealed before query")
        return tuple(self._entries[key] for key in sorted(self._entries))
