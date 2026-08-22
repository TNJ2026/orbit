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
        self.assertIn("@deepseek-ai/dsh-mcp-client", manifest["peerDependencies"])

    def test_patch_starts_the_minimal_profile_under_a_distinct_actor(self) -> None:
        patch = yaml.safe_load((BUNDLE / "cordis.patch.yml").read_text(encoding="utf-8"))
        plugin = patch[0]["insert"][0]
        self.assertEqual("@deepseek-ai/dsh-mcp-client", plugin["name"])
        config = plugin["config"]
        self.assertEqual("stdio", config["transport"])
        self.assertEqual("orbit", config["command"])
        self.assertEqual(
            ["mcp", "--mcp-tool-profile", "harness", "--actor", "harness:profile"],
            config["args"],
        )
        self.assertTrue(config["failOnStartupError"])


if __name__ == "__main__":
    unittest.main()
