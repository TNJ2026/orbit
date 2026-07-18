"""Stable Step 7 contracts for typed Values and immutable Artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import re
from typing import Any

from .ids import EntityId
from .models import ArtifactRef
from .serialization import canonical_json, definition_hash, freeze_json
from .versions import DefinitionHash


MAX_INLINE_VALUE_BYTES = 262_144
MAX_INLINE_RESULT_BYTES = 1_048_576
DEFAULT_ARTIFACT_BYTES = 67_108_864
MAX_ARTIFACT_BYTES = 1_073_741_824

_CONTENT_TYPE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$"
)


class PortTransport(str, Enum):
    INLINE = "inline"
    ARTIFACT_REF = "artifact_ref"
    SECRET_REF = "secret_ref"


class ArtifactVisibility(str, Enum):
    NODE = "node"
    RUN = "run"
    SUBFLOW = "subflow"
    WORKFLOW = "workflow"


class DataOwnerKind(str, Enum):
    RUN_INPUT = "run_input"
    NODE_INPUT = "node_input"
    ATTEMPT_OUTPUT = "attempt_output"


class ArtifactStatus(str, Enum):
    STAGED = "staged"
    COMMITTED = "committed"
    ABANDONED = "abandoned"


class ValueLinkType(str, Enum):
    MAPPED_FROM = "mapped_from"
    CONSUMED_BY = "consumed_by"


class ArtifactLinkType(str, Enum):
    PRODUCER = "producer"
    CONSUMER = "consumer"
    DERIVED_FROM = "derived_from"


def _required_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")


def _aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _expect(identifier: EntityId, *kinds: str) -> None:
    if not isinstance(identifier, EntityId):
        raise TypeError("identifier must be an EntityId")
    if identifier.kind not in kinds:
        raise ValueError(f"expected {'/'.join(kinds)} id, got {identifier.kind}")


def _canonical_size(value: Any) -> int:
    return len(canonical_json(value).encode("utf-8"))


def _data_id(kind: str, *parts: object) -> EntityId:
    raw = "|".join(str(item) for item in parts)
    return EntityId(kind, hashlib.sha256(raw.encode("utf-8")).hexdigest())


def derive_value_id(owner_id: EntityId, port_id: str, generation: int = 1) -> EntityId:
    _expect(owner_id, "run", "node_run", "attempt")
    _required_text(port_id, "port_id")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("Value generation must be positive")
    return _data_id("value", owner_id, port_id, generation)


def derive_artifact_id(
    owner_id: EntityId, port_id: str, logical_name: str,
) -> EntityId:
    _expect(owner_id, "run", "attempt")
    _required_text(port_id, "port_id")
    _required_text(logical_name, "logical_name")
    return _data_id("artifact", owner_id, port_id, logical_name)


@dataclass(frozen=True)
class PortDataPolicy:
    transport: PortTransport = PortTransport.INLINE
    max_size_bytes: int | None = None
    content_types: tuple[str, ...] = ()
    visibility: ArtifactVisibility | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.transport, PortTransport):
            raise TypeError("transport must be PortTransport")
        maximum = self.max_size_bytes
        content_types = tuple(
            sorted({item.strip().lower() for item in self.content_types})
        )
        visibility = self.visibility
        if self.transport is PortTransport.INLINE:
            maximum = MAX_INLINE_VALUE_BYTES if maximum is None else maximum
            if content_types or visibility is not None:
                raise ValueError("inline ports cannot declare content types or visibility")
            hard_limit = MAX_INLINE_VALUE_BYTES
        elif self.transport is PortTransport.ARTIFACT_REF:
            maximum = DEFAULT_ARTIFACT_BYTES if maximum is None else maximum
            content_types = content_types or ("application/octet-stream",)
            visibility = visibility or ArtifactVisibility.RUN
            if not isinstance(visibility, ArtifactVisibility):
                raise TypeError("visibility must be ArtifactVisibility")
            if any(_CONTENT_TYPE.fullmatch(item) is None for item in content_types):
                raise ValueError("artifact content_types must be normalized MIME types")
            hard_limit = MAX_ARTIFACT_BYTES
        else:
            maximum = 0 if maximum is None else maximum
            if maximum != 0 or content_types or visibility is not None:
                raise ValueError("secret_ref ports cannot declare size, content type, or visibility")
            hard_limit = 0
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
            raise ValueError("max_size_bytes must be a non-negative integer")
        if self.transport is not PortTransport.SECRET_REF and maximum < 1:
            raise ValueError("data port max_size_bytes must be positive")
        if maximum > hard_limit:
            raise ValueError("max_size_bytes exceeds the transport hard limit")
        object.__setattr__(self, "max_size_bytes", maximum)
        object.__setattr__(self, "content_types", content_types)
        object.__setattr__(self, "visibility", visibility)


@dataclass(frozen=True)
class SecretRef:
    logical_name: str
    version: str | None = None
    provider_hint: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.logical_name, "logical_name")
        for field in ("version", "provider_hint"):
            value = getattr(self, field)
            if value is not None:
                _required_text(value, field)


@dataclass(frozen=True)
class ValueCommit:
    port_id: str
    schema_id: str
    data: Any
    checksum: DefinitionHash
    size_bytes: int

    def __post_init__(self) -> None:
        _required_text(self.port_id, "port_id")
        _required_text(self.schema_id, "schema_id")
        frozen = freeze_json(self.data)
        actual_size = _canonical_size(frozen)
        if actual_size > MAX_INLINE_VALUE_BYTES:
            raise ValueError("inline Value exceeds the 256 KiB hard limit")
        if self.size_bytes != actual_size:
            raise ValueError("Value size_bytes does not match canonical data")
        if not isinstance(self.checksum, DefinitionHash):
            raise TypeError("Value checksum must be DefinitionHash")
        if self.checksum != definition_hash(frozen):
            raise ValueError("Value checksum does not match canonical data")
        object.__setattr__(self, "data", frozen)


@dataclass(frozen=True)
class StagedArtifactCommit:
    port_id: str
    artifact_id: EntityId
    checksum: DefinitionHash
    size_bytes: int

    def __post_init__(self) -> None:
        _required_text(self.port_id, "port_id")
        _expect(self.artifact_id, "artifact")
        if not isinstance(self.checksum, DefinitionHash):
            raise TypeError("Artifact checksum must be DefinitionHash")
        if (
            isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int)
            or not 0 <= self.size_bytes <= MAX_ARTIFACT_BYTES
        ):
            raise ValueError("Artifact size_bytes is outside the system limit")


@dataclass(frozen=True)
class DataCommitManifest:
    run_id: EntityId
    owner_kind: DataOwnerKind
    owner_id: EntityId
    values: tuple[ValueCommit, ...] = ()
    artifacts: tuple[StagedArtifactCommit, ...] = ()

    def __post_init__(self) -> None:
        _expect(self.run_id, "run")
        if not isinstance(self.owner_kind, DataOwnerKind):
            raise TypeError("owner_kind must be DataOwnerKind")
        expected = {
            DataOwnerKind.RUN_INPUT: "run",
            DataOwnerKind.NODE_INPUT: "node_run",
            DataOwnerKind.ATTEMPT_OUTPUT: "attempt",
        }[self.owner_kind]
        _expect(self.owner_id, expected)
        if self.owner_kind is DataOwnerKind.RUN_INPUT and self.owner_id != self.run_id:
            raise ValueError("run_input owner_id must equal run_id")
        values = tuple(self.values)
        artifacts = tuple(self.artifacts)
        ports = [item.port_id for item in (*values, *artifacts)]
        if len(ports) != len(set(ports)):
            raise ValueError("Data Commit Manifest contains duplicate output ports")
        total_inline = sum(item.size_bytes for item in values)
        if total_inline > MAX_INLINE_RESULT_BYTES:
            raise ValueError("Data Commit Manifest exceeds the 1 MiB inline result limit")
        object.__setattr__(self, "values", values)
        object.__setattr__(self, "artifacts", artifacts)


@dataclass(frozen=True)
class ValueRecord:
    value_id: EntityId
    run_id: EntityId
    owner_kind: DataOwnerKind
    owner_id: EntityId
    port_id: str
    schema_id: str
    data: Any
    checksum: DefinitionHash
    size_bytes: int
    created_event_id: EntityId
    created_at: datetime

    def __post_init__(self) -> None:
        _expect(self.value_id, "value")
        _expect(self.run_id, "run")
        _expect(self.created_event_id, "event")
        _aware(self.created_at, "created_at")
        commit = ValueCommit(
            self.port_id, self.schema_id, self.data, self.checksum, self.size_bytes
        )
        if not isinstance(self.owner_kind, DataOwnerKind):
            raise TypeError("owner_kind must be DataOwnerKind")
        expected = {
            DataOwnerKind.RUN_INPUT: "run",
            DataOwnerKind.NODE_INPUT: "node_run",
            DataOwnerKind.ATTEMPT_OUTPUT: "attempt",
        }[self.owner_kind]
        _expect(self.owner_id, expected)
        if self.owner_kind is DataOwnerKind.RUN_INPUT and self.owner_id != self.run_id:
            raise ValueError("run_input owner_id must equal run_id")
        object.__setattr__(self, "data", commit.data)


@dataclass(frozen=True)
class ValueLink:
    link_id: EntityId
    run_id: EntityId
    source_value_id: EntityId
    target_value_id: EntityId
    link_type: ValueLinkType
    mapping_hash: DefinitionHash | None
    created_event_id: EntityId
    created_at: datetime

    def __post_init__(self) -> None:
        _expect(self.link_id, "value_link")
        _expect(self.run_id, "run")
        _expect(self.source_value_id, "value")
        _expect(self.target_value_id, "value")
        _expect(self.created_event_id, "event")
        _aware(self.created_at, "created_at")
        if self.source_value_id == self.target_value_id:
            raise ValueError("Value Link cannot reference itself")
        if not isinstance(self.link_type, ValueLinkType):
            raise TypeError("link_type must be ValueLinkType")
        if self.link_type is ValueLinkType.MAPPED_FROM and self.mapping_hash is None:
            raise ValueError("mapped_from Value Link requires mapping_hash")
        if self.link_type is ValueLinkType.CONSUMED_BY and self.mapping_hash is not None:
            raise ValueError("consumed_by Value Link cannot contain mapping_hash")
        if self.mapping_hash is not None and not isinstance(self.mapping_hash, DefinitionHash):
            raise TypeError("mapping_hash must be DefinitionHash")


@dataclass(frozen=True)
class ArtifactMetadata:
    artifact_id: EntityId
    run_id: EntityId
    workflow_id: EntityId
    producer_type: str
    producer_id: EntityId
    producer_node_run_id: EntityId | None
    output_port_id: str
    schema_id: str
    content_type: str
    checksum: DefinitionHash
    size_bytes: int
    blob_key: str
    visibility: ArtifactVisibility
    scope_id: EntityId
    status: ArtifactStatus
    created_at: datetime
    committed_at: datetime | None = None
    created_event_id: EntityId | None = None

    def __post_init__(self) -> None:
        _expect(self.artifact_id, "artifact")
        _expect(self.run_id, "run")
        _expect(self.workflow_id, "workflow")
        _required_text(self.output_port_id, "output_port_id")
        _required_text(self.schema_id, "schema_id")
        normalized_type = self.content_type.strip().lower()
        if _CONTENT_TYPE.fullmatch(normalized_type) is None:
            raise ValueError("content_type must be a normalized MIME type")
        if not isinstance(self.checksum, DefinitionHash):
            raise TypeError("Artifact checksum must be DefinitionHash")
        if (
            isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int)
            or not 0 <= self.size_bytes <= MAX_ARTIFACT_BYTES
        ):
            raise ValueError("Artifact size_bytes is outside the system limit")
        if self.blob_key != self.checksum.value:
            raise ValueError("Artifact blob_key must equal its content checksum")
        if self.producer_type == "attempt":
            _expect(self.producer_id, "attempt")
            if self.producer_node_run_id is None:
                raise ValueError("Attempt Artifact requires producer_node_run_id")
            _expect(self.producer_node_run_id, "node_run")
        elif self.producer_type == "run_ingress":
            _expect(self.producer_id, "run")
            if self.producer_node_run_id is not None:
                raise ValueError("Run ingress Artifact cannot have producer_node_run_id")
        else:
            raise ValueError("unsupported Artifact producer_type")
        if not isinstance(self.visibility, ArtifactVisibility):
            raise TypeError("visibility must be ArtifactVisibility")
        if not isinstance(self.status, ArtifactStatus):
            raise TypeError("status must be ArtifactStatus")
        scope_kind = {
            ArtifactVisibility.NODE: "node_run",
            ArtifactVisibility.RUN: "run",
            ArtifactVisibility.SUBFLOW: "subflow",
            ArtifactVisibility.WORKFLOW: "workflow",
        }[self.visibility]
        if not isinstance(self.scope_id, EntityId) or self.scope_id.kind != scope_kind:
            raise ValueError(
                f"Artifact scope_id must be {scope_kind} for {self.visibility.value} visibility"
            )
        _aware(self.created_at, "created_at")
        if self.status is ArtifactStatus.COMMITTED:
            if self.committed_at is None or self.created_event_id is None:
                raise ValueError("committed Artifact requires commit time and Event")
            _aware(self.committed_at, "committed_at")
            _expect(self.created_event_id, "event")
        elif self.committed_at is not None or self.created_event_id is not None:
            raise ValueError("operational Artifact state cannot reference a commit Event")
        object.__setattr__(self, "content_type", normalized_type)

    @property
    def ref(self) -> ArtifactRef:
        return ArtifactRef(
            self.artifact_id, self.schema_id, self.content_type,
            self.checksum, self.size_bytes,
        )


@dataclass(frozen=True)
class ArtifactLink:
    link_id: EntityId
    workflow_id: EntityId
    run_id: EntityId
    artifact_id: EntityId
    link_type: ArtifactLinkType
    target_id: EntityId
    created_event_id: EntityId
    created_at: datetime

    def __post_init__(self) -> None:
        _expect(self.link_id, "artifact_link")
        _expect(self.workflow_id, "workflow")
        _expect(self.run_id, "run")
        _expect(self.artifact_id, "artifact")
        _expect(self.created_event_id, "event")
        _aware(self.created_at, "created_at")
        if not isinstance(self.link_type, ArtifactLinkType):
            raise TypeError("link_type must be ArtifactLinkType")
        allowed = {
            ArtifactLinkType.PRODUCER: ("attempt", "run"),
            ArtifactLinkType.CONSUMER: ("attempt", "node_run"),
            ArtifactLinkType.DERIVED_FROM: ("artifact",),
        }[self.link_type]
        _expect(self.target_id, *allowed)
        if self.target_id == self.artifact_id:
            raise ValueError("Artifact cannot derive from itself")


@dataclass(frozen=True)
class InputManifestItem:
    port_id: str
    transport: PortTransport
    schema_id: str
    value: ValueCommit | None = None
    artifact: ArtifactRef | None = None
    secret: SecretRef | None = None

    def __post_init__(self) -> None:
        _required_text(self.port_id, "port_id")
        _required_text(self.schema_id, "schema_id")
        if not isinstance(self.transport, PortTransport):
            raise TypeError("transport must be PortTransport")
        present = sum(item is not None for item in (self.value, self.artifact, self.secret))
        if present != 1:
            raise ValueError("Input Manifest item requires exactly one data representation")
        if self.transport is PortTransport.INLINE and self.value is None:
            raise ValueError("inline Input Manifest item requires Value")
        if self.transport is PortTransport.ARTIFACT_REF and self.artifact is None:
            raise ValueError("artifact_ref Input Manifest item requires ArtifactRef")
        if self.transport is PortTransport.SECRET_REF and self.secret is None:
            raise ValueError("secret_ref Input Manifest item requires SecretRef")
        if self.value is not None and not isinstance(self.value, ValueCommit):
            raise TypeError("value must be ValueCommit")
        if self.artifact is not None and not isinstance(self.artifact, ArtifactRef):
            raise TypeError("artifact must be ArtifactRef")
        if self.secret is not None and not isinstance(self.secret, SecretRef):
            raise TypeError("secret must be SecretRef")
        if self.value is not None and self.value.schema_id != self.schema_id:
            raise ValueError("Input Value schema does not match its port")
        if self.value is not None and self.value.port_id != self.port_id:
            raise ValueError("Input Value port does not match its manifest item")
        if self.artifact is not None and self.artifact.schema_id != self.schema_id:
            raise ValueError("Input Artifact schema does not match its port")


@dataclass(frozen=True)
class InputManifest:
    run_id: EntityId
    node_run_id: EntityId
    attempt_id: EntityId
    items: tuple[InputManifestItem, ...]

    def __post_init__(self) -> None:
        _expect(self.run_id, "run")
        _expect(self.node_run_id, "node_run")
        _expect(self.attempt_id, "attempt")
        items = tuple(sorted(self.items, key=lambda item: item.port_id))
        if len(items) != len({item.port_id for item in items}):
            raise ValueError("Input Manifest contains duplicate ports")
        object.__setattr__(self, "items", items)
