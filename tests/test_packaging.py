"""M6: what the installed package contains, and what it must never contain again.

The legacy engine is gone. This file used to assert the shape of its workflow
config, its UI and its agent detection; all of that was deleted with the code.
What remains is the package manifest guard the migration plan asks for: the
wheel ships the Runtime and the modular UI, and nothing from the old world can
come back without failing here.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
import unittest


ORBIT = resources.files("orbit")
REMOVED_MODULES = ("server", "store", "project_index")
REMOVED_ASSETS = (
    "static/ui.html",
    "static/workflow-ui.html",
    "static/vendor/dagre.min.js",
)


class PackageContentTests(unittest.TestCase):
    def test_the_modular_ui_ships(self) -> None:
        root = ORBIT.joinpath("static/workflow-ui")
        index = root.joinpath("index.html").read_text(encoding="utf-8")
        self.assertIn("Orbit Runtime", index)
        self.assertIn('src="assets/app.js"', index)
        for asset in (
            "app.css", "app.js", "api.js", "i18n.js",
            "i18n.zh-CN.json", "i18n.en-US.json",
            "router.js", "components/command-dialog.js",
            "components/data-state.js", "styles/tokens.css",
            "styles/shell.css", "styles/components.css", "styles/views.css",
        ):
            with self.subTest(asset=asset):
                self.assertTrue(root.joinpath("assets", asset).is_file())

    def test_the_mcp_xyflow_bundle_ships_offline(self) -> None:
        root = ORBIT.joinpath("static/mcp-app")
        script = root.joinpath("workflow-detail.js").read_text(encoding="utf-8")
        style = root.joinpath("workflow-detail.css").read_text(encoding="utf-8")
        self.assertIn("OrbitWorkflowGraph", script)
        self.assertIn("react-flow__controls", style)
        self.assertFalse(root.joinpath("workflow-detail.js.map").is_file())
        self.assertNotIn("process.env.NODE_ENV", script)

    def test_the_mcp_graph_keeps_nodes_readable(self) -> None:
        source = Path(__file__).resolve().parents[1].joinpath(
            "ui/editor/src/mcp-workflow-graph.jsx"
        ).read_text(encoding="utf-8")
        self.assertIn("const MIN_ZOOM = 0.5", source)
        self.assertIn("minZoom: MIN_ZOOM", source)
        self.assertIn("minZoom={MIN_ZOOM}", source)

    def test_the_runtime_packages_are_importable(self) -> None:
        for module in (
            "orbit.web.app", "orbit.web.api_v1", "orbit.web.mcp",
            "orbit.platform.cutover", "orbit.workflow.langgraph_runtime",
            "orbit.hub", "orbit.workflow.langgraph_runtime.execution_worker",
        ):
            with self.subTest(module=module):
                __import__(module)


class LegacyRemovalTests(unittest.TestCase):
    """The absolute prohibitions from the migration plan, as assertions."""

    def test_the_legacy_modules_are_gone(self) -> None:
        for name in REMOVED_MODULES:
            with self.subTest(module=name):
                self.assertFalse(ORBIT.joinpath(f"{name}.py").is_file())
                with self.assertRaises(ImportError):
                    __import__(f"orbit.{name}")

    def test_the_legacy_assets_are_gone(self) -> None:
        for asset in REMOVED_ASSETS:
            with self.subTest(asset=asset):
                self.assertFalse(ORBIT.joinpath(asset).is_file())

    def test_platform_metadata_does_not_ship_as_an_asset(self) -> None:
        for path in Path(str(ORBIT)).rglob("*"):
            self.assertNotIn(path.name, {".DS_Store", "Thumbs.db"})

    def test_no_legacy_config_template_ships(self) -> None:
        """`workflow.json` was the legacy engine's config; nothing writes it."""

        for path in Path(str(ORBIT)).rglob("workflow.json"):
            self.fail(f"legacy workflow config shipped: {path}")

    def test_the_state_dirs_stay_out_of_git(self) -> None:
        lines = Path(".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".orbit/", lines)
        self.assertIn(".dev_loop/", lines)


if __name__ == "__main__":
    unittest.main()


class SourceIsReviewableTests(unittest.TestCase):
    """Text that git cannot diff is text nobody reviews.

    `WorkflowPanel.jsx` carried a NUL byte, so git classified it as binary and
    showed `Bin 0 -> 6684 bytes` instead of its contents. A function that was
    never defined shipped inside it, took the React tree down on the first
    click, and was invisible in every diff along the way.
    """

    ROOTS = ("src/orbit", "ui/editor/src", "tests")
    TEXT_SUFFIXES = {
        ".py", ".js", ".mjs", ".jsx", ".css", ".html", ".json", ".md", ".toml",
    }

    def test_no_source_file_contains_a_nul_byte(self) -> None:
        root = Path(__file__).resolve().parents[1]
        offenders = []
        for folder in self.ROOTS:
            for path in (root / folder).rglob("*"):
                if not path.is_file() or path.suffix not in self.TEXT_SUFFIXES:
                    continue
                if "node_modules" in path.parts or "__pycache__" in path.parts:
                    continue
                data = path.read_bytes()
                position = data.find(b"\x00")
                if position >= 0:
                    offenders.append(f"{path.relative_to(root)} at byte {position}")
        self.assertEqual([], offenders)
