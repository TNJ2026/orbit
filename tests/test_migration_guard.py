"""Architecture guards for the legacy-engine migration.

M0 installs these in *record* mode: they pin the migration inventories to the
code that exists today, so drift is visible immediately, but they do not yet
forbid legacy imports.  M6 flips the recorded baselines into hard denials.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
MIGRATION = TESTS / "migration"
SRC = ROOT / "src" / "orbit"

LEGACY_TEST_FILES = (
    "test_workflow_engine.py",
    "test_worktree.py",
    "test_store.py",
    "test_packaging.py",
    "test_project_index.py",
)

# Files that make up the new Runtime.  Everything else under src/orbit is legacy
# and must disappear (or move) by M6.
NEW_RUNTIME_ROOTS = ("workflow",)


def _load(name: str) -> dict:
    return json.loads((MIGRATION / name).read_text(encoding="utf-8"))


def _test_ids(path: Path) -> set[str]:
    """Every ``file::Class::test_method`` id declared in a test module."""

    ids: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name.startswith("test_"):
                ids.add(f"tests/{path.name}::{node.name}::{item.name}")
    return ids


class LegacyTestDispositionGuard(unittest.TestCase):
    """Every legacy test must carry an explicit migrate/rewrite/delete decision."""

    def setUp(self) -> None:
        self.inventory = _load("legacy_test_disposition.json")
        self.declared = {item["test_id"]: item for item in self.inventory["tests"]}

    def test_every_legacy_test_has_a_disposition(self) -> None:
        actual: set[str] = set()
        for name in LEGACY_TEST_FILES:
            actual |= _test_ids(TESTS / name)

        missing = sorted(actual - set(self.declared))
        stale = sorted(set(self.declared) - actual)
        self.assertEqual(
            [], missing,
            "legacy tests without a disposition (add them to "
            "tests/migration/legacy_test_disposition.json):\n" + "\n".join(missing),
        )
        self.assertEqual(
            [], stale,
            "disposition entries for tests that no longer exist:\n" + "\n".join(stale),
        )

    def test_dispositions_are_well_formed(self) -> None:
        allowed = set(self.inventory["dispositions"])
        for test_id, item in self.declared.items():
            with self.subTest(test_id):
                self.assertIn(item["disposition"], allowed)
                self.assertTrue(item["target_phase"], "target_phase is required")
                # A surviving capability needs somewhere to land before M6 may
                # delete the original test.
                if item["disposition"] in {"migrate", "rewrite"}:
                    self.assertTrue(
                        item.get("replacement_hint"),
                        "migrate/rewrite requires a replacement hint",
                    )
                else:
                    self.assertTrue(
                        item.get("deletion_rationale") or item.get("replacement_hint"),
                        "delete requires a rationale",
                    )


class ExternalIntegrationGuard(unittest.TestCase):
    """Nothing consumer-facing may vanish silently with the legacy server."""

    def setUp(self) -> None:
        self.inventory = _load("external_integrations.json")

    def test_no_unclassified_entry_points(self) -> None:
        self.assertEqual(
            [], self.inventory["unclassified"],
            "unclassified external entry points block M2/M6",
        )

    def test_entries_are_actionable(self) -> None:
        allowed = {"retain_unchanged", "retain_and_rewrite", "replace", "delete"}
        for entry in self.inventory["integrations"]:
            with self.subTest(entry["id"]):
                self.assertIn(entry["disposition"], allowed)
                self.assertTrue(entry["consumer"])
                self.assertTrue(entry["target_phase"])
                if entry["disposition"] != "delete":
                    self.assertTrue(
                        entry.get("target_url"), "surviving integrations need a target URL"
                    )

    def test_codex_mcp_config_is_covered(self) -> None:
        """The stale .codex MCP url must stay a tracked contract, not a surprise."""

        config = ROOT / ".codex" / "config.toml"
        if not config.exists():
            self.skipTest(".codex/config.toml is not present in this checkout")
        urls = re.findall(r'url\s*=\s*"([^"]+)"', config.read_text(encoding="utf-8"))
        covered = {entry["current_url"] for entry in self.inventory["integrations"]}
        for url in urls:
            self.assertIn(
                url, covered,
                f"{url} is configured for an external consumer but is not in "
                "tests/migration/external_integrations.json",
            )


class LegacySurfaceBaseline(unittest.TestCase):
    """Record the legacy surface M6 has to remove.

    Recording it as a test means the numbers cannot drift unnoticed while the
    migration is in flight.  These assertions are deliberately *upper bounds*:
    the legacy surface may shrink during M1–M5, never grow.
    """

    def test_legacy_production_modules_do_not_grow(self) -> None:
        baseline = {
            "server.py": 6903,
            "store.py": 1976,
            "__main__.py": 463,
            "project_index.py": 146,
        }
        for name, ceiling in baseline.items():
            with self.subTest(name):
                path = SRC / name
                if not path.exists():
                    continue  # already deleted by a later milestone
                lines = len(path.read_text(encoding="utf-8").splitlines())
                self.assertLessEqual(
                    lines, ceiling,
                    f"{name} grew past its M0 baseline; the legacy engine is "
                    "frozen — new behaviour belongs in the new Runtime",
                )

    def test_new_runtime_never_imports_the_legacy_engine(self) -> None:
        """This one is already a hard rule: the new Runtime must stay clean."""

        offenders: list[str] = []
        for root in NEW_RUNTIME_ROOTS:
            for path in (SRC / root).rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    names: list[str] = []
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        names = [node.module]
                    for name in names:
                        if re.search(r"(^|\.)(server|store|project_index)$", name):
                            offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}:{name}")
        self.assertEqual([], offenders, "new Runtime imported a legacy module")


if __name__ == "__main__":
    unittest.main()
