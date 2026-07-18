from pathlib import Path
import io
import tempfile
import unittest

from orbit.workflow.artifacts.local_cas import BlobIntegrityError, LocalCASBackend


class LocalCASBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend = LocalCASBackend(Path(self.temp.name) / "artifacts")

    def tearDown(self): self.temp.cleanup()

    def test_streaming_write_deduplicates_and_verifies(self):
        one = self.backend.write_stream(io.BytesIO(b"abc"), max_size_bytes=3)
        two = self.backend.write(b"abc", max_size_bytes=3)
        self.assertEqual(one, two)
        self.assertEqual(b"abc", self.backend.read(one.blob_key))
        self.assertTrue(self.backend.verify(one.blob_key, one.checksum, 3))
        self.assertEqual((one.blob_key,), self.backend.list_blob_keys())

    def test_size_and_path_are_fail_closed(self):
        with self.assertRaises(ValueError): self.backend.write(b"abcd", max_size_bytes=3)
        with self.assertRaises(ValueError): self.backend.read("../../etc/passwd")
        self.assertEqual((), self.backend.list_blob_keys())

    def test_corruption_is_detected(self):
        receipt = self.backend.write(b"abc", max_size_bytes=3)
        self.backend._path(receipt.blob_key).write_bytes(b"abd")
        with self.assertRaises(BlobIntegrityError): self.backend.read(receipt.blob_key)

    def test_fault_before_rename_leaves_no_final_blob(self):
        def fail(point):
            if point == "before_artifact_rename": raise RuntimeError("kill")
        backend = LocalCASBackend(Path(self.temp.name) / "fault", fault_hook=fail)
        with self.assertRaises(RuntimeError): backend.write(b"abc", max_size_bytes=3)
        self.assertEqual((), backend.list_blob_keys())

    def test_fault_after_rename_leaves_only_a_valid_orphan(self):
        def fail(point):
            if point == "after_artifact_rename": raise RuntimeError("kill")
        backend = LocalCASBackend(Path(self.temp.name) / "after", fault_hook=fail)
        with self.assertRaises(RuntimeError): backend.write(b"abc", max_size_bytes=3)
        key = backend.list_blob_keys()[0]
        self.assertEqual(b"abc", backend.read(key))

    def test_symlink_bucket_escape_is_rejected(self):
        receipt = self.backend.write(b"abc", max_size_bytes=3)
        bucket = self.backend._path(receipt.blob_key).parent
        for item in bucket.iterdir(): item.unlink()
        bucket.rmdir()
        outside = Path(self.temp.name) / "outside"; outside.mkdir()
        bucket.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError): self.backend.read(receipt.blob_key)
