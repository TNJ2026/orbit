"""Versioned compile-time registry for DSL extension envelopes."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator

from ..domain.serialization import canonical_json, freeze_json, to_primitive


@dataclass(frozen=True)
class ExtensionManifest:
    extension_id: str
    extension_version: str
    config_schema: Mapping[str, Any]
    draft: bool = True
    executable: bool = False

    def __post_init__(self) -> None:
        if not self.extension_id.strip() or not self.extension_version.strip():
            raise ValueError("extension id and version are required")
        if not self.draft and not self.executable:
            raise ValueError("a stable extension must declare executable semantics")
        object.__setattr__(self, "config_schema", freeze_json(self.config_schema))
        Draft202012Validator.check_schema(to_primitive(self.config_schema))


class InMemoryExtensionRegistry:
    def __init__(self, manifests: Iterable[ExtensionManifest] = ()) -> None:
        values: dict[tuple[str, str], ExtensionManifest] = {}
        for manifest in manifests:
            key = (manifest.extension_id, manifest.extension_version)
            if key in values:
                raise ValueError(f"duplicate extension manifest: {key[0]}@{key[1]}")
            values[key] = manifest
        self._values = MappingProxyType(values)
        self._fingerprint = "sha256:" + hashlib.sha256(
            canonical_json([values[key] for key in sorted(values)]).encode()
        ).hexdigest()

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def get(self, extension_id: str, extension_version: str) -> ExtensionManifest | None:
        return self._values.get((extension_id, extension_version))
