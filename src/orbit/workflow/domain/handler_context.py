"""Immutable request and capability ports exposed to NodeHandler code."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol, runtime_checkable

from .accounting import UsageSnapshot
from .durable_execution import ExecutionSafety
from .handlers import ResourceProfile
from .ids import EntityId
from .serialization import freeze_json
from .versions import Revision


@dataclass(frozen=True)
class ExecutorRequest:
    run_id: EntityId
    plan_id: EntityId
    plan_version: Revision
    node_run_id: EntityId
    attempt_id: EntityId
    attempt_number: Revision
    job_id: EntityId
    lease_id: EntityId
    node_id: str
    handler_name: str
    handler_version: str
    handler_manifest_fingerprint: str
    config: Any
    input: Any
    input_manifest: Mapping[str, str]
    output_manifest: Mapping[str, str]
    idempotency_key: str
    deadline: datetime
    execution_safety: ExecutionSafety
    resource_profile: ResourceProfile

    def __post_init__(self) -> None:
        for value, kind in (
            (self.run_id, "run"), (self.plan_id, "plan"),
            (self.node_run_id, "node_run"), (self.attempt_id, "attempt"),
            (self.job_id, "job"), (self.lease_id, "lease"),
        ):
            if value.kind != kind:
                raise ValueError(f"expected {kind} id")
        for value in (
            self.node_id, self.handler_name, self.handler_version,
            self.handler_manifest_fingerprint, self.idempotency_key,
        ):
            if not value.strip():
                raise ValueError("ExecutorRequest string fields are required")
        if self.deadline.tzinfo is None or self.deadline.utcoffset() is None:
            raise ValueError("deadline must be timezone-aware")
        object.__setattr__(self, "config", freeze_json(self.config))
        object.__setattr__(self, "input", freeze_json(self.input))
        object.__setattr__(self, "input_manifest", freeze_json(self.input_manifest))
        object.__setattr__(self, "output_manifest", freeze_json(self.output_manifest))


@dataclass(frozen=True)
class PrepareContext:
    request: ExecutorRequest


@runtime_checkable
class SecretResolverPort(Protocol):
    def resolve(self, name: str) -> object: ...


@runtime_checkable
class ArtifactWriterPort(Protocol):
    def write(self, *, name: str, content: bytes, content_type: str) -> EntityId: ...
    def open_writer(self, *, name: str, content_type: str): ...
    def read(self, artifact_id: EntityId, *, max_size_bytes: int | None = None) -> bytes: ...
    def open(self, artifact_id: EntityId): ...


@runtime_checkable
class UsageReporterPort(Protocol):
    def report(self, snapshot: UsageSnapshot) -> bool: ...
    def latest(self, attempt_id: EntityId) -> UsageSnapshot | None: ...


@runtime_checkable
class CancellationTokenPort(Protocol):
    @property
    def cancelled(self) -> bool: ...
    def raise_if_cancelled(self) -> None: ...


@runtime_checkable
class HandlerLoggerPort(Protocol):
    def __call__(self, message: str, fields: Mapping[str, Any]) -> None: ...


@runtime_checkable
class TracerPort(Protocol):
    def record(self, name: str, fields: Mapping[str, Any]) -> None: ...


@runtime_checkable
class ClockPort(Protocol):
    def __call__(self) -> datetime: ...


@dataclass(frozen=True)
class HandlerContext:
    request: ExecutorRequest
    secrets: SecretResolverPort
    artifacts: ArtifactWriterPort
    usage: UsageReporterPort
    cancellation: CancellationTokenPort
    logger: HandlerLoggerPort
    tracer: TracerPort
    clock: ClockPort
