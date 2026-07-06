"""Guards that the bundled UI asset stays importable/packaged."""

import unittest
from importlib import resources
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


class PackagingTests(unittest.TestCase):
    def test_init_project_bootstraps_everything_and_is_idempotent(self):
        import json
        from dev_loop.__main__ import init_project

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = init_project(root, port=9999)

            self.assertTrue((root / "agents" / "hub.md").exists())
            self.assertTrue((root / "agents" / "_protocol.md").exists())
            self.assertTrue((root / ".dev_loop" / "workflow.json").exists())
            self.assertTrue((root / ".dev_loop" / "team.json").exists())
            mcp = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(
                "http://127.0.0.1:9999/mcp", mcp["mcpServers"]["devloop"]["url"]
            )
            self.assertIn(".dev_loop/tasks/", (root / ".gitignore").read_text(encoding="utf-8"))
            self.assertIn("多 agent 角色", (root / "CLAUDE.md").read_text(encoding="utf-8"))
            self.assertTrue(first["created"])

            # second run touches nothing
            second = init_project(root, port=9999)
            self.assertEqual([], second["created"])

            # team covers the core roles
            team = json.loads((root / ".dev_loop" / "team.json").read_text(encoding="utf-8"))
            roles = {m["role_id"] for m in team["members"]}
            self.assertEqual({"hub", "implementer", "reviewer"}, roles)

    def test_agents_dir_falls_back_to_packaged_templates(self):
        import dev_loop.server as server

        with TemporaryDirectory() as project, TemporaryDirectory() as cwd:
            with mock.patch.object(server.Path, "cwd", return_value=Path(cwd)):
                resolved = server._agents_dir(project)
            self.assertEqual(server._packaged_role_templates_dir(), resolved)
            roles = server.list_agent_roles(resolved)
            self.assertIn("hub", {role["id"] for role in roles})

    def test_materialize_role_templates_fills_project_agents(self):
        import dev_loop.server as server

        with TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            server._materialize_role_templates(agents_dir)
            names = {p.name for p in agents_dir.glob("*.md")}
            self.assertIn("hub.md", names)
            self.assertIn("_protocol.md", names)
            # existing files are not overwritten
            (agents_dir / "hub.md").write_text("custom", encoding="utf-8")
            server._materialize_role_templates(agents_dir)
            self.assertEqual("custom", (agents_dir / "hub.md").read_text(encoding="utf-8"))

    def test_create_server_locals_do_not_shadow_module_functions(self):
        # A closure-local function reusing a module-level name shadows it for
        # every call site inside create_server (this broke workflow start:
        # _engine_start resolved the MCP tool instead of the engine function).
        import ast
        import inspect
        import dev_loop.server as server

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

    def test_role_templates_are_packaged(self):
        templates = resources.files("dev_loop") / "role_templates"
        names = {entry.name for entry in templates.iterdir()}
        for required in ("_protocol.md", "_template.md", "hub.md", "implementer.md", "reviewer.md"):
            self.assertIn(required, names)

    def test_ui_asset_is_present_and_loadable(self):
        html = (
            resources.files("dev_loop")
            .joinpath("static/ui.html")
            .read_text(encoding="utf-8")
        )
        self.assertIn("<!doctype html>", html.lower())
        self.assertIn('id="dbPath"', html)
        self.assertIn("/api/status", html)
        self.assertIn("/api/projects", html)
        self.assertIn('id="agentTools"', html)
        self.assertIn('id="toolsTab"', html)
        self.assertIn('id="teamTab"', html)
        self.assertIn('id="workflowTab"', html)
        self.assertIn('id="rolesTab"', html)
        self.assertIn('id="tasksTab"', html)
        self.assertIn('id="toolsPage"', html)
        self.assertIn('id="teamPage"', html)
        self.assertIn('id="workflowPage"', html)
        self.assertIn('id="rolesPage"', html)
        self.assertIn('id="tasksPage"', html)
        self.assertIn('id="agentRoles"', html)
        self.assertIn('id="tasksList"', html)
        self.assertIn('id="taskDetails"', html)
        self.assertIn('id="toggleTaskDetails"', html)
        self.assertIn("details-collapsed", html)
        self.assertIn("function toggleTaskDetails()", html)
        self.assertIn("function renderTaskDetails(task)", html)
        self.assertIn("task-detail-body", html)
        self.assertIn('localStorage.getItem("devloop-task-details-collapsed") !== "0"', html)
        self.assertIn('class="side-menu"', html)
        self.assertIn("menu-button", html)
        self.assertIn("display: block;", html)
        self.assertIn("tool.profile_name", html)
        self.assertIn("tool.profile_path", html)
        self.assertIn("function setPage(page)", html)
        self.assertIn("/api/agent-tools", html)
        self.assertIn("/api/agent-roles", html)
        self.assertIn("/api/team", html)
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
        self.assertNotIn("function createTaskRun(taskId)", html)
        self.assertIn("function renderTaskRuns()", html)
        self.assertIn("function renderRoles()", html)
        self.assertIn("function renderTeam()", html)
        self.assertIn("function addTeamMember()", html)
        self.assertIn("function renderWorkflow()", html)
        self.assertIn("function addWorkflowStep()", html)
        self.assertIn("workflow-canvas", html)
        self.assertIn("workflow-node", html)
        self.assertIn("workflow-port", html)
        self.assertIn('id="wfArrow"', html)
        self.assertIn('marker-end="url(#wfArrow)"', html)
        self.assertIn("x: step.x, y: step.y + h / 2", html)
        self.assertIn("x: step.x + w / 2, y: step.y", html)
        self.assertIn("function startExistingEdgeDrag(", html)
        self.assertIn("function updateWorkflowEdgeTarget(", html)
        self.assertIn("Drag to reconnect; click to delete", html)
        self.assertIn("<strong>Assignment</strong>", html)
        self.assertIn("role: ${escapeHtml(task.role_required", html)
        self.assertIn("agent: ${escapeHtml(task.assignee", html)
        self.assertIn('id="teamRequirements"', html)
        self.assertIn('const REQUIRED_TEAM_ROLES = ["hub", "implementer", "reviewer"]', html)
        self.assertNotIn("function recommendAgent(taskId)", html)
        self.assertNotIn("function renderAssignmentCandidates()", html)
        self.assertNotIn("/api/tasks/${taskId}/assignment-candidates", html)
        self.assertIn("Expertise", html)
        self.assertIn("capability subscriptions", html)
        self.assertNotIn("Weight", html)
        self.assertIn("function wireRoleActionButtons()", html)
        self.assertIn('data-action="edit"', html)
        self.assertIn('data-role-id=', html)
        self.assertIn("selectedProjectId", html)
        self.assertIn("async function refreshWorkspace()", html)
        self.assertIn("function wireRefresh(", html)
        self.assertNotIn("function renderTaskWorkflow(", html)
        self.assertNotIn("function startTaskWorkflow(", html)
        self.assertNotIn("function completeTaskStep(", html)
        for button_id in (
            "refreshTools",
            "refreshTeam",
            "refreshWorkflow",
            "refreshRoles",
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
        import dev_loop.server as server

        self.assertTrue(server._UI_HTML)

    def test_gitignore_keeps_team_config_trackable(self):
        gitignore = Path(".gitignore").read_text(encoding="utf-8")
        self.assertIn(".dev_loop/tasks/", gitignore)
        self.assertNotIn(".dev_loop/\n", gitignore)

    def test_agent_tool_detection_shape(self):
        import dev_loop.server as server

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
        import dev_loop.server as server

        def fake_which(command):
            return f"/bin/{command}" if command == "codex" else None

        with mock.patch("dev_loop.server.shutil.which", side_effect=fake_which):
            tools = {tool["id"]: tool for tool in server.detect_agent_tools()}

        self.assertTrue(tools["codex"]["installed"])
        self.assertEqual("/bin/codex", tools["codex"]["path"])
        self.assertFalse(tools["claude"]["installed"])
        self.assertIsNone(tools["claude"]["path"])

    def test_hermes_profile_detection_lists_profile_directories(self):
        import dev_loop.server as server

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manager").mkdir()
            (root / "researcher").mkdir()
            (root / ".hidden").mkdir()
            (root / "notes.txt").write_text("not a profile", encoding="utf-8")

            profiles = server.detect_hermes_profiles(root)

        self.assertEqual(["manager", "researcher"], [p["name"] for p in profiles])

    def test_agent_tool_detection_splits_hermes_profiles_into_agents(self):
        import dev_loop.server as server

        fake_profiles = [
            {"name": "default", "path": "/tmp/default"},
            {"name": "manager", "path": "/tmp/manager"},
        ]
        with mock.patch("dev_loop.server.detect_hermes_profiles", return_value=fake_profiles):
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
        import dev_loop.server as server

        fake_profiles = [
            {"name": "manager qa", "path": "/tmp/manager-qa"},
            {"name": "manager/qa", "path": "/tmp/manager-qa-2"},
        ]
        with mock.patch("dev_loop.server.detect_hermes_profiles", return_value=fake_profiles):
            by_id = {tool["id"]: tool for tool in server.detect_agent_tools()}

        self.assertIn("hermes-manager-qa", by_id)
        self.assertIn("hermes-manager-qa-2", by_id)
        self.assertEqual("manager qa", by_id["hermes-manager-qa"]["profile_name"])
        self.assertEqual("manager/qa", by_id["hermes-manager-qa-2"]["profile_name"])

    def test_team_config_round_trips_project_file(self):
        import dev_loop.server as server

        with TemporaryDirectory() as tmp:
            team = server.write_team_config(
                [
                    {
                        "agent_name": "hermes-manager",
                        "role_id": "hub",
                    },
                    {
                        "agent_name": "codex",
                        "role_id": "implementer",
                        "enabled": True,
                        "expertise_level": "4",
                        "max_concurrent_tasks": "2",
                        "capabilities": "python, tests",
                        "notes": "primary builder",
                    },
                    {
                        "agent_name": "claude-code",
                        "role_id": "reviewer",
                    },
                ],
                tmp,
            )
            loaded = server.read_team_config(tmp)

        self.assertEqual(team, loaded)
        implementer = next(
            member for member in loaded["members"] if member["role_id"] == "implementer"
        )
        self.assertEqual("codex", implementer["agent_name"])
        self.assertEqual(4, implementer["expertise_level"])
        self.assertEqual(2, implementer["max_concurrent_tasks"])
        self.assertEqual(["python", "tests"], implementer["capabilities"])
        self.assertTrue(loaded["path"].endswith(".dev_loop/team.json"))

    def test_team_config_migrates_legacy_priority_to_expertise(self):
        import dev_loop.server as server

        member = server._normalize_team_member(
            {"agent_name": "codex", "role_id": "implementer", "priority": 120}
        )

        self.assertEqual(5, member["expertise_level"])
        self.assertNotIn("priority", member)
        self.assertNotIn("weight", member)

    def test_team_config_allows_unlimited_concurrency(self):
        import dev_loop.server as server

        member = server._normalize_team_member(
            {
                "agent_name": "codex",
                "role_id": "implementer",
                "max_concurrent_tasks": "0",
            }
        )

        self.assertEqual(0, member["max_concurrent_tasks"])

    def test_team_config_parses_string_enabled_explicitly(self):
        import dev_loop.server as server

        member = server._normalize_team_member(
            {
                "agent_name": "codex",
                "role_id": "implementer",
                "enabled": "false",
            }
        )

        self.assertFalse(member["enabled"])
        with self.assertRaisesRegex(ValueError, "enabled"):
            server._normalize_team_member(
                {
                    "agent_name": "codex",
                    "role_id": "implementer",
                    "enabled": "nope",
                }
            )

    def test_workflow_config_defaults_and_round_trips_project_file(self):
        import dev_loop.server as server

        with TemporaryDirectory() as tmp:
            default = server.read_workflow_config(tmp)
            saved = server.write_workflow_config(
                [
                    {
                        "id": "plan",
                        "name": "Plan",
                        "role_id": "hub",
                        "task_status": "created",
                        "required": True,
                    },
                    {
                        "id": "ship",
                        "name": "Ship",
                        "role_id": "implementer",
                        "task_status": "closed",
                    },
                    {
                        "id": "check",
                        "name": "Check",
                        "role_id": "reviewer",
                        "task_status": "replied",
                    },
                ],
                tmp,
            )
            loaded = server.read_workflow_config(tmp)

        self.assertEqual("intake", default["steps"][0]["id"])
        self.assertEqual(saved, loaded)
        self.assertEqual(["plan", "ship", "check"], [step["id"] for step in loaded["steps"]])
        self.assertTrue(loaded["path"].endswith(".dev_loop/workflow.json"))

    def test_workflow_rejects_payload_missing_core_role_steps(self):
        import dev_loop.server as server
        from dev_loop.store import InvalidInputError

        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                InvalidInputError,
                "workflow must keep steps for core roles: hub, reviewer",
            ):
                server.write_workflow_config(
                    [{"id": "impl", "name": "Impl", "role_id": "implementer"}],
                    tmp,
                )

    def test_write_rejects_roles_without_role_file(self):
        import dev_loop.server as server
        from dev_loop.store import InvalidInputError

        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(InvalidInputError, "unknown roles: ghost_role"):
                server.write_workflow_config(
                    [
                        {"id": "a", "name": "A", "role_id": "hub"},
                        {"id": "b", "name": "B", "role_id": "implementer"},
                        {"id": "c", "name": "C", "role_id": "reviewer"},
                        {"id": "g", "name": "G", "role_id": "ghost_role"},
                    ],
                    tmp,
                )
            with self.assertRaisesRegex(InvalidInputError, "unknown roles: ghost_role"):
                server.write_team_config(
                    [{"agent_name": "codex", "role_id": "ghost_role"}], tmp
                )

    def test_workflow_reports_graph_warnings(self):
        import dev_loop.server as server

        steps = [
            {"id": "a", "name": "A", "role_id": "hub"},
            {"id": "b", "name": "B", "role_id": "implementer"},
            {"id": "c", "name": "C", "role_id": "reviewer"},
        ]
        with TemporaryDirectory() as tmp:
            connected = server.write_workflow_config(
                steps, tmp, [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}]
            )
            orphaned = server.write_workflow_config(
                steps, tmp, [{"from": "a", "to": "b"}]
            )

        self.assertEqual([], connected["warnings"])
        # c has no incoming edge, so it is a second entry point rather than
        # unreachable; a fully disconnected node yields no warning about
        # reachability but b/c both count as terminals -> no warnings there.
        self.assertEqual([], orphaned["warnings"])

    def test_workflow_warns_on_unreachable_steps(self):
        import dev_loop.server as server

        steps = [
            {"id": "a", "name": "A", "role_id": "hub"},
            {"id": "b", "name": "B", "role_id": "implementer"},
            {"id": "c", "name": "C", "role_id": "reviewer"},
        ]
        # b <-> c form a cycle with no entry from a's component.
        with TemporaryDirectory() as tmp:
            saved = server.write_workflow_config(
                steps, tmp, [{"from": "b", "to": "c"}, {"from": "c", "to": "b"}]
            )

        self.assertTrue(any("unreachable" in w for w in saved["warnings"]))
        self.assertTrue(any("no path to an end" in w for w in saved["warnings"]))

    def test_core_role_steps_are_always_required_and_locked(self):
        import dev_loop.server as server

        with TemporaryDirectory() as tmp:
            saved = server.write_workflow_config(
                [
                    {"id": "impl", "name": "Impl", "role_id": "implementer", "required": False},
                    {"id": "check", "name": "Check", "role_id": "reviewer", "required": False},
                    {"id": "gate", "name": "Gate", "role_id": "hub", "required": False},
                    {"id": "qa", "name": "QA", "role_id": "tester", "required": False},
                ],
                tmp,
            )
            loaded = server.read_workflow_config(tmp)

        self.assertEqual(saved, loaded)
        by_id = {step["id"]: step for step in loaded["steps"]}
        for core in ("impl", "check", "gate"):
            self.assertTrue(by_id[core]["required"], core)
            self.assertTrue(by_id[core]["required_locked"], core)
        self.assertFalse(by_id["qa"]["required"])
        self.assertFalse(by_id["qa"]["required_locked"])

    def test_default_workflow_has_parallel_merge_and_loopback(self):
        import dev_loop.server as server

        edges = server.default_workflow_edges()
        ids = {s["id"] for s in server.default_workflow_steps()}
        # every edge references a real step
        for e in edges:
            self.assertIn(e["from"], ids)
            self.assertIn(e["to"], ids)
        out_of = lambda n: [e["to"] for e in edges if e["from"] == n]
        into = lambda n: [e["from"] for e in edges if e["to"] == n]
        # parallel: product_design fans out to two branches
        self.assertEqual({"ui_design", "architecture"}, set(out_of("product_design")))
        # merge: implement is fed by both branches
        self.assertLessEqual({"ui_design", "architecture"}, set(into("implement")))
        # loop-back: review returns to implement
        self.assertIn("implement", out_of("review"))
        # config round-trips through write/read with the branching edges intact
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            loaded = server.read_workflow_config(tmp)
        self.assertEqual(edges, loaded["edges"])

    def test_workflow_persists_positions_and_edges(self):
        import dev_loop.server as server

        with TemporaryDirectory() as tmp:
            saved = server.write_workflow_config(
                [
                    {"id": "a", "name": "A", "role_id": "hub", "x": 100, "y": 50},
                    {"id": "b", "name": "B", "role_id": "implementer", "x": 400, "y": 200},
                    {"id": "c", "name": "C", "role_id": "reviewer", "x": 700, "y": 50},
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
        import dev_loop.server as server
        from dev_loop.store import InvalidInputError

        steps = [
            {"id": "a", "name": "A", "role_id": "hub"},
            {"id": "b", "name": "B", "role_id": "implementer"},
            {"id": "c", "name": "C", "role_id": "reviewer"},
        ]
        with TemporaryDirectory() as tmp:
            saved = server.write_workflow_config(
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
                server.write_workflow_config(steps, tmp, [{"from": "a", "to": "z"}])

    def test_legacy_workflow_without_edges_gets_sequential_chain(self):
        import dev_loop.server as server
        import json as _json

        with TemporaryDirectory() as tmp:
            cfg = Path(tmp) / ".dev_loop" / "workflow.json"
            cfg.parent.mkdir(parents=True)
            cfg.write_text(_json.dumps({"steps": [
                {"id": "a", "name": "A", "role_id": "hub"},
                {"id": "b", "name": "B", "role_id": "hub"},
                {"id": "c", "name": "C", "role_id": "hub"},
            ]}))
            loaded = server.read_workflow_config(tmp)
        self.assertEqual(
            [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}], loaded["edges"]
        )

    def test_workflow_config_rejects_invalid_step_role(self):
        import dev_loop.server as server
        from dev_loop.store import InvalidInputError

        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(InvalidInputError, "role_id is invalid"):
                server.write_workflow_config(
                    [{"name": "Bad", "role_id": "bad-role"}],
                    tmp,
                )

    def test_assignment_candidates_rank_by_capabilities_expertise_and_load(self):
        import dev_loop.server as server

        task = {
            "id": 1,
            "role_required": "implementer",
            "importance": "critical",
            "size": "large",
            "risk": "high",
            "required_capabilities": ["python", "sqlite"],
            "exclusive_workspace": True,
        }
        members = [
            {
                "agent_name": "codex",
                "role_id": "implementer",
                "enabled": True,
                "expertise_level": 5,
                "max_concurrent_tasks": 2,
                "capabilities": ["python", "sqlite"],
            },
            {
                "agent_name": "gemini",
                "role_id": "implementer",
                "enabled": True,
                "expertise_level": 5,
                "max_concurrent_tasks": 1,
                "capabilities": ["python"],
            },
            {
                "agent_name": "claude-code",
                "role_id": "reviewer",
                "enabled": True,
                "expertise_level": 5,
                "max_concurrent_tasks": 1,
                "capabilities": ["python", "sqlite"],
            },
        ]

        ranked = server.rank_assignment_candidates(task, members, {"codex": 1})

        self.assertEqual("implementer", ranked["role_id"])
        self.assertEqual(5, ranked["required_expertise_level"])
        self.assertEqual("codex", ranked["selected"]["agent_name"])
        self.assertEqual(["reviewer", "tester"], ranked["required_followups"])
        self.assertEqual([], ranked["selected"]["missing_capabilities"])
        self.assertNotIn(
            "claude-code", [candidate["agent_name"] for candidate in ranked["candidates"]]
        )

    def test_assignment_candidates_penalize_low_expertise_for_complex_tasks(self):
        import dev_loop.server as server

        task = {
            "id": 1,
            "role_required": "implementer",
            "importance": "critical",
            "size": "large",
            "risk": "high",
            "required_capabilities": ["python"],
            "exclusive_workspace": True,
        }
        members = [
            {
                "agent_name": "junior",
                "role_id": "implementer",
                "enabled": True,
                "expertise_level": 2,
                "max_concurrent_tasks": 1,
                "capabilities": ["python"],
            },
            {
                "agent_name": "senior",
                "role_id": "implementer",
                "enabled": True,
                "expertise_level": 5,
                "max_concurrent_tasks": 1,
                "capabilities": ["python"],
            },
        ]

        ranked = server.rank_assignment_candidates(task, members)
        junior = next(
            candidate for candidate in ranked["candidates"] if candidate["agent_name"] == "junior"
        )

        self.assertEqual("senior", ranked["selected"]["agent_name"])
        self.assertEqual(3, junior["expertise_gap"])

    def test_team_config_reports_missing_core_roles_without_rejecting(self):
        import dev_loop.server as server

        with TemporaryDirectory() as tmp:
            saved = server.write_team_config(
                [
                    {"agent_name": "hermes-manager", "role_id": "hub"},
                    {"agent_name": "codex", "role_id": "implementer"},
                    {
                        "agent_name": "claude-code",
                        "role_id": "reviewer",
                        "enabled": False,
                    },
                ],
                tmp,
            )
            loaded = server.read_team_config(tmp)

        self.assertEqual(["reviewer"], saved["missing_roles"])
        self.assertEqual(3, len(loaded["members"]))

    def test_team_config_saves_single_member(self):
        import dev_loop.server as server

        with TemporaryDirectory() as tmp:
            saved = server.write_team_config(
                [{"agent_name": "codex", "role_id": "implementer"}], tmp
            )

        self.assertEqual(1, len(saved["members"]))
        self.assertEqual(["hub", "reviewer"], saved["missing_roles"])

    def test_team_config_rejects_invalid_role_id(self):
        import dev_loop.server as server
        from dev_loop.store import InvalidInputError

        with TemporaryDirectory() as tmp:
            with self.assertRaises(InvalidInputError):
                server.write_team_config(
                    [{"agent_name": "codex", "role_id": "bad-role"}],
                    tmp,
                )

    def test_agent_role_detection_lists_non_private_role_files(self):
        import dev_loop.server as server

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "hub.md").write_text("# 角色：hub\n\nbody", encoding="utf-8")
            (root / "reviewer.md").write_text("# 角色：reviewer\n", encoding="utf-8")
            (root / "tester.md").write_text("# 角色：tester\n", encoding="utf-8")
            (root / "bad-name.md").write_text("# bad\n", encoding="utf-8")
            (root / "123bad.md").write_text("# bad\n", encoding="utf-8")
            (root / "_protocol.md").write_text("# protocol\n", encoding="utf-8")

            roles = server.list_agent_roles(root)

        self.assertEqual(["hub", "reviewer", "tester"], [role["id"] for role in roles])
        self.assertEqual("角色：hub", roles[0]["name"])
        self.assertIn("body", roles[0]["content"])

    def test_role_id_validation_rejects_private_and_non_identifiers(self):
        import dev_loop.server as server

        self.assertTrue(server._is_valid_role_id("tester"))
        self.assertTrue(server._is_valid_role_id("security_auditor"))
        self.assertFalse(server._is_valid_role_id("_protocol"))
        self.assertFalse(server._is_valid_role_id("bad-name"))
        self.assertFalse(server._is_valid_role_id("123bad"))

    def test_role_content_validation_requires_string(self):
        import dev_loop.server as server
        from dev_loop.store import InvalidInputError

        self.assertEqual("body", server._validate_role_content("body"))
        with self.assertRaises(InvalidInputError):
            server._validate_role_content(None)
        with self.assertRaises(InvalidInputError):
            server._validate_role_content([])


if __name__ == "__main__":
    unittest.main()
