from __future__ import annotations

import json
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "integrations" / "deepseek-harness"


class DeepSeekHarnessBundleTests(unittest.TestCase):
    def test_manifest_declares_the_profile_patch(self) -> None:
        manifest = json.loads((BUNDLE / "package.json").read_text(encoding="utf-8"))
        self.assertEqual("./cordis.patch.yml", manifest["dsh"]["bundle"]["patch"])
        self.assertEqual("lib/index.js", manifest["main"])
        self.assertNotIn("private", manifest)
        self.assertIn("@deepseek-ai/dsh-typert-protocol", manifest["peerDependencies"])
        self.assertIn("@deepseek-ai/dsh-attachment", manifest["peerDependencies"])

    def test_patch_registers_the_host_gateway(self) -> None:
        patch = yaml.safe_load((BUNDLE / "cordis.patch.yml").read_text(encoding="utf-8"))
        plugin = patch[0]["insert"][0]
        self.assertEqual("@orbit-runtime/dsh-orbit", plugin["name"])
        self.assertEqual("orbit", plugin["id"])

    def test_host_sources_include_gateway_and_remote_contract(self) -> None:
        gateway = (BUNDLE / "src" / "gateway.ts").read_text(encoding="utf-8")
        remote = (BUNDLE / "src" / "index.ts").read_text(encoding="utf-8")
        self.assertIn("'runtimes', '--json'", gateway)
        self.assertIn("entry.project_root === workspaceRoot", gateway)
        self.assertIn("entry.mcp_url", gateway)
        self.assertIn("'x-orbit-actor': actor", gateway)
        self.assertIn("process.env.ORBIT_RUNTIME_ROOT", gateway)
        self.assertNotIn("'mcp', '--transport'", gateway)
        self.assertIn("@Remote('getRuntime')", remote)
        self.assertIn("@Remote('executeCommand')", remote)
        self.assertIn("@Remote('getGraph')", remote)
        self.assertIn("@Remote('readOutput')", remote)
        self.assertIn("@Remote('listArtifacts')", remote)
        self.assertIn("@Remote('getArtifactContent')", remote)
        self.assertIn("allowed_commands.find", remote)

    def test_the_bridge_still_records_runs_after_the_views_went_away(self) -> None:
        """The Run Card is gone; the record it was drawn from is not.

        Session events outlived the views because they are the durable account
        of what ran, not a projection kept for one renderer.
        """

        manifest = json.loads((BUNDLE / "package.json").read_text(encoding="utf-8"))
        self.assertIn("lib/**/*.js", manifest["files"])
        bridge = (BUNDLE / "src" / "session-bridge.ts").read_text(encoding="utf-8")
        self.assertIn("list_runtime_events", bridge)
        self.assertIn("sourcePosition", bridge)
        self.assertIn("orbit/run-started", (BUNDLE / "src" / "types.ts").read_text(encoding="utf-8"))
        self.assertFalse((BUNDLE / "src" / "run-card.ts").exists())

    def test_the_client_shows_orbits_page_and_never_redraws_it(self) -> None:
        """One argument-free command, and a frame around Orbit's own UI.

        Every earlier version of this module drew Orbit's data itself. The
        panel exists so there is one interface rather than two, which holds
        only as long as nothing here reads what that interface already shows.
        """

        client = (BUNDLE / "src" / "client.ts").read_text(encoding="utf-8")
        self.assertIn("getRuntimeUi", client)
        self.assertIn("createElement('iframe'", client)
        self.assertIn("takes no argument", client)
        for redrawn in ("listRuns", "getSteps", "listArtifacts", "executeCommand"):
            self.assertNotIn(redrawn, client)
        remote = (BUNDLE / "src" / "index.ts").read_text(encoding="utf-8")
        self.assertIn("@Remote('getRuntimeUi')", remote)
        self.assertIn("@Remote('reconcileDelegation')", remote)

    def test_agent_tools_use_independent_runtime_mcp(self) -> None:
        remote = (BUNDLE / "src" / "index.ts").read_text(encoding="utf-8")
        tools = (BUNDLE / "src" / "orbit-tools.ts").read_text(encoding="utf-8")
        manifest = json.loads((BUNDLE / "package.json").read_text(encoding="utf-8"))
        self.assertIn("new OrbitToolBridge(ctx, this.gateway).register()", remote)
        for name in (
            "orbit_list_workflows", "orbit_list_runs", "orbit_inspect_run",
            "orbit_start_run", "orbit_cancel_run", "orbit_resume_run",
        ):
            self.assertIn(name, tools)
        self.assertIn("exec.agent?.session", tools)
        self.assertIn("wait: false", tools)
        self.assertIn("run.allowed_commands.find", tools)
        self.assertNotIn("configure_execution_lease", remote)
        self.assertNotIn("claim_delegation", remote)
        self.assertNotIn("@deepseek-ai/dsh-subagent", manifest["peerDependencies"])
        for removed in ("effects.ts", "delegation-execution.ts", "delegation-policy.ts"):
            self.assertFalse((BUNDLE / "src" / removed).exists())

    def test_the_host_surface_outlived_the_views_that_called_it(self) -> None:
        """Catalog, authoring, import and diagnostics are still reachable.

        They were reached through panels that no longer exist, which says
        nothing about whether the operations should: a caller with the Host API
        can still perform every one of them.
        """

        remote = (BUNDLE / "src" / "index.ts").read_text(encoding="utf-8")
        gateway = (BUNDLE / "src" / "gateway.ts").read_text(encoding="utf-8")
        for method in (
            "listWorkflows", "listRuns", "generateWorkflow", "modifyWorkflow",
            "getAuthoringJob", "importArtifact", "getDiagnostics", "getRuntimeUi",
        ):
            self.assertIn(f"@Remote('{method}')", remote)
        self.assertIn("diagnostics()", gateway)
        # The Workflow picker's server half went with the picker: nothing can
        # produce the command event it read.
        for gone in (
            "startWorkflowSelection", "cancelWorkflowSelection",
            "beginWorkflowSelection", "getPendingWorkflowSelection",
        ):
            self.assertNotIn(gone, remote)
        self.assertFalse((BUNDLE / "src" / "orbit-command.ts").exists())

        built_client = (BUNDLE / "lib" / "client.js").read_text(encoding="utf-8")
        self.assertIn("window.__ModuleLoader__.load({", built_client)
        self.assertIn('id: "@orbit-runtime/dsh-orbit"', built_client)


if __name__ == "__main__":
    unittest.main()
