"""Guards that the bundled UI asset stays importable/packaged."""

import unittest
from importlib import resources
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


class PackagingTests(unittest.TestCase):
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
        self.assertIn('id="rolesTab"', html)
        self.assertIn('id="tasksTab"', html)
        self.assertIn('id="toolsPage"', html)
        self.assertIn('id="rolesPage"', html)
        self.assertIn('id="tasksPage"', html)
        self.assertIn('id="agentRoles"', html)
        self.assertIn('id="tasksList"', html)
        self.assertIn('class="side-menu"', html)
        self.assertIn("menu-button", html)
        self.assertIn("display: block;", html)
        self.assertIn("class=\"profiles\"", html)
        self.assertIn("profile-name", html)
        self.assertIn("function setPage(page)", html)
        self.assertIn("/api/agent-tools", html)
        self.assertIn("/api/agent-roles", html)
        self.assertIn("function renderRoles()", html)
        self.assertIn("selectedProjectId", html)
        self.assertIn("async function refreshWorkspace()", html)
        self.assertIn('addEventListener("click", () => run(refreshWorkspace))', html)
        self.assertNotIn('id="projectSelect"', html)
        self.assertNotIn('id="registerAgent"', html)
        self.assertGreater(len(html), 1000)

    def test_server_module_imports(self):
        # Importing the module reads the UI asset at module load time;
        # a missing resource would raise here.
        import dev_loop.server as server

        self.assertTrue(server._UI_HTML)

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
        self.assertIn("openclaw", by_id)
        self.assertIn("profiles", by_id["hermes"])
        self.assertIn("profile_count", by_id["hermes"])

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

    def test_agent_tool_detection_includes_hermes_profiles(self):
        import dev_loop.server as server

        fake_profiles = [{"name": "manager", "path": "/tmp/manager"}]
        with mock.patch("dev_loop.server.detect_hermes_profiles", return_value=fake_profiles):
            tools = {tool["id"]: tool for tool in server.detect_agent_tools()}

        self.assertEqual(fake_profiles, tools["hermes"]["profiles"])
        self.assertEqual(1, tools["hermes"]["profile_count"])

    def test_agent_role_detection_lists_non_private_role_files(self):
        import dev_loop.server as server

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "hub.md").write_text("# 角色：hub\n\nbody", encoding="utf-8")
            (root / "reviewer.md").write_text("# 角色：reviewer\n", encoding="utf-8")
            (root / "tester.md").write_text("# 角色：tester\n", encoding="utf-8")
            (root / "_protocol.md").write_text("# protocol\n", encoding="utf-8")

            roles = server.list_agent_roles(root)

        self.assertEqual(["hub", "reviewer", "tester"], [role["id"] for role in roles])
        self.assertEqual("角色：hub", roles[0]["name"])
        self.assertIn("body", roles[0]["content"])


if __name__ == "__main__":
    unittest.main()
