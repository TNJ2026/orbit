"""Application facade for Artifact diagnostics, lineage, and maintenance."""

from pathlib import Path

from ..artifacts import ArtifactGarbageCollector, LocalCASBackend, check_artifacts
from ..data.lineage import LineageQueryService
from ..persistence import SQLiteUnitOfWork
from ..domain.data import derive_artifact_id


class RunInputIngressSession:
    """Publish input Blobs first; only the Kernel may commit their metadata."""
    def __init__(self, backend, *, run_id, policies, secret_values=()):
        self.backend, self.run_id, self.policies = backend, run_id, dict(policies)
        self.secret_values, self._items = tuple(secret_values), {}

    def write(self, *, port_id, content, content_type):
        from ..data.secrets import assert_no_secret_values
        from ..domain.data import PortTransport
        schema_id, policy = self.policies.get(port_id, (None, None))
        if policy is None or policy.transport is not PortTransport.ARTIFACT_REF:
            raise PermissionError("Run input Artifact port was not declared")
        content_type = content_type.strip().lower()
        if content_type not in policy.content_types: raise PermissionError("content type is not allowed")
        assert_no_secret_values(content, self.secret_values)
        receipt = self.backend.write(content, max_size_bytes=policy.max_size_bytes)
        artifact_id = derive_artifact_id(self.run_id, port_id, port_id)
        item = {
            "port_id": port_id, "artifact_id": str(artifact_id), "schema_id": schema_id,
            "content_type": content_type, "checksum": receipt.checksum.value,
            "size_bytes": receipt.size_bytes, "blob_key": receipt.blob_key,
        }
        prior = self._items.get(port_id)
        if prior is not None and prior != item: raise ValueError("Run input port content changed")
        self._items[port_id] = item
        return artifact_id

    def command_payload(self): return [self._items[key] for key in sorted(self._items)]


class DataApplicationService:
    def __init__(self, database_path, artifact_root) -> None:
        self.uow_factory = lambda: SQLiteUnitOfWork(Path(database_path))
        self.backend = LocalCASBackend(artifact_root)
        self.lineage = LineageQueryService(self.uow_factory)
        self.gc = ArtifactGarbageCollector(self.uow_factory, self.backend)

    def get_value(self, value_id):
        with self.uow_factory() as uow: return uow.values.get(value_id)

    def get_artifact(self, artifact_id):
        with self.uow_factory() as uow: return uow.artifacts.get(artifact_id, committed_only=True)

    def check_integrity(self, *, run_id=None):
        return check_artifacts(self.uow_factory, self.backend, run_id=run_id)

    def diagnostics(self, artifact_id):
        with self.uow_factory() as uow:
            item = uow.artifacts.get(artifact_id)
        if item is None: return {"readable": False, "reason": "not_found"}
        if item.status.value != "committed": return {"readable": False, "reason": item.status.value}
        valid = self.backend.verify(item.blob_key, item.checksum, item.size_bytes)
        return {"readable": valid, "reason": "ok" if valid else "blob_invalid"}

    def new_run_input_ingress(self, *, run_id, policies, secret_values=()):
        return RunInputIngressSession(
            self.backend, run_id=run_id, policies=policies,
            secret_values=secret_values,
        )
