"""M3.F: trusted Agent CLI discovery.

The security property under test: discovery can only ever produce a manifest
for a CLI named in the in-code allowlist, and no caller-supplied string can
become an executable, an argument or a path.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest

from orbit.workflow.catalogs.agent_discovery import (
    TRUSTED_AGENT_CLIS, AgentCliSpec, AgentDiscoveryError, DiscoveredAgent,
    agent_manifest, catalog_entries, discover_agent_clis, registrable_agents,
)
from orbit.workflow.domain.durable_execution import ExecutionSafety


CLAUDE = AgentCliSpec("claude", "claude")


def fake_which(installed):
    return lambda name: f"/usr/local/bin/{name}" if name in installed else None


def fake_runner(outputs):
    """outputs: executable basename -> (returncode, stdout)."""

    def run(argv, **_kwargs):
        code, text = outputs.get(argv[0].rsplit("/", 1)[-1], (1, ""))
        return SimpleNamespace(returncode=code, stdout=text, stderr="")

    return run


class SpecValidationTests(unittest.TestCase):
    def test_executable_must_be_a_bare_name(self) -> None:
        for bad in ("/usr/bin/claude", "../claude", "claude; rm -rf /", "cl aude"):
            with self.subTest(bad=bad):
                with self.assertRaises(AgentDiscoveryError):
                    AgentCliSpec("claude", bad)

    def test_version_probe_accepts_flags_only(self) -> None:
        with self.assertRaises(AgentDiscoveryError):
            AgentCliSpec("claude", "claude", version_args=("run", "--version"))

    def test_the_allowlist_is_valid(self) -> None:
        self.assertTrue(TRUSTED_AGENT_CLIS)
        for spec in TRUSTED_AGENT_CLIS:
            self.assertEqual(spec.executable, spec.executable.strip())


class DiscoveryTests(unittest.TestCase):
    def test_only_installed_clis_are_reported(self) -> None:
        found = discover_agent_clis(
            TRUSTED_AGENT_CLIS,
            which=fake_which({"claude"}),
            runner=fake_runner({"claude": (0, "claude 2.1.3")}),
        )
        self.assertEqual(["claude"], [agent.name for agent in found])
        self.assertEqual("2.1.3", found[0].version)

    def test_nothing_installed_is_not_an_error(self) -> None:
        self.assertEqual(
            (), discover_agent_clis(TRUSTED_AGENT_CLIS, which=fake_which(set()))
        )

    def test_a_cli_whose_version_cannot_be_read_is_skipped(self) -> None:
        """An unpinned version would make the manifest fingerprint a lie."""

        found = discover_agent_clis(
            (CLAUDE,), which=fake_which({"claude"}),
            runner=fake_runner({"claude": (0, "not a version string")}),
        )
        self.assertEqual((), found)

    def test_a_failing_probe_is_skipped(self) -> None:
        found = discover_agent_clis(
            (CLAUDE,), which=fake_which({"claude"}),
            runner=fake_runner({"claude": (127, "")}),
        )
        self.assertEqual((), found)

    def test_a_crashing_probe_does_not_propagate(self) -> None:
        def explode(*_args, **_kwargs):
            raise OSError("no such file")

        self.assertEqual(
            (),
            discover_agent_clis(
                (CLAUDE,), which=fake_which({"claude"}), runner=explode
            ),
        )

    def test_the_probe_runs_the_resolved_path_with_flags_only(self) -> None:
        seen = {}

        def record(argv, **kwargs):
            seen["argv"] = argv
            seen["env"] = kwargs.get("env")
            return SimpleNamespace(returncode=0, stdout="claude 1.0.0", stderr="")

        discover_agent_clis((CLAUDE,), which=fake_which({"claude"}), runner=record)
        self.assertEqual(["/usr/local/bin/claude", "--version"], seen["argv"])
        # No inherited credentials in a probe.
        self.assertEqual({"PATH", "HOME"}, set(seen["env"]))


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = DiscoveredAgent(CLAUDE, "/usr/local/bin/claude", "2.1.3")

    def test_agent_manifests_are_unknown_on_lease_loss(self) -> None:
        manifest = agent_manifest(self.agent)
        self.assertEqual(
            ExecutionSafety.UNKNOWN_ON_LEASE_LOSS, manifest.execution_safety
        )

    def test_the_cli_version_is_the_handler_version(self) -> None:
        self.assertEqual("2.1.3", agent_manifest(self.agent).version)

    def test_a_different_cli_version_is_a_different_fingerprint(self) -> None:
        other = DiscoveredAgent(CLAUDE, "/usr/local/bin/claude", "2.2.0")
        self.assertNotEqual(
            agent_manifest(self.agent).fingerprint, agent_manifest(other).fingerprint
        )

    def test_the_same_cli_at_the_same_version_is_stable(self) -> None:
        same = DiscoveredAgent(CLAUDE, "/opt/bin/claude", "2.1.3")
        self.assertEqual(
            agent_manifest(self.agent).fingerprint, agent_manifest(same).fingerprint
        )

    def test_the_manifest_config_takes_no_command(self) -> None:
        properties = agent_manifest(self.agent).config_schema["properties"]
        self.assertEqual({"prompt", "timeout_seconds"}, set(properties))
        self.assertFalse(agent_manifest(self.agent).config_schema["additionalProperties"])


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = DiscoveredAgent(CLAUDE, "/usr/local/bin/claude", "2.1.3")

    def test_an_ungranted_capability_never_reaches_the_registry(self) -> None:
        self.assertEqual(
            (), registrable_agents([self.agent], allowed_capabilities=[])
        )

    def test_a_granted_capability_is_registrable(self) -> None:
        pairs = registrable_agents(
            [self.agent], allowed_capabilities=["agent.invoke"]
        )
        self.assertEqual(1, len(pairs))
        self.assertEqual("agent.claude", pairs[0][1].name)


class RegistrationTests(unittest.TestCase):
    """Discovery has to end in a registration, before the registry seals.

    The migration plan's M3 task 17 requires it, and for a while the code did
    the opposite: the composition sealed the registry in its constructor and
    discovery ran afterwards, so an installed agent appeared in the UI catalog
    and could never be invoked by a workflow. "Registered later" is not a
    weaker version of this — a sealed registry cannot be added to at all.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "runtime.db"
        # A real executable: the registry runs each handler's preflight before
        # sealing, and TrustedCliAgentClient refuses a CLI that is not on PATH.
        # That check is the point — a registry must not seal around a handler
        # that cannot run — so the fixture satisfies it rather than mocking it.
        self.executable = shutil.which("true") or "/usr/bin/true"
        self.agent = DiscoveredAgent(CLAUDE, self.executable, "2.1.3")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_a_registry_refuses_to_seal_around_a_missing_cli(self) -> None:
        """Preflight is what keeps "registered" from meaning "unusable"."""

        from orbit.web.app import RuntimeComposition
        from orbit.web.builtin_handlers import BUILTIN_SCHEMAS, agent_handlers

        absent = DiscoveredAgent(CLAUDE, "/nonexistent/claude", "2.1.3")
        registrations, _ = agent_handlers([absent])
        with self.assertRaises(RuntimeError) as caught:
            RuntimeComposition(
                self.db, handlers=registrations, schemas=BUILTIN_SCHEMAS,
            )
        self.assertIn("preflight", str(caught.exception))

    def test_a_discovered_agent_becomes_a_registration(self) -> None:
        from orbit.web.builtin_handlers import agent_handlers

        registrations, names = agent_handlers([self.agent])
        self.assertEqual(("agent.claude",), names)
        self.assertEqual("agent.claude", registrations[0].manifest.name)

    def test_the_registration_carries_the_discovered_executable(self) -> None:
        """The command is constructor-owned; nothing else may supply it."""

        from orbit.web.builtin_handlers import agent_handlers

        registrations, _ = agent_handlers([self.agent])
        client = registrations[0].implementation.client
        self.assertEqual((self.executable,), client.command)

    def test_an_ungranted_capability_produces_no_registration(self) -> None:
        from orbit.web.builtin_handlers import agent_handlers

        registrations, names = agent_handlers([self.agent], allowed_capabilities=[])
        self.assertEqual((), registrations)
        self.assertEqual((), names)

    def test_the_composition_can_resolve_a_registered_agent(self) -> None:
        """End of the chain: sealed registry, resolvable by fingerprint."""

        from orbit.web.app import RuntimeComposition
        from orbit.web.builtin_handlers import BUILTIN_SCHEMAS, agent_handlers

        registrations, _ = agent_handlers([self.agent])
        composition = RuntimeComposition(
            self.db, handlers=registrations, schemas=BUILTIN_SCHEMAS,
        )
        try:
            self.assertTrue(composition.handler_registry.sealed)
            manifest = registrations[0].manifest
            entry = composition.handler_registry.resolve(
                manifest.name, manifest.version,
                expected_manifest_fingerprint=manifest.fingerprint,
            )
            self.assertEqual(manifest.fingerprint, entry.manifest.fingerprint)
        finally:
            composition.stop()

    def test_discovery_runs_before_the_registry_seals(self) -> None:
        """Ordering, asserted on the real create_app rather than by reading it."""

        from unittest.mock import patch

        from orbit.web.app import create_app
        from orbit.web.builtin_handlers import BUILTIN_SCHEMAS

        with patch(
            "orbit.workflow.catalogs.agent_discovery.discover_agent_clis",
            return_value=(self.agent,),
        ):
            app = create_app(
                self.db, schemas=BUILTIN_SCHEMAS, discover_agents=True,
            )
        composition = app.state.runtime
        try:
            registered = {
                entry.manifest.name for entry in composition.handler_registry.entries()
            }
            self.assertIn(
                "agent.claude", registered,
                "the agent was discovered but never reached the sealed registry",
            )
        finally:
            composition.stop()


class CatalogExposureTests(unittest.TestCase):
    def test_the_executable_path_is_never_exposed(self) -> None:
        entries = catalog_entries(
            [DiscoveredAgent(CLAUDE, "/usr/local/bin/claude", "2.1.3")]
        )
        self.assertEqual(1, len(entries))
        self.assertNotIn("/usr/local/bin/claude", repr(entries))
        self.assertEqual(
            {"name", "agent", "version", "capabilities"}, set(entries[0])
        )


if __name__ == "__main__":
    unittest.main()
