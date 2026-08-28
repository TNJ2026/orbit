"""Read-only Handler Catalog port and deterministic in-memory adapter."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from types import MappingProxyType
from typing import Iterable, Mapping, Protocol

from ..domain.durable_execution import ExecutionSafety
from ..domain.handlers import ResourceProfile
from ..domain.serialization import canonical_json, freeze_json, to_primitive


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
        """The contract this Handler promises, without its build number.

        A CLI release is an operational upgrade, not a contract migration. What
        a binding has to be sure of is the ports, schemas and capabilities a
        Workflow was compiled against — the version is none of those. While it
        was hashed in here, every routine upgrade produced a different
        fingerprint, so a published Workflow naming the build it was written
        against stopped resolving the moment that build was replaced.
        """

        payload = {
            key: value for key, value in to_primitive(self).items()
            if key != "version"
        }
        return "sha256:" + hashlib.sha256(canonical_json(payload).encode()).hexdigest()

    @property
    def legacy_fingerprint(self) -> str:
        """What `fingerprint` was while the build number was still in it.

        Published WorkflowVersions are immutable — a database trigger enforces
        it — so fingerprints already recorded in them can never be rewritten.
        They are accepted at resolve time instead: a Workflow published before
        this change keeps running on the exact build it named, and anything
        compiled after it survives an upgrade. Both are the same Handler; only
        one of them says so in a way that outlives a release.
        """

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
        """The manifest a declaration selects, or None when nothing answers.

        The constraint is honoured whenever something satisfies it. When
        nothing does and the Handler is an Agent, the installed build answers
        anyway: an Agent's version says which CLI release is on this machine,
        which is not a choice the author made and not one they can keep true.
        A Workflow that named the build it was written against would otherwise
        stop compiling the day that build was replaced — so editing a Workflow
        from last month would begin by working out which release it was born
        on. Other Handler kinds keep their pin, because their versions move
        only when this repository does, and a pin that misses is real drift
        worth reporting rather than papering over.

        This is not a hole in the contract. The compiler still checks the
        selected manifest's ports and schemas against the node that declared
        it, so an Agent whose contract really did change is refused here — it
        is only the release number that stopped being a reason to refuse.
        """

        candidates = self._by_name.get(name, ())
        matches = [item for item in candidates if _matches(item.version, constraint)]
        if matches:
            return matches[0]
        # Sorted newest-first at construction, so the first is the newest.
        installed = [
            item for item in candidates if "agent.invoke" in item.capabilities
        ]
        return installed[0] if installed else None
