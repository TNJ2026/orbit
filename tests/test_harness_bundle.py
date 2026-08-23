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
        self.assertIn("capabilities.orbit_version", client)
        self.assertIn("capabilities.integration_protocol", client)
        self.assertIn("复制启动命令", client)
        self.assertIn("刷新连接", client)
        self.assertIn("sessionStorage", client)
        self.assertIn("event.key === 'Escape'", client)
        self.assertIn("aria-modal", client)
        self.assertIn("查看原始输出", client)
        self.assertIn("需要人工核对外部 Agent 结果", client)
        self.assertIn("确认外部执行成功", client)
        self.assertIn("复制 ID", client)
        self.assertIn("刷新核对状态", client)
        self.assertIn("@Remote('reconcileDelegation')", (BUNDLE / "src" / "index.ts").read_text(encoding="utf-8"))

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

    def test_p4_workspace_catalog_authoring_import_and_diagnostics_are_wired(self) -> None:
        remote = (BUNDLE / "src" / "index.ts").read_text(encoding="utf-8")
        client = (BUNDLE / "src" / "client.ts").read_text(encoding="utf-8")
        gateway = (BUNDLE / "src" / "gateway.ts").read_text(encoding="utf-8")
        for method in (
            "listWorkflows", "listRuns", "generateWorkflow", "modifyWorkflow",
            "getAuthoringJob", "importArtifact", "getDiagnostics",
        ):
            self.assertIn(f"@Remote('{method}')", remote)
        self.assertIn("settings.section", client)
        self.assertIn("Workflow Catalog", client)
        self.assertIn("Run 历史", client)
        self.assertIn("导入 Attachment", client)
        self.assertIn("下载诊断包", client)
        self.assertIn("diagnostics()", gateway)
        self.assertIn("startWorkflowSelection", remote)
        self.assertIn("cancelWorkflowSelection", remote)
        self.assertIn("beginWorkflowSelection", remote)
        self.assertIn("getPendingWorkflowSelection", remote)
        self.assertIn("registerOrbitSlashSource", client)
        self.assertIn("OrbitCommandView", client)
        self.assertIn("conversation.chat.commandview", client)
        self.assertIn("选择 Orbit Workflow", client)
        self.assertTrue((BUNDLE / "src" / "orbit-command.ts").exists())

        built_client = (BUNDLE / "lib" / "client.js").read_text(encoding="utf-8")
        self.assertIn("window.__ModuleLoader__.load({", built_client)
        self.assertIn('id: "@orbit-runtime/dsh-orbit"', built_client)


if __name__ == "__main__":
    unittest.main()
