"""The bundled Orbit skill routes each intent to its dedicated MCP App."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1] / "skills" / "orbit"


class OrbitSkillCardRoutingTests(unittest.TestCase):
    def test_main_skill_names_each_card_and_tool(self) -> None:
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        for marker in (
            "open_orbit_dashboard", "list_workflows",
            "get_workflow_definition", "delete_workflow", "generate_workflow", "start_run",
            "Workflow list", "Workflow generation", "Goal execution",
        ):
            self.assertIn(marker, text)

    def test_workflow_view_procedure_covers_detail_actions(self) -> None:
        text = (ROOT / "reference" / "view-workflows.md").read_text(
            encoding="utf-8"
        )
        for marker in ("New goal", "Modify", "Delete", "explicit confirmation"):
            self.assertIn(marker, text)

    def test_workflow_list_offers_new_goal_without_opening_detail(self) -> None:
        text = (ROOT / "reference" / "view-workflows.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Every item exposes a\n   **New goal** action directly", text)
        self.assertIn("Do not make\n   the user open the detail card first", text)

    def test_workflow_detail_is_an_embedded_list_card_view(self) -> None:
        text = (ROOT / "reference" / "view-workflows.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("switches to its built-in detail\n   view", text)
        self.assertIn("must not open a separate workflow-detail MCP App card", text)
        self.assertIn("returns to the list inside the same card", text)

    def test_run_and_authoring_do_not_open_dashboard_as_a_surrogate(self) -> None:
        for name in ("execute-goal.md", "authoring-with-current-app.md"):
            text = (ROOT / "reference" / name).read_text(encoding="utf-8")
            self.assertIn("Do not call", text)
            self.assertIn("open_orbit_dashboard", text)

    def test_explicit_workflow_execution_skips_list_card(self) -> None:
        text = (ROOT / "reference" / "execute-goal.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("explicit workflow ID", text)
        self.assertIn("`list_workflows` first", text)
        self.assertIn("must not open the workflow-list or", text)

    def test_goal_execution_inspects_without_opening_workflow_detail(self) -> None:
        text = (ROOT / "reference" / "execute-goal.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("`inspect_workflow_definition`", text)
        self.assertIn("no App card binding", text)
        self.assertIn("must not open the workflow-list or\n   workflow-detail card", text)

    def test_explicit_goal_execution_does_not_repeat_start_confirmation(self) -> None:
        text = (ROOT / "reference" / "execute-goal.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("already authorizes the\n   `start_run` mutation", text)
        self.assertIn("do not ask again merely because the workflow contains", text)
        self.assertIn("human interrupt or Runtime\n   confirmation gates", text)

    def test_human_resume_uses_the_interrupt_output_ports(self) -> None:
        text = (ROOT / "reference" / "execute-goal.md").read_text(
            encoding="utf-8"
        )
        for marker in (
            "interrupt's `output_ports`",
            "top-level keys are port IDs",
            '{"result":{"decision":"approve","value":null}}',
            'decision:"reject"',
        ):
            self.assertIn(marker, text)

    def test_opening_orbit_does_not_register_or_listen(self) -> None:
        text = (ROOT / "reference" / "open-orbit.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Opening Orbit is display-only", text)
        self.assertIn("Do not call `register_authoring_client`", text)
        self.assertIn("`wait_authoring_request`", text)
        self.assertIn("only when the user explicitly", text)

    def test_local_plugin_refresh_stops_before_codex_restart(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        procedure = (ROOT / "reference" / "refresh-codex-plugin.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("rebuild, refresh, or reinstall the local Orbit plugin", skill)
        self.assertIn("refresh-codex-plugin.md", skill)
        for marker in (
            "read_marketplace_name.py",
            "update_plugin_cachebuster.py",
            "codex plugin add orbit@<resolved-marketplace-name>",
            "codex plugin list",
            "lsof -nP -iTCP:8848 -sTCP:LISTEN",
            "kill <exact-pid>",
            "Stop here",
            "fully quit Codex",
            "start a new task",
        ):
            self.assertIn(marker, procedure)


if __name__ == "__main__":
    unittest.main()
