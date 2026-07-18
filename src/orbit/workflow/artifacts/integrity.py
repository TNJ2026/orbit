"""Cross-check committed Artifact metadata against CAS content."""

from dataclasses import dataclass

from ..domain.data import ArtifactLinkType, ArtifactStatus


@dataclass(frozen=True)
class ArtifactIntegrityIssue:
    code: str
    artifact_id: object
    message: str


def check_artifacts(uow_factory, backend, *, run_id=None):
    issues = []
    with uow_factory() as uow:
        records = (
            uow.artifacts.list_by_run(run_id) if run_id is not None
            else uow.artifacts.list_all(limit=100_000)
        )
        for artifact in records:
            if artifact.status is not ArtifactStatus.COMMITTED: continue
            if not backend.verify(artifact.blob_key, artifact.checksum, artifact.size_bytes):
                issues.append(ArtifactIntegrityIssue("artifact_blob_invalid", artifact.artifact_id, "Blob missing, corrupt, or wrong size"))
            producers = uow.artifact_links.list_for_artifact(artifact.artifact_id, link_type=ArtifactLinkType.PRODUCER)
            if len(producers) != 1:
                issues.append(ArtifactIntegrityIssue("artifact_producer_invalid", artifact.artifact_id, "committed Artifact must have exactly one producer"))
    return tuple(issues)
