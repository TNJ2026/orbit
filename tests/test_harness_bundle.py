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
        # The property, not its old address: every mutation re-reads the Run
        # and matches the command against what Orbit advertises at the exact
        # revision the caller was reading. It now lives in one shared helper
        # rather than being spelled twice.
        commands = (BUNDLE / "src" / "commands.ts").read_text(encoding="utf-8")
        self.assertIn("allowed_commands.find", commands)
        self.assertIn("item.expected_version === expectedRevision", commands)
        self.assertEqual(2, remote.count("advertisedAt("))

    def test_no_orbit_event_type_is_written_into_a_Session_log(self) -> None:
        """The Harness refuses a log carrying a type it does not know.

        `orbit/run-started` and its siblings are not in the Harness's own event
        vocabulary, and `Session.append` offers no way to set the envelope's
        `ignorable` marker — the one thing that lets a reader skip an
        unrecognized type. Writing them made every Session that ran a Workflow
        unreadable on reload:

            contains event type "orbit/run-started" (seq 964) unknown to this
            harness and not marked ignorable; refusing to interpret the log

        So the Bridge is not started. What guards that is the call site, not
        the appending code: `bridgeSession` still exists, still writes the
        events, and is the thing to call again once the Harness can take them.
        """

        remote = (BUNDLE / "src" / "index.ts").read_text(encoding="utf-8")
        started = remote[remote.index("private startSessionBridge"):]
        started = started[:started.index("\n  }")]
        self.assertNotIn("bridgeSession", started,
                         "starting a Session must not start the event Bridge")
        self.assertIn("bindSessionWorkspace", started)
        # The binding is not incidental: the model's catalog contribution reads
        # it, so a Session that stops being bound stops being told what can run.
        binding = remote[remote.index("private async bindSessionWorkspace"):]
        self.assertIn("this.bridgedWorkspaces.set", binding[:binding.index("\n  }")])
        self.assertIn("this.bridgedWorkspaces.values()", remote)

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

    def test_the_panel_stops_where_orbits_own_UI_begins(self) -> None:
        """A resident panel of Runs, and a link out for everything deeper.

        The boundary is the whole reason this is not the 685-line duplicate it
        replaced: graphs and Artifacts are drawn by Orbit, and reading their
        data here is how a panel becomes a second Orbit.

        Authoring sits on the boundary rather than beyond it, and the line is
        between asking and editing. `/orbit-generate` starts a job and the
        Workflows page follows it, because a job somebody started from this
        input box is news about this Workspace. Changing a published Workflow
        is the other thing: that is the authoring surface, Orbit draws all of
        it, and a second half-copy here would be the duplicate again.
        """

        client = (BUNDLE / "src" / "client").glob("*.ts*")
        code = "\n".join(path.read_text(encoding="utf-8") for path in client)
        self.assertIn("shell.overlay", code)
        self.assertIn("getPanelState", code)
        self.assertIn("--dsw-alias-", (BUNDLE / "src" / "client" / "OrbitPanel.module.css").read_text(encoding="utf-8"))
        # The one authoring entrance, and it is Session-scoped: the panel never
        # names a Workspace, so a command that could start a job anywhere would
        # be a capability the rest of this surface deliberately does not have.
        self.assertIn("generateWorkflowForSession", code)
        # Job state arrives folded into the panel poll; the console output of a
        # job already on the page is the only authoring read of its own.
        self.assertIn("getAuthoringOutput", code)
        for elsewhere in (
            "getGraph", "listArtifacts", "modifyWorkflow", "getAuthoringJob",
        ):
            self.assertNotIn(elsewhere, code)
        remote = (BUNDLE / "src" / "index.ts").read_text(encoding="utf-8")
        self.assertIn("@Remote('getPanelState')", remote)
        self.assertIn("@Remote('reconcileDelegation')", remote)
        self.assertIn("@Remote('generateWorkflowForSession')", remote)

    def test_agent_tools_use_independent_runtime_mcp(self) -> None:
        remote = (BUNDLE / "src" / "index.ts").read_text(encoding="utf-8")
        tools = (BUNDLE / "src" / "orbit-tools.ts").read_text(encoding="utf-8")
        manifest = json.loads((BUNDLE / "package.json").read_text(encoding="utf-8"))
        # Built on the Gateway, which is the point — the model's tools reach the
        # Runtime's own MCP rather than borrowing the Host's Session remotes.
        self.assertRegex(
            remote, r"new OrbitToolBridge\(ctx, this\.gateway[\s\S]{0,200}?\)\.register\(\)",
        )
        # A job the model starts is told to the panel as it starts, so a person
        # watching does not have to wait for the Agent's next turn to see it.
        self.assertIn("watchAuthoring", remote)
        self.assertIn("this.watch(workspace", tools)
        # Quoted, so a name survives here only as a declaration. The tools
        # describe each other by name — "poll orbit_get_authoring_job" — and a
        # bare substring is satisfied by that prose long after the tool it
        # names has gone.
        for name in (
            "orbit_list_workflows", "orbit_list_runs", "orbit_inspect_run",
            "orbit_start_run", "orbit_cancel_run", "orbit_resume_run",
            "orbit_generate_workflow", "orbit_get_authoring_job",
        ):
            self.assertIn(f"'{name}'", tools)
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
