from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from orbit.workflow.artifacts.access import ArtifactAccessDenied, ScopedArtifactAccess
from orbit.workflow.artifacts.local_cas import LocalCASBackend
from orbit.workflow.domain.data import ArtifactVisibility, PortDataPolicy, PortTransport
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.persistence.memory import MemoryRuntimeDatabase, MemoryUnitOfWork


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


class ScopedArtifactAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.backend = LocalCASBackend(Path(self.temp.name))
        self.database = MemoryRuntimeDatabase(); self.factory = lambda: MemoryUnitOfWork(self.database)
        self.access = ScopedArtifactAccess(
            self.backend, self.factory, run_id=EntityId("run", "r"),
            workflow_id=EntityId("workflow", "w"), node_run_id=EntityId("node_run", "n"),
            attempt_id=EntityId("attempt", "a"), output_policies={
                "report": ("example://bytes/1.0", PortDataPolicy(
                    PortTransport.ARTIFACT_REF, 16, ("text/plain",), ArtifactVisibility.RUN,
                ))
            }, secret_values=("top-secret",), clock=lambda: NOW,
        )

    def tearDown(self): self.temp.cleanup()

    def test_writer_derives_metadata_and_deduplicates(self):
        first = self.access.write(name="report", content=b"hello", content_type="text/plain")
        self.assertEqual(first, self.access.write(name="report", content=b"hello", content_type="text/plain"))
        self.assertEqual((first,), self.access.produced_artifact_ids)
        self.assertEqual("staged", self.database.artifacts.get(first).status.value)

    def test_open_writer_streams_and_commits_on_clean_exit(self):
        writer = self.access.open_writer(name="report", content_type="text/plain")
        with writer as stream:
            stream.write(b"hel"); stream.write(b"lo")
        metadata = self.database.artifacts.get(writer.artifact_id)
        self.assertEqual(b"hello", self.backend.read(metadata.blob_key))

    def test_writer_and_reader_fail_closed(self):
        with self.assertRaises(ArtifactAccessDenied): self.access.write(name="other", content=b"x", content_type="text/plain")
        with self.assertRaises(ArtifactAccessDenied): self.access.write(name="report", content=b"x", content_type="image/png")
        with self.assertRaises(ValueError): self.access.write(name="report", content=b"top-secret", content_type="text/plain")
        with self.assertRaises(ArtifactAccessDenied): self.access.read(EntityId("artifact", "known-but-unbound"))
