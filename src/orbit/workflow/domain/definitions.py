"""Immutable Canonical WorkflowIR 1.1 contracts."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .data import PortDataPolicy
from .serialization import freeze_json
from .versions import DefinitionHash


def _required(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value


@dataclass(frozen=True)
class IRPort:
    id: str
    schema_id: str
    required: bool
    has_default: bool
    default: Any
    description: str
    data_policy: PortDataPolicy = PortDataPolicy()

    def __post_init__(self) -> None:
        _required(self.id, "port id")
        _required(self.schema_id, "port schema_id")
        if not isinstance(self.required, bool) or not isinstance(self.has_default, bool):
            raise TypeError("port required and has_default must be booleans")
        if not isinstance(self.data_policy, PortDataPolicy):
            raise TypeError("port data_policy must be PortDataPolicy")
        if self.has_default and self.data_policy.transport.value != "inline":
            raise ValueError("only inline ports may declare defaults")
        object.__setattr__(self, "default", freeze_json(self.default))


@dataclass(frozen=True)
class IRHandlerRef:
    name: str
    version: str
    manifest_fingerprint: str

    def __post_init__(self) -> None:
        _required(self.name, "handler name")
        _required(self.version, "handler version")
        _required(self.manifest_fingerprint, "handler manifest fingerprint")
        if not self.manifest_fingerprint.startswith("sha256:"):
            raise ValueError("handler manifest fingerprint must be sha256")
        if self.version.startswith(("^", "~", ">", "<", "=")):
            raise ValueError("IR handler version must be exact")


@dataclass(frozen=True)
class IRExtension:
    extension_id: str
    extension_version: str
    config: Any

    def __post_init__(self) -> None:
        _required(self.extension_id, "extension id")
        _required(self.extension_version, "extension version")
        object.__setattr__(self, "config", freeze_json(self.config))


@dataclass(frozen=True)
class IRNode:
    id: str
    kind: str
    inputs: tuple[IRPort, ...]
    outputs: tuple[IRPort, ...]
    handler: IRHandlerRef | None
    config: Any
    policies: tuple[str, ...]
    extension: IRExtension | None
    route_mode: str | None = None

    def __post_init__(self) -> None:
        _required(self.id, "node id")
        if self.kind not in {"action", "human", "decision", "join", "terminal", "extension"}:
            raise ValueError(f"unsupported IR node kind: {self.kind}")
        if self.route_mode is not None and self.route_mode not in {"exclusive", "parallel"}:
            raise ValueError("route_mode must be exclusive or parallel")
        if self.route_mode is not None and self.kind not in {"action", "decision"}:
            raise ValueError("route_mode is only valid for action/decision nodes")
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "outputs", tuple(self.outputs))
        object.__setattr__(self, "config", freeze_json(self.config))
        object.__setattr__(self, "policies", tuple(self.policies))


@dataclass(frozen=True)
class IREdge:
    id: str
    source_node: str
    source_port: str
    target_node: str
    target_port: str
    route: str
    condition: Any
    mapping: Any
    priority: int = 0
    back_edge: bool = False
    policy_ref: str | None = None

    def __post_init__(self) -> None:
        for value, field in [
            (self.id, "edge id"),
            (self.source_node, "source node"),
            (self.source_port, "source port"),
            (self.target_node, "target node"),
            (self.target_port, "target port"),
        ]:
            _required(value, field)
        if self.route not in {"success", "error", "timeout", "cancel"}:
            raise ValueError("edge route must be success, error, timeout, or cancel")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int) or self.priority < 0:
            raise ValueError("edge priority must be a non-negative integer")
        if not isinstance(self.back_edge, bool):
            raise TypeError("edge back_edge must be boolean")
        if self.policy_ref is not None:
            _required(self.policy_ref, "edge policy_ref")
        object.__setattr__(self, "condition", freeze_json(self.condition))
        object.__setattr__(self, "mapping", freeze_json(self.mapping))


@dataclass(frozen=True)
class IRPolicy:
    id: str
    kind: str
    config: Any

    def __post_init__(self) -> None:
        _required(self.id, "policy id")
        _required(self.kind, "policy kind")
        object.__setattr__(self, "config", freeze_json(self.config))


@dataclass(frozen=True)
class WorkflowIR:
    ir_version: str
    workflow_id: str
    name: str
    description: str
    labels: Mapping[str, str]
    inputs: tuple[IRPort, ...]
    outputs: tuple[IRPort, ...]
    nodes: tuple[IRNode, ...]
    edges: tuple[IREdge, ...]
    entry: tuple[str, ...]
    terminals: tuple[str, ...]
    policies: tuple[IRPolicy, ...]
    extensions: tuple[IRExtension, ...]
    indexes: Any

    def __post_init__(self) -> None:
        if self.ir_version not in {"1.1", "1.2"}:
            raise ValueError("unsupported WorkflowIR version")
        _required(self.workflow_id, "workflow id")
        _required(self.name, "workflow name")
        object.__setattr__(self, "labels", MappingProxyType(dict(self.labels)))
        for field in ("inputs", "outputs", "nodes", "edges", "entry", "terminals", "policies", "extensions"):
            object.__setattr__(self, field, tuple(getattr(self, field)))
        object.__setattr__(self, "indexes", freeze_json(self.indexes))


@dataclass(frozen=True)
class CompiledWorkflow:
    ir: WorkflowIR
    definition_hash: DefinitionHash
    compiler_version: str
    catalog_fingerprint: str

    def __post_init__(self) -> None:
        _required(self.compiler_version, "compiler version")
        _required(self.catalog_fingerprint, "catalog fingerprint")
