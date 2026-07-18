"""Fail-closed Artifact capabilities scoped to one Handler Attempt."""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
import tempfile
from typing import Mapping

from ..data.secrets import assert_no_secret_values
from ..domain.data import (
    ArtifactMetadata, ArtifactStatus, ArtifactVisibility, PortDataPolicy,
    PortTransport, derive_artifact_id,
)


class ArtifactAccessDenied(PermissionError):
    code = "artifact_access_denied"


class _ScopedWriter:
    def __init__(self, access, name, content_type, maximum):
        self.access, self.name = access, name
        self.content_type, self.maximum = content_type, maximum
        self._file = tempfile.SpooledTemporaryFile(
            max_size=min(maximum, 1024 * 1024), mode="w+b"
        )
        self.size = 0
        self.artifact_id = None

    def write(self, content):
        if not isinstance(content, bytes):
            raise TypeError("Artifact stream accepts bytes")
        if self.size + len(content) > self.maximum:
            raise ValueError("Artifact exceeds output port size limit")
        self.size += len(content)
        return self._file.write(content)

    def __enter__(self): return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is None:
                with self.access.backend.mutation_lock():
                    self._file.seek(0)
                    self.access._scan_stream(self._file)
                    self._file.seek(0)
                    receipt = self.access.backend.write_stream(
                        self._file, max_size_bytes=self.maximum
                    )
                    self.artifact_id = self.access._stage(
                        self.name, self.content_type, receipt
                    )
        finally:
            self._file.close()


class ScopedArtifactAccess:
    """No enumeration and no caller-controlled paths, schema, or visibility."""

    def __init__(
        self, backend, uow_factory, *, run_id, workflow_id, node_run_id,
        attempt_id, output_policies: Mapping[str, tuple[str, PortDataPolicy]],
        authorized_inputs=(), secret_values=(), clock,
    ) -> None:
        self.backend, self.uow_factory = backend, uow_factory
        self.run_id, self.workflow_id = run_id, workflow_id
        self.node_run_id, self.attempt_id = node_run_id, attempt_id
        self.output_policies = dict(output_policies)
        self.authorized = {item.artifact_id: item for item in authorized_inputs}
        self.secret_values, self.clock = tuple(secret_values), clock
        self._produced = {}

    @property
    def produced_artifact_ids(self):
        return tuple(self._produced[name].artifact_id for name in sorted(self._produced))

    def write(self, *, name: str, content: bytes, content_type: str):
        specification = self.output_policies.get(name)
        if specification is None:
            raise ArtifactAccessDenied("Artifact output port was not declared")
        schema_id, policy = specification
        if policy.transport is not PortTransport.ARTIFACT_REF:
            raise ArtifactAccessDenied("output port does not accept Artifacts")
        normalized = content_type.strip().lower()
        if normalized not in policy.content_types:
            raise ArtifactAccessDenied("Artifact content type is not allowed")
        with self.backend.mutation_lock():
            assert_no_secret_values(content, self.secret_values)
            receipt = self.backend.write(content, max_size_bytes=policy.max_size_bytes)
            return self._stage(name, normalized, receipt)

    def open_writer(self, *, name: str, content_type: str):
        specification = self.output_policies.get(name)
        if specification is None or specification[1].transport is not PortTransport.ARTIFACT_REF:
            raise ArtifactAccessDenied("Artifact output port was not declared")
        normalized = content_type.strip().lower()
        policy = specification[1]
        if normalized not in policy.content_types:
            raise ArtifactAccessDenied("Artifact content type is not allowed")
        return _ScopedWriter(self, name, normalized, policy.max_size_bytes)

    def _scan_stream(self, source):
        secrets = tuple(value.encode("utf-8") for value in self.secret_values if value)
        width = max((len(value) for value in secrets), default=1) - 1
        overlap = b""
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk: break
            combined = overlap + chunk
            if any(secret in combined for secret in secrets):
                raise ValueError("resolved Secret value detected in Artifact stream")
            overlap = combined[-width:] if width else b""

    def _stage(self, name, normalized, receipt):
        schema_id, policy = self.output_policies[name]
        artifact_id = derive_artifact_id(self.attempt_id, name, name)
        visibility = policy.visibility
        if visibility is ArtifactVisibility.NODE: scope_id = self.node_run_id
        elif visibility is ArtifactVisibility.RUN: scope_id = self.run_id
        elif visibility is ArtifactVisibility.WORKFLOW: scope_id = self.workflow_id
        else: raise ArtifactAccessDenied("subflow Artifact access is unavailable before Step 11")
        record = ArtifactMetadata(
            artifact_id, self.run_id, self.workflow_id, "attempt", self.attempt_id,
            self.node_run_id, name, schema_id, normalized, receipt.checksum,
            receipt.size_bytes, receipt.blob_key, visibility, scope_id,
            ArtifactStatus.STAGED, self.clock(),
        )
        prior = self._produced.get(name)
        if prior is not None:
            if prior.checksum == record.checksum: return artifact_id
            raise ArtifactAccessDenied("Artifact output port was written with different content")
        with self.uow_factory() as uow:
            stored = uow.artifacts.get(artifact_id)
            if stored is None: uow.artifacts.stage(record)
            elif not self._same_stage(stored, record):
                raise ArtifactAccessDenied("Artifact ID metadata conflict")
            else:
                record = stored
            uow.commit()
        self._produced[name] = record
        return artifact_id

    @staticmethod
    def _same_stage(left, right):
        fields = (
            "artifact_id", "run_id", "workflow_id", "producer_type", "producer_id",
            "producer_node_run_id", "output_port_id", "schema_id", "content_type",
            "checksum", "size_bytes", "blob_key", "visibility", "scope_id", "status",
        )
        return all(getattr(left, field) == getattr(right, field) for field in fields)

    def read(self, artifact_id, *, max_size_bytes: int | None = None) -> bytes:
        metadata = self.authorized.get(artifact_id)
        if metadata is None:
            raise ArtifactAccessDenied("Artifact was not authorized by Input Manifest")
        if metadata.status is not ArtifactStatus.COMMITTED:
            raise ArtifactAccessDenied("Artifact is not committed")
        limit = metadata.size_bytes if max_size_bytes is None else min(max_size_bytes, metadata.size_bytes)
        return self.backend.read(metadata.blob_key, max_size_bytes=limit)

    def open(self, artifact_id):
        return BytesIO(self.read(artifact_id))
