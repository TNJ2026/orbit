"""Content-addressed Artifact storage and scoped access."""

from .backend import BlobReceipt
from .local_cas import LocalCASBackend
from .access import ArtifactAccessDenied, ScopedArtifactAccess
from .integrity import ArtifactIntegrityIssue, check_artifacts
from .gc import ArtifactGarbageCollector, GCReport

__all__ = [
    "BlobReceipt", "LocalCASBackend", "ArtifactAccessDenied",
    "ScopedArtifactAccess", "ArtifactIntegrityIssue", "check_artifacts",
    "ArtifactGarbageCollector", "GCReport",
]
