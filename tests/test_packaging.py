"""Guards that the bundled UI asset stays importable/packaged."""

import unittest
from importlib import resources
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


def _write_workflow_config(server, steps, *args, **kwargs):
    configured = [
        {**step, "agents": step.get("agents") or ["codex"]}
        for step in steps
    ]
    return server.write_workflow_config(configured, *args, **kwargs)


class PackagingTests(unittest.TestCase):
    def test_init_project_bootstraps_everything_and_is_idempotent(self):
        import json
        from orbit.__main__ import init_project

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = init_project(root)

            self.assertTrue((root / ".orbit" / "workflow.json").exists())
            self.assertIn(".orbit/tasks/", (root / ".gitignore").read_text(encoding="utf-8"))
            self.assertTrue(first["created"])

            # second run touches nothing
            second = init_project(root)
            self.assertEqual([], second["created"])

    def test_ensure_state_dir_gitignored_adds_entry_once(self):
        from orbit.__main__ import ensure_state_dir_gitignored

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Fresh repo: no .gitignore -> the state dir gets ignored.
            self.assertTrue(ensure_state_dir_gitignored(root))
            gitignore = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertIn(".orbit/", gitignore)
            # Idempotent: a second call is a no-op, no duplicate line.
            self.assertFalse(ensure_state_dir_gitignored(root))
            self.assertEqual(
                1, (root / ".gitignore").read_text(encoding="utf-8").count(".orbit/")
            )

    def test_ensure_state_dir_gitignored_respects_existing_content(self):
        from orbit.__main__ import ensure_state_dir_gitignored

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".gitignore").write_text(".venv/\n", encoding="utf-8")
            self.assertTrue(ensure_state_dir_gitignored(root))
            text = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertEqual(".venv/\n.orbit/\n", text)

    def test_append_missing_gitignore_adds_only_absent_entries(self):
        from orbit.__main__ import append_missing_gitignore

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".gitignore").write_text(".orbit/\n", encoding="utf-8")
            # .orbit/ already present (agents/ isn't) -> only agents/ is added.
            added = append_missing_gitignore(root, [".orbit/", "agents/"])
            self.assertEqual(["agents/"], added)
            text = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertEqual(".orbit/\nagents/\n", text)
            # Trailing-slash-insensitive: `agents` without a slash counts as present.
            (root / ".gitignore").write_text("agents\n", encoding="utf-8")
            self.assertEqual([], append_missing_gitignore(root, ["agents/"]))

    def test_create_server_locals_do_not_shadow_module_functions(self):
        # A closure-local function reusing a module-level name shadows it for
        # every call site inside create_server (this once broke workflow start:
        # a local wrapper shadowed the module-level engine function).
        import ast
        import inspect
        import orbit.server as server

        tree = ast.parse(inspect.getsource(server))
        module_funcs = {
            node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        create = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "create_server"
        )
        inner_funcs = {
            node.name for node in ast.walk(create)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node is not create
        }
        self.assertEqual(set(), module_funcs & inner_funcs)

    def test_ui_asset_is_present_and_loadable(self):
        html = (
            resources.files("orbit")
            .joinpath("static/ui.html")
            .read_text(encoding="utf-8")
        )
        self.assertIn("<!doctype html>", html.lower())
        self.assertIn('id="dbPath"', html)
        self.assertIn("/api/status", html)
        self.assertIn("/api/projects", html)
        self.assertIn('id="agentTools"', html)
        self.assertIn('id="toolsTab"', html)
        self.assertIn('id="workflowTab"', html)
        self.assertIn('id="tasksTab"', html)
        self.assertIn('id="toolsPage"', html)
        self.assertIn('id="workflowPage"', html)
        self.assertIn('id="tasksPage"', html)
        self.assertIn('id="tasksList"', html)
        self.assertIn('id="taskDetails"', html)
        self.assertIn('id="toggleTaskDetails"', html)
        self.assertIn("details-collapsed", html)
        self.assertIn("function toggleTaskDetails()", html)
        self.assertIn("function renderTaskDetails(task)", html)
        self.assertIn("task-detail-body", html)
        self.assertIn('localStorage.getItem("orbit-task-details-collapsed") !== "0"', html)
        self.assertIn('class="side-menu"', html)
        self.assertIn("menu-button", html)
        self.assertIn("display: block;", html)
        self.assertIn("tool.profile_name", html)
        self.assertIn("tool.profile_path", html)
        self.assertIn("function setPage(page)", html)
        self.assertIn("/api/agent-tools", html)
        self.assertIn("/api/workflow", html)
        self.assertIn("/api/tasks?limit=200", html)
        self.assertIn("/api/tasks/${goalId}/status", html)
        self.assertNotIn("function moveTask", html)
        self.assertNotIn("/api/tasks/${taskId}/workflow/start", html)
        self.assertNotIn("/api/tasks/${taskId}/workflow/complete", html)
        self.assertIn("/api/tasks/${taskId}/runs?limit=10", html)
        self.assertIn("/api/task-runs/${runId}/files/${fileKey}?tail=65536", html)
        self.assertIn("function rerunTask(taskId)", html)
        self.assertIn("/api/tasks/${taskId}/rerun", html)
        self.assertIn("function reimplementTask(taskId)", html)
        self.assertIn("/api/tasks/${taskId}/reimplement", html)
        self.assertIn("function goalBudgetAlertHtml(goal, inputId", html)
        self.assertIn("goal.budgetExceeded", html)
        self.assertIn("function resumeGoalBudget(goalId, inputId", html)
        self.assertIn("executionGoalBudgetResume-${goal.id}", html)
        self.assertIn("/api/goals/${goalId}/resume-budget", html)
        self.assertNotIn("function createTaskRun(taskId)", html)
        self.assertIn("function renderTaskRuns()", html)
        self.assertIn("function renderWorkflow()", html)
        # Steps assign their own Agent, with a per-agent command under each row.
        self.assertIn('id="addStepAgents"', html)
        self.assertIn("step-agent-cmd", html)
        self.assertIn("function selectedAgentCommands()", html)
        self.assertNotIn('id="setDefaultCommand"', html)
        self.assertNotIn('id="setSubtaskAgents"', html)
        # The global agent-command overrides setting is gone (moved onto steps).
        self.assertNotIn('id="setAgentCommands"', html)
        self.assertNotIn('id="addStepCommand"', html)
        # Agent dropdowns list only available agents; they cannot be cleared to
        # an explicit "no agent" option in the step editor.
        self.assertNotIn("modal.addStep.agentDefault", html)
        self.assertNotIn("modal.addStep.agentRequired", html)
        # The Add-step toolbar tool opens the modal (add); double-click opens it
        # (edit). No standalone Add-step button in the pane header.
        self.assertIn("function saveEditStep()", html)
        self.assertIn("function confirmAddStep()", html)
        self.assertIn('data-action="add-step"', html)
        self.assertNotIn('id="addWorkflowStep"', html)
        self.assertIn('id="addStepModalBackdrop"', html)
        self.assertIn('id="addStepPrompt"', html)
        self.assertIn('step.prompt = ($("addStepPrompt").value || "").trim()', html)
        self.assertNotIn('id="workflowStatuses"', html)
        self.assertIn("function workflowStatusList()", html)
        self.assertIn("function taskBoardColumns(tasks)", html)
        self.assertIn("function goalStepsHtml(goal)", html)
        self.assertIn('const icon = sub.task_status === "closed" ? "✓ " : "";', html)
        self.assertIn("function currentBoardGoal()", html)
        self.assertIn("function goalFlowHtml(goal)", html)
        self.assertIn("function phaseInstances(goal, phase)", html)
        self.assertIn("function showPhaseDetails(stepId)", html)
        # A workflow phase backed by one subtask opens the task, not its first
        # materialized step card (notably the Implement card).
        self.assertIn("oneInstance.owner.id !== goal.id", html)
        self.assertIn("showTaskDetails(${detailTask.id})", html)
        self.assertIn("if (visibleTasks.length < 2)", html)
        self.assertIn("task.step_inputs", html)
        self.assertIn("task.result_summary", html)
        self.assertIn("task.artifacts", html)
        self.assertIn("task.parent_task_id === goal.id", html)
        self.assertNotIn("function goalFilterId()", html)
        self.assertNotIn("const COLUMN_MAP", html)
        self.assertIn("workflow-canvas", html)
        self.assertIn("workflow-node", html)
        self.assertIn('id="wfArrow"', html)
        self.assertIn('marker-end="url(#wfArrow)"', html)
        self.assertIn("function wfEdgeAnchor(", html)
        self.assertIn("function wfEdgePath(", html)
        self.assertIn('marker-start="url(#wfArrow)"', html)
        # Auto-laid-out editor: dagre computes coordinates, the user only edits
        # the graph. The manual-drag machinery (ports, node/edge dragging) is
        # gone — clicks edit nodes and delete edges.
        self.assertIn('src="/static/dagre.min.js"', html)
        self.assertIn("function wfApplyDagreLayout(", html)
        self.assertIn("function handleNodeClick(", html)
        # Layout is fully automatic (in saveWorkflow) — no manual re-tidy button.
        self.assertNotIn("function autoLayoutWorkflow(", html)
        self.assertNotIn('data-action="auto-layout"', html)
        self.assertIn('t("wf.clickDelete")', html)
        self.assertNotIn("workflow-port", html)
        self.assertNotIn("function startNodeDrag(", html)
        self.assertNotIn("function startExistingEdgeDrag(", html)
        self.assertNotIn("function updateWorkflowEdgeTarget(", html)
        self.assertNotIn("<strong>Assignment</strong>", html)  # Assignment section removed
        self.assertIn("function toggleStep(", html)            # inline-expand step detail
        self.assertIn('class="step-item', html)
        self.assertNotIn('id="jobsTab"', html)                 # jobs page removed
        self.assertNotIn('const REQUIRED_TEAM_ROLES', html)
        self.assertNotIn("function recommendAgent(taskId)", html)
        self.assertNotIn("function renderAssignmentCandidates()", html)
        self.assertNotIn("/api/tasks/${taskId}/assignment-candidates", html)
        self.assertNotIn("Weight", html)
        self.assertIn("selectedProjectId", html)
        self.assertIn("async function refreshWorkspace()", html)
        self.assertIn("function wireRefresh(", html)
        self.assertNotIn("function renderTaskWorkflow(", html)
        self.assertNotIn("function startTaskWorkflow(", html)
        self.assertNotIn("function completeTaskStep(", html)
        for button_id in (
            "refreshTools",
            "refreshWorkflow",
            "refreshTasks",
        ):
            self.assertIn(f'wireRefresh("{button_id}"', html)
        self.assertNotIn('onclick="editRole', html)
        self.assertNotIn('onclick="saveRole', html)
        self.assertNotIn('onclick="cancelEdit', html)
        self.assertNotIn('id="projectSelect"', html)
        self.assertNotIn('id="registerAgent"', html)
        self.assertGreater(len(html), 1000)

    def test_server_module_imports(self):
        # Importing the module reads the UI asset at module load time;
        # a missing resource would raise here.
        import orbit.server as server

        self.assertTrue(server._UI_HTML)

    def test_gitignore_excludes_orbit_state_dirs(self):
        # Orbit does not commit its own state dir: the whole .orbit/ and legacy
        # .dev_loop/ are ignored so a clone falls back to code defaults instead
        # of inheriting a checked-in (and potentially stale) config snapshot.
        lines = Path(".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".orbit/", lines)
        self.assertIn(".dev_loop/", lines)

    def test_agent_tool_detection_shape(self):
        import orbit.server as server

        tools = server.detect_agent_tools()
        self.assertTrue(tools)
        self.assertTrue(
            {"id", "name", "command", "agent_name", "installed", "path"}.issubset(
                tools[0]
            )
        )
        by_id = {tool["id"]: tool for tool in tools}
        self.assertIn("hermes", by_id)
        self.assertNotIn("openclaw", by_id)
        self.assertNotIn("profiles", by_id["hermes"])
        self.assertNotIn("profile_count", by_id["hermes"])

    def test_agent_tool_detection_marks_installed_paths(self):
        import orbit.server as server

        def fake_which(command):
            return f"/bin/{command}" if command == "codex" else None

        with mock.patch("orbit.server.shutil.which", side_effect=fake_which):
            tools = {tool["id"]: tool for tool in server.detect_agent_tools()}

        self.assertTrue(tools["codex"]["installed"])
        self.assertEqual("/bin/codex", tools["codex"]["path"])
        self.assertFalse(tools["claude"]["installed"])
        self.assertIsNone(tools["claude"]["path"])

    def test_hermes_profile_detection_lists_profile_directories(self):
        import orbit.server as server

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manager").mkdir()
            (root / "researcher").mkdir()
            (root / ".hidden").mkdir()
            (root / "notes.txt").write_text("not a profile", encoding="utf-8")

            profiles = server.detect_hermes_profiles(root)

        self.assertEqual(["manager", "researcher"], [p["name"] for p in profiles])

    def test_agent_tool_detection_splits_hermes_profiles_into_agents(self):
        import orbit.server as server

        fake_profiles = [
            {"name": "default", "path": "/tmp/default"},
            {"name": "manager", "path": "/tmp/manager"},
        ]
        with mock.patch("orbit.server.detect_hermes_profiles", return_value=fake_profiles):
            tools = {tool["id"]: tool for tool in server.detect_agent_tools()}

        self.assertIn("hermes", tools)
        self.assertNotIn("profile_name", tools["hermes"])
        self.assertIn("hermes-default", tools)
        self.assertIn("hermes-manager", tools)
        self.assertEqual("Hermes default", tools["hermes-default"]["name"])
        self.assertEqual("hermes-default", tools["hermes-default"]["agent_name"])
        self.assertEqual("default", tools["hermes-default"]["profile_name"])
        self.assertEqual("/tmp/default", tools["hermes-default"]["profile_path"])
        self.assertEqual("hermes --profile manager", tools["hermes-manager"]["command"])

    def test_hermes_profile_agent_ids_are_slugged(self):
        import orbit.server as server

        fake_profiles = [
            {"name": "manager qa", "path": "/tmp/manager-qa"},
            {"name": "manager/qa", "path": "/tmp/manager-qa-2"},
        ]
        with mock.patch("orbit.server.detect_hermes_profiles", return_value=fake_profiles):
            by_id = {tool["id"]: tool for tool in server.detect_agent_tools()}

        self.assertIn("hermes-manager-qa", by_id)
        self.assertIn("hermes-manager-qa-2", by_id)
        self.assertEqual("manager qa", by_id["hermes-manager-qa"]["profile_name"])
        self.assertEqual("manager/qa", by_id["hermes-manager-qa-2"]["profile_name"])

    def test_workflow_config_defaults_and_round_trips_project_file(self):
        import orbit.server as server

        with TemporaryDirectory() as tmp:
            default = server.read_workflow_config(tmp)
            saved = _write_workflow_config(server,
                [
                    {
                        "id": "plan",
                        "name": "Plan",

                        "task_status": "created",
                        "required": True,
                    },
                    {
                        "id": "ship",
                        "name": "Ship",

                        "task_status": "closed",
                    },
                    {
                        "id": "check",
                        "name": "Check",

                        "task_status": "in_progress",
                    },
                ],
                tmp,
            )
            loaded = server.read_workflow_config(tmp)

        self.assertEqual("intake", default["steps"][0]["id"])
        self.assertIn(
            {"value": "assigned", "label": "Assigned"},
            default["statuses"],
        )
        self.assertEqual(saved, loaded)
        self.assertEqual(["plan", "ship", "check"], [step["id"] for step in loaded["steps"]])
        self.assertTrue(loaded["path"].endswith(".orbit/workflow.json"))

    def test_workflow_steps_do_not_configure_task_status(self):
        import orbit.server as server
        from orbit.store import InvalidInputError

        steps = [
            {"id": "a", "name": "A", "task_status": "created"},
            {
                "id": "b",
                "name": "B",

                "task_status": "assigned",
            },
            {"id": "c", "name": "C", "task_status": "in_progress"},
        ]
        with TemporaryDirectory() as tmp:
            saved = _write_workflow_config(server, steps, tmp)
            self.assertEqual(server.default_workflow_statuses(), saved["statuses"])
            by_id = {s["id"]: s for s in saved["steps"]}
            self.assertNotIn("task_status", by_id["b"])

    def test_workflow_allows_step_without_agent(self):
        # Steps default empty; the agent gate is deferred to goal start, so a
        # workflow with no Agents saves and round-trips cleanly.
        import orbit.server as server

        with TemporaryDirectory() as tmp:
            saved = server.write_workflow_config(
                [{"id": "a", "name": "A", "agents": []}], tmp, []
            )
            self.assertEqual([], saved["steps"][0]["agents"])
            self.assertEqual(
                [], server.read_workflow_config(tmp)["steps"][0]["agents"]
            )

    def test_workflow_reports_graph_warnings(self):
        import orbit.server as server

        steps = [
            {"id": "a", "name": "A"},
            {"id": "b", "name": "B"},
            {"id": "c", "name": "C"},
        ]
        with TemporaryDirectory() as tmp:
            connected = _write_workflow_config(server,
                steps, tmp, [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}]
            )
            orphaned = _write_workflow_config(server,
                steps, tmp, [{"from": "a", "to": "b"}]
            )

        self.assertEqual([], connected["warnings"])
        # c has no incoming edge, so it is a second entry point rather than
        # unreachable; a fully disconnected node yields no warning about
        # reachability but b/c both count as terminals -> no warnings there.
        self.assertEqual([], orphaned["warnings"])

    def test_workflow_warns_on_unreachable_steps(self):
        import orbit.server as server

        steps = [
            {"id": "a", "name": "A"},
            {"id": "b", "name": "B"},
            {"id": "c", "name": "C"},
        ]
        # b <-> c form a cycle with no entry from a's component.
        with TemporaryDirectory() as tmp:
            saved = _write_workflow_config(server,
                steps, tmp, [{"from": "b", "to": "c"}, {"from": "c", "to": "b"}]
            )

        self.assertTrue(any("unreachable" in w for w in saved["warnings"]))
        self.assertTrue(any("no path to an end" in w for w in saved["warnings"]))

    def test_integrate_and_decompose_steps_are_always_required_and_locked(self):
        import orbit.server as server

        with TemporaryDirectory() as tmp:
            saved = _write_workflow_config(server,
                [
                    {"id": "impl", "name": "Impl", "required": False},
                    {"id": "split", "name": "Split", "decompose": True, "required": False},
                    {"id": "merge", "name": "Merge", "integrate": True, "required": False},
                    {"id": "qa", "name": "QA", "required": False},
                ],
                tmp,
            )
            loaded = server.read_workflow_config(tmp)

        self.assertEqual(saved, loaded)
        by_id = {step["id"]: step for step in loaded["steps"]}
        # integrate/decompose steps are structural -> always required and locked.
        for locked in ("split", "merge"):
            self.assertTrue(by_id[locked]["required"], locked)
            self.assertTrue(by_id[locked]["required_locked"], locked)
        for free in ("impl", "qa"):
            self.assertFalse(by_id[free]["required"], free)
            self.assertFalse(by_id[free]["required_locked"], free)

    def test_triage_intake_step_is_required_and_locked(self):
        import orbit.server as server

        with TemporaryDirectory() as tmp:
            saved = server.write_workflow_config(
                [{"id": "intake", "name": "Triage", "required": False},
                 {"id": "impl", "name": "Impl", "required": False}],
                tmp, [{"from": "intake", "to": "impl"}],
            )
            intake = {s["id"]: s for s in saved["steps"]}["intake"]
            # Locked even though it was submitted required=False.
            self.assertTrue(intake["required"])
            self.assertTrue(intake["required_locked"])

    def test_default_workflow_is_sequential_with_split_and_loopback(self):
        import orbit.server as server

        edges = server.default_workflow_edges()
        ids = {s["id"] for s in server.default_workflow_steps()}
        # every edge references a real step
        for e in edges:
            self.assertIn(e["from"], ids)
            self.assertIn(e["to"], ids)
        out_of = lambda n: [e["to"] for e in edges if e["from"] == n]
        into = lambda n: [e["from"] for e in edges if e["to"] == n]
        # sequential design chain: product -> ui -> architecture -> decompose
        self.assertEqual(["ui_design"], out_of("product_design"))
        self.assertEqual(["architecture"], out_of("ui_design"))
        self.assertEqual(["decompose"], out_of("architecture"))
        # architecture is decompose's only forward predecessor
        self.assertEqual(["architecture"], into("decompose"))
        # split: subtasks begin at implement, one per module
        self.assertIn("implement", out_of("decompose"))
        # loop-back: review returns to implement
        self.assertIn("implement", out_of("review"))
        # config round-trips through write/read with the branching edges intact
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            loaded = server.read_workflow_config(tmp)
        self.assertEqual(edges, loaded["edges"])

    def test_workflow_persists_positions_and_edges(self):
        import orbit.server as server

        with TemporaryDirectory() as tmp:
            saved = _write_workflow_config(server,
                [
                    {"id": "a", "name": "A", "x": 100, "y": 50},
                    {"id": "b", "name": "B", "x": 400, "y": 200},
                    {"id": "c", "name": "C", "x": 700, "y": 50},
                ],
                tmp,
                [{"from": "a", "to": "b"}],
            )
            loaded = server.read_workflow_config(tmp)

        self.assertEqual(saved, loaded)
        self.assertEqual(100.0, loaded["steps"][0]["x"])
        self.assertEqual(200.0, loaded["steps"][1]["y"])
        self.assertEqual([{"from": "a", "to": "b"}], loaded["edges"])

    def test_workflow_edges_drop_selfloops_and_dupes_and_reject_unknown(self):
        import orbit.server as server
        from orbit.store import InvalidInputError

        steps = [
            {"id": "a", "name": "A"},
            {"id": "b", "name": "B"},
            {"id": "c", "name": "C"},
        ]
        with TemporaryDirectory() as tmp:
            saved = _write_workflow_config(server,
                steps,
                tmp,
                [
                    {"from": "a", "to": "b"},
                    {"from": "a", "to": "b"},  # dupe -> dropped
                    {"from": "a", "to": "a"},  # self-loop -> dropped
                ],
            )
            self.assertEqual([{"from": "a", "to": "b"}], saved["edges"])
            with self.assertRaisesRegex(InvalidInputError, "unknown step"):
                _write_workflow_config(server, steps, tmp, [{"from": "a", "to": "z"}])

    def test_legacy_workflow_without_edges_gets_sequential_chain(self):
        import orbit.server as server
        import json as _json

        with TemporaryDirectory() as tmp:
            cfg = Path(tmp) / ".orbit" / "workflow.json"
            cfg.parent.mkdir(parents=True)
            cfg.write_text(_json.dumps({"steps": [
                {"id": "a", "name": "A"},
                {"id": "b", "name": "B"},
                {"id": "c", "name": "C"},
            ]}))
            loaded = server.read_workflow_config(tmp)
        self.assertEqual(
            [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}], loaded["edges"]
        )

    def test_workflow_steps_have_no_kind_field(self):
        # Every node is a plain step; the normalized config carries no "kind"
        # field. Branching is expressed with edges (parallel/merge) and rework
        # loop-backs.
        import orbit.server as server

        steps = [
            {"id": "intake", "name": "Triage", "required": True},
            {"id": "implement", "name": "Implement", "required": True},
            {"id": "review", "name": "Review", "required": True},
        ]
        edges = [
            {"from": "intake", "to": "implement"},
            {"from": "implement", "to": "review"},
            {"from": "review", "to": "implement", "rework": True},
        ]
        with TemporaryDirectory() as tmp:
            saved = _write_workflow_config(server, steps, tmp, edges=edges)
            loaded = server.read_workflow_config(tmp)

        self.assertEqual(saved, loaded)
        for step in loaded["steps"]:
            self.assertNotIn("kind", step)


if __name__ == "__main__":
    unittest.main()
