"""Two-phase, grace-period Artifact garbage collection."""

from dataclasses import dataclass


@dataclass(frozen=True)
class GCReport:
    abandoned_artifact_ids: tuple[object, ...]
    deleted_blob_keys: tuple[str, ...]
    dry_run: bool


class ArtifactGarbageCollector:
    def __init__(self, uow_factory, backend) -> None:
        self.uow_factory, self.backend = uow_factory, backend

    def collect(self, *, staged_before, dry_run=True, limit=100):
        with self.backend.mutation_lock():
            with self.uow_factory() as uow:
                expired = uow.artifacts.list_staged_before(staged_before, limit=limit)
                committed = uow.artifacts.committed_blob_keys()
                if not dry_run:
                    for artifact in expired: uow.artifacts.abandon(artifact.artifact_id)
                    uow.commit()
            candidates = {item.blob_key for item in expired if item.blob_key not in committed}
            deleted = []
            if not dry_run:
                # Lock spans the reference recheck and delete, so a CAS dedupe
                # writer cannot publish a new staged reference in this gap.
                with self.uow_factory() as uow: retained = uow.artifacts.retained_blob_keys()
                for key in sorted(candidates - retained):
                    if self.backend.delete(key): deleted.append(key)
        return GCReport(tuple(item.artifact_id for item in expired), tuple(deleted), dry_run)

    def orphaned_blob_keys(self):
        with self.uow_factory() as uow:
            retained = uow.artifacts.retained_blob_keys()
        return tuple(sorted(set(self.backend.list_blob_keys()) - retained))
