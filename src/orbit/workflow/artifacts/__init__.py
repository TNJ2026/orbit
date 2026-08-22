"""Content-addressed Artifact storage and scoped access."""

from .backend import BlobReceipt
from .local_cas import LocalCASBackend
from .integrity import ArtifactIntegrityIssue, check_artifacts
from .gc import ArtifactGarbageCollector, GCReport

__all__ = [
    "BlobReceipt", "LocalCASBackend",
    "ArtifactIntegrityIssue", "check_artifacts",
    "ArtifactGarbageCollector", "GCReport",
]
