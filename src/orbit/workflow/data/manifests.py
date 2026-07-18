"""Build immutable Handler input manifests without retaining a UoW."""

from __future__ import annotations

from ..domain.data import (
    ArtifactVisibility, DataOwnerKind, InputManifest, InputManifestItem, PortTransport,
    SecretRef, ValueCommit,
)


class InputManifestBuilder:
    def __init__(self, uow_factory) -> None:
        self.uow_factory = uow_factory

    def build(self, *, run_id, node_run_id, attempt_id, port_policies, bindings, secrets=None):
        secrets, items = dict(secrets or {}), []
        with self.uow_factory() as uow:
            for port_id, (schema_id, policy) in sorted(port_policies.items()):
                bound = bindings.get(port_id)
                if policy.transport is PortTransport.INLINE:
                    value = uow.values.get(bound) if bound is not None else uow.values.get_by_owner_port(DataOwnerKind.NODE_INPUT, node_run_id, port_id)
                    if value is None: raise KeyError(f"missing input Value: {port_id}")
                    commit = ValueCommit(port_id, schema_id, value.data, value.checksum, value.size_bytes)
                    items.append(InputManifestItem(port_id, policy.transport, schema_id, value=commit))
                elif policy.transport is PortTransport.ARTIFACT_REF:
                    artifact = uow.artifacts.get(bound, committed_only=True)
                    if artifact is None: raise PermissionError(f"Artifact is not committed: {port_id}")
                    if artifact.schema_id != schema_id: raise ValueError("Artifact schema does not match input port")
                    run = uow.runs.get(run_id)
                    allowed = (
                        artifact.visibility is ArtifactVisibility.NODE and artifact.scope_id == node_run_id
                        or artifact.visibility is ArtifactVisibility.RUN and artifact.run_id == run_id
                        or artifact.visibility is ArtifactVisibility.WORKFLOW and run is not None and artifact.workflow_id == run.workflow_id
                    )
                    if not allowed:
                        raise PermissionError("Artifact visibility denies this Input Manifest")
                    if artifact.size_bytes > policy.max_size_bytes:
                        raise ValueError("Artifact exceeds input port size limit")
                    if artifact.content_type not in policy.content_types:
                        raise ValueError("Artifact content type is not allowed by input port")
                    items.append(InputManifestItem(port_id, policy.transport, schema_id, artifact=artifact.ref))
                else:
                    secret = secrets.get(port_id)
                    if not isinstance(secret, SecretRef): raise KeyError(f"missing SecretRef: {port_id}")
                    items.append(InputManifestItem(port_id, policy.transport, schema_id, secret=secret))
        return InputManifest(run_id, node_run_id, attempt_id, tuple(items))
