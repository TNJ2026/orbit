from __future__ import annotations

import unittest

from orbit.workflow.catalogs import HandlerManifest
from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.handlers import (
    CancelAck, CancelDisposition, HandlerValidationResult, PreparedExecution,
    RawHandlerResult, RecoveryDisposition, RecoveryResult, ResourceProfile,
)
from orbit.workflow.domain.schemas import validate_contract
from orbit.workflow.domain.serialization import to_primitive
from orbit.workflow.handlers.registry import (
    ExecutionRegistry, HandlerContractMismatchError, HandlerNotAvailableError,
)


class _Handler:
    def validate(self, manifest, config): return HandlerValidationResult()
    def prepare(self, request, context): return PreparedExecution({})
    def execute(self, prepared, context): return RawHandlerResult({}, None)
    def cancel(self, execution_ref, context): return CancelAck(CancelDisposition.NOT_SUPPORTED)
    def recover(self, recovery_ref, context): return RecoveryResult(RecoveryDisposition.NOT_FOUND)
    def normalize_result(self, raw, context): raise NotImplementedError


def manifest(version="1.0.0"):
    return HandlerManifest(
        "transform.identity", version, ("action",), {}, {},
        {"type": "object"}, ExecutionSafety.REPLAY_SAFE,
        ResourceProfile(0, 0, 0, 60, 0, "free"), "schema://object/1.0",
    )


class HandlerRegistryTests(unittest.TestCase):
    def test_manifest_is_versioned_validated_and_fingerprinted(self):
        value = manifest()
        validate_contract(to_primitive(value), "handler-manifest/1.0")
        self.assertTrue(value.fingerprint.startswith("sha256:"))
        with self.assertRaisesRegex(ValueError, "hard limit"):
            ResourceProfile(0, 0, 0, 86_401, 0, "free")

    def test_registry_requires_exact_version_and_seal(self):
        registry = ExecutionRegistry()
        entry = registry.register(manifest(), _Handler(), implementation_id="builtin.identity.v1")
        with self.assertRaises(RuntimeError): registry.resolve("transform.identity", "1.0.0")
        fingerprint = registry.seal()
        self.assertEqual(entry, registry.resolve("transform.identity", "1.0.0"))
        self.assertEqual(fingerprint, registry.seal())
        with self.assertRaises(ValueError): registry.resolve("transform.identity", "^1.0")
        with self.assertRaises(RuntimeError):
            registry.register(manifest("1.0.1"), _Handler(), implementation_id="new")

    def test_missing_duplicate_and_manifest_drift_fail_closed(self):
        registry = ExecutionRegistry()
        value = manifest()
        registry.register(value, _Handler(), implementation_id="identity")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            registry.register(value, _Handler(), implementation_id="other")
        registry.seal()
        with self.assertRaises(HandlerNotAvailableError):
            registry.resolve("missing", "1.0.0")
        with self.assertRaises(HandlerContractMismatchError):
            registry.resolve(
                value.name, value.version,
                expected_manifest_fingerprint="sha256:" + "0" * 64,
            )


if __name__ == "__main__": unittest.main()
