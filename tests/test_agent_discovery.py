"""M3.F: trusted Agent CLI discovery.

The security property under test: discovery can only ever produce a manifest
for a CLI named in the in-code allowlist, and no caller-supplied string can
become an executable, an argument or a path.
"""

from __future__ import annotations

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
