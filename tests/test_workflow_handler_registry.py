from __future__ import annotations

import unittest

from orbit.workflow.catalogs import HandlerManifest, InMemoryHandlerCatalog
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


def agent_manifest(version="1.0.0"):
    return HandlerManifest(
        "agent.codex", version, ("action",), {}, {},
        {"type": "object"}, ExecutionSafety.UNKNOWN_ON_LEASE_LOSS,
        ResourceProfile(0, 0, 0, 60, 0, "agent"), "schema://object/1.0",
        ("agent.invoke",),
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

    def test_a_cli_upgrade_resolves_because_the_build_is_not_identity(self):
        """A newer build with the same contract answers for the published one.

        Not an Agent-shaped exception: the fingerprint stopped covering the
        version, so this is how every Handler kind now survives a release.
        """

        registry = ExecutionRegistry()
        installed = registry.register(
            agent_manifest("1.1.7"), _Handler(), implementation_id="agent.codex.1.1.7",
        )
        registry.seal()
        published = agent_manifest("1.1.5")

        self.assertEqual(installed.manifest.fingerprint, published.fingerprint)
        self.assertEqual(installed, registry.resolve(
            "agent.codex", "1.1.5",
            expected_manifest_fingerprint=published.fingerprint,
        ))

    def test_ignoring_the_build_is_not_licence_to_skip_the_contract(self):
        """Agents were exempt from the fingerprint check outright. They are not.

        Dropping the version from identity is only safe while the thing that
        replaced it is still enforced, so the case that used to pass here — a
        fingerprint that matches nothing — must now fail.
        """

        registry = ExecutionRegistry()
        registry.register(
            agent_manifest("1.1.7"), _Handler(), implementation_id="agent.codex",
        )
        registry.seal()

        with self.assertRaises(HandlerContractMismatchError):
            registry.resolve(
                "agent.codex", "1.1.5",
                expected_manifest_fingerprint="sha256:" + "0" * 64,
            )

    def test_a_fingerprint_recorded_before_the_version_left_it_still_resolves(self):
        """A published WorkflowVersion is immutable, so its old value must work.

        A database trigger forbids rewriting one, which means every Workflow
        published before the fingerprint changed shape names a value this
        Runtime can no longer compute from its manifest alone.
        """

        registry = ExecutionRegistry()
        installed = registry.register(
            agent_manifest("1.1.7"), _Handler(), implementation_id="agent.codex",
        )
        registry.seal()
        legacy = installed.manifest.legacy_fingerprint

        self.assertNotEqual(legacy, installed.manifest.fingerprint)
        self.assertEqual(installed, registry.resolve(
            "agent.codex", "1.1.7", expected_manifest_fingerprint=legacy,
        ))


class CatalogResolutionTests(unittest.TestCase):
    """Which manifest a DSL declaration selects, at compile time.

    Separate from the execution registry above: this is the step that turns
    what an author wrote into the exact build recorded in the IR, and it is the
    last place a release number could still refuse a Workflow.
    """

    def test_a_constraint_that_can_be_satisfied_is_honoured(self) -> None:
        catalog = InMemoryHandlerCatalog([manifest("1.0.0"), manifest("1.2.0")])

        self.assertEqual("1.2.0", catalog.resolve("transform.identity", "^1.0").version)
        self.assertEqual("1.0.0", catalog.resolve("transform.identity", "1.0.0").version)

    def test_an_agent_pinned_to_a_build_that_is_gone_still_resolves(self) -> None:
        """The whole point: last month's Workflow still compiles today.

        The author named the CLI release that happened to be installed when
        they wrote it. Refusing them for that would mean editing an old
        Workflow starts by working out which release it was born on.
        """

        catalog = InMemoryHandlerCatalog([agent_manifest("1.1.7")])

        self.assertEqual("1.1.7", catalog.resolve("agent.codex", "1.1.5").version)

    def test_the_newest_installed_agent_build_is_the_one_selected(self) -> None:
        catalog = InMemoryHandlerCatalog([
            agent_manifest("1.1.5"), agent_manifest("2.0.0"), agent_manifest("1.9.0"),
        ])

        self.assertEqual("2.0.0", catalog.resolve("agent.codex", "0.1.0").version)

    def test_a_first_party_pin_that_misses_is_still_drift(self) -> None:
        """Only Agents get this. A transform's version moves with this repo.

        Silently substituting one there would hide a real mismatch behind a
        successful compile, which is the opposite of what the pin is for.
        """

        catalog = InMemoryHandlerCatalog([manifest("1.0.0")])

        self.assertIsNone(catalog.resolve("transform.identity", "2.0.0"))

    def test_a_name_nobody_installed_still_resolves_to_nothing(self) -> None:
        catalog = InMemoryHandlerCatalog([agent_manifest("1.1.7")])

        self.assertIsNone(catalog.resolve("agent.nobody", "1.1.7"))


if __name__ == "__main__": unittest.main()
