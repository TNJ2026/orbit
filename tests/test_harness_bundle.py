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

    def test_patch_registers_the_host_gateway(self) -> None:
        patch = yaml.safe_load((BUNDLE / "cordis.patch.yml").read_text(encoding="utf-8"))
        plugin = patch[0]["insert"][0]
        self.assertEqual("@orbit-runtime/dsh-orbit", plugin["name"])
        self.assertEqual("orbit", plugin["id"])

    def test_host_sources_include_gateway_and_remote_contract(self) -> None:
        gateway = (BUNDLE / "src" / "gateway.ts").read_text(encoding="utf-8")
        remote = (BUNDLE / "src" / "index.ts").read_text(encoding="utf-8")
        self.assertIn("--actor-prefix', 'harness:session:", gateway)
        self.assertIn("@Remote('getRuntime')", remote)
        self.assertIn("@Remote('executeCommand')", remote)
        self.assertIn("@Remote('getGraph')", remote)
        self.assertIn("@Remote('readOutput')", remote)
        self.assertIn("@Remote('listArtifacts')", remote)
        self.assertIn("@Remote('getArtifactContent')", remote)
        self.assertIn("allowed_commands.find", remote)

    def test_p1_bridge_and_run_card_sources_are_shipped(self) -> None:
        manifest = json.loads((BUNDLE / "package.json").read_text(encoding="utf-8"))
        self.assertIn("lib/**/*.js", manifest["files"])
        bridge = (BUNDLE / "src" / "session-bridge.ts").read_text(encoding="utf-8")
        reducer = (BUNDLE / "src" / "run-card.ts").read_text(encoding="utf-8")
        self.assertIn("list_runtime_events", bridge)
        self.assertIn("sourcePosition", bridge)
        self.assertIn("previous.sourcePosition >= event.sourcePosition", reducer)

    def test_p2_detail_human_command_and_settings_are_wired(self) -> None:
        client = (BUNDLE / "src" / "client.ts").read_text(encoding="utf-8")
        self.assertIn("role: 'dialog'", client)
        self.assertIn("remote.orbit.readOutput", client)
        self.assertIn("langgraph_run.resume", client)
        self.assertIn("settings.general.item", client)
        self.assertIn("sessionStorage", client)
        self.assertIn("event.key === 'Escape'", client)
        self.assertIn("aria-modal", client)
        self.assertIn("查看原始输出", client)
        self.assertIn("需要人工核对外部 Agent 结果", client)

    def test_p3_delegation_worker_uses_native_subagent_runtime(self) -> None:
        remote = (BUNDLE / "src" / "index.ts").read_text(encoding="utf-8")
        manifest = json.loads((BUNDLE / "package.json").read_text(encoding="utf-8"))
        self.assertIn("configure_execution_lease", remote)
        self.assertIn("claim_delegation", remote)
        self.assertIn("subagents.start", remote)
        self.assertIn("renew_delegation", remote)
        self.assertIn("cancel_requested", remote)
        self.assertIn("@deepseek-ai/dsh-subagent", manifest["peerDependencies"])
        effects = (BUNDLE / "src" / "effects.ts").read_text(encoding="utf-8")
        self.assertIn("--porcelain=v1", effects)
        self.assertIn("effectManifest", remote)
        self.assertIn("delegationRefusal(workspace, delegation, subagents.list())", remote)
        policy = (BUNDLE / "src" / "delegation-policy.ts").read_text(encoding="utf-8")
        self.assertIn("registeredProviders.includes", policy)
        self.assertIn("write delegation refused", policy)


if __name__ == "__main__":
    unittest.main()
