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

    def test_the_runtime_packages_are_importable(self) -> None:
        for module in (
            "orbit.web.app", "orbit.web.api_v1", "orbit.web.mcp",
            "orbit.platform.cutover", "orbit.workflow.api.plan_read_models",
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
