"""Read-only Handler Catalog port and deterministic in-memory adapter."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from types import MappingProxyType
from typing import Iterable, Mapping, Protocol

from ..domain.durable_execution import ExecutionSafety
from ..domain.handlers import ResourceProfile
from ..domain.serialization import canonical_json, freeze_json


_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(value)
    if match is None:
        raise ValueError(f"handler version must be semantic x.y.z: {value!r}")
    return tuple(int(item) for item in match.groups())  # type: ignore[return-value]


def _matches(version: str, constraint: str) -> bool:
    candidate = _version_tuple(version)
    if constraint.startswith("^"):
        parts = constraint[1:].split(".")
        if len(parts) not in {1, 2, 3} or not all(part.isdigit() for part in parts):
            raise ValueError(f"unsupported handler version constraint: {constraint!r}")
        requested = tuple(int(item) for item in parts)
        lower = requested + (0,) * (3 - len(requested))
        if candidate < lower:
            return False
        if lower[0] > 0:
            return candidate < (lower[0] + 1, 0, 0)
        if len(requested) >= 2:
            return candidate < (0, lower[1] + 1, 0)
        return candidate < (1, 0, 0)
    return version == constraint


@dataclass(frozen=True)
class HandlerManifest:
    name: str
    version: str
    node_kinds: tuple[str, ...]
    inputs: Mapping[str, str]
    outputs: Mapping[str, str]
    config_schema: Mapping[str, object]
    execution_safety: ExecutionSafety
    resource_profile: ResourceProfile
    result_schema_id: str
    capabilities: tuple[str, ...] = ()
    required_secrets: tuple[str, ...] = ()
    supports_cancel: bool = False
    supports_recover: bool = False
    manifest_version: str = "1.0"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("handler name is required")
        _version_tuple(self.version)
        if self.manifest_version != "1.0":
            raise ValueError("unsupported Handler Execution Manifest version")
        if not self.node_kinds:
            raise ValueError("handler must support at least one node kind")
        if not self.result_schema_id.strip():
            raise ValueError("result_schema_id is required")
        object.__setattr__(self, "node_kinds", tuple(sorted(set(self.node_kinds))))
        object.__setattr__(self, "inputs", MappingProxyType(dict(sorted(self.inputs.items()))))
        object.__setattr__(self, "outputs", MappingProxyType(dict(sorted(self.outputs.items()))))
        object.__setattr__(self, "config_schema", freeze_json(self.config_schema))
        object.__setattr__(self, "capabilities", tuple(sorted(set(self.capabilities))))
        object.__setattr__(self, "required_secrets", tuple(sorted(set(self.required_secrets))))
        for value in (*self.capabilities, *self.required_secrets):
            if not value.strip():
                raise ValueError("capability and secret names cannot be empty")

    @property
    def fingerprint(self) -> str:
        return "sha256:" + hashlib.sha256(canonical_json(self).encode()).hexdigest()


class HandlerCatalog(Protocol):
    @property
    def fingerprint(self) -> str: ...

    def resolve(self, name: str, constraint: str) -> HandlerManifest | None: ...


class InMemoryHandlerCatalog:
    def __init__(self, manifests: Iterable[HandlerManifest]) -> None:
        by_name: dict[str, list[HandlerManifest]] = {}
        seen: set[tuple[str, str]] = set()
        for manifest in manifests:
            key = (manifest.name, manifest.version)
            if key in seen:
                raise ValueError(f"duplicate handler manifest: {manifest.name}@{manifest.version}")
            seen.add(key)
            by_name.setdefault(manifest.name, []).append(manifest)
        self._by_name = {
            name: tuple(sorted(values, key=lambda item: _version_tuple(item.version), reverse=True))
            for name, values in by_name.items()
        }
        payload = [
            manifest
            for name in sorted(self._by_name)
            for manifest in reversed(self._by_name[name])
        ]
        self._fingerprint = "sha256:" + hashlib.sha256(canonical_json(payload).encode()).hexdigest()

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def resolve(self, name: str, constraint: str) -> HandlerManifest | None:
        matches = [item for item in self._by_name.get(name, ()) if _matches(item.version, constraint)]
        return matches[0] if matches else None
