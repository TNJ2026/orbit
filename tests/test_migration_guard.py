"""Architecture guards for the legacy-engine migration.

M0 installed these in *record* mode. M6 flipped them: the legacy engine is
deleted, so the baselines below are now hard denials. What they forbid is not
"code that looks old" but specific things that would restore dual state —
importing a removed module, shipping a removed asset, advertising a removed
command, or reading a pre-migration database file.
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

# The legacy test modules M6 deleted. `test_packaging.py` survives as a
# package-manifest guard, so its rewritten tests stay under disposition.
LEGACY_TEST_FILES = ("test_packaging.py",)
DELETED_TEST_FILES = (
    "test_workflow_engine.py",
    "test_worktree.py",
    "test_store.py",
    "test_project_index.py",
    "test_workflow_db_check_cli.py",
)

# Modules and assets that must never come back.
REMOVED_MODULES = ("server.py", "store.py", "project_index.py")
REMOVED_ASSETS = (
    "static/ui.html", "static/workflow-ui.html", "static/vendor/dagre.min.js",
)
# Commands the cutover retired. A CLI that advertises one again has grown a
# second way to run the system.
REMOVED_COMMANDS = ("start", "up", "init", "config", "runner")

# After the cutover every package under src/orbit is new Runtime, so the
# import guard covers all of them rather than one subtree.
NEW_RUNTIME_ROOTS = ("workflow", "web", "platform", "workspace")


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

    def test_the_inventory_is_closed(self) -> None:
        """Post-cutover the inventory is a record, not a to-do list.

        Every legacy test either lives in a module that M6 deleted, or was
        rewritten into `test_packaging.py`. Nothing may still be pending, and
        no entry may point at a module that was never dealt with.
        """

        pending = sorted(
            test_id for test_id, item in self.declared.items()
            if item["disposition"] != "delete"
            and not item.get("replacement_delivered")
            and Path(ROOT / test_id.split("::")[0]).exists()
            and test_id.split("::")[0].split("/")[-1] not in {"test_packaging.py"}
        )
        self.assertEqual([], pending, "unmigrated legacy tests:\n" + "\n".join(pending))

    def test_every_declared_module_is_accounted_for(self) -> None:
        surviving = {"tests/test_packaging.py"}
        for test_id in self.declared:
            module = test_id.split("::")[0]
            with self.subTest(module=module):
                self.assertTrue(
                    module in surviving or not (ROOT / module).exists(),
                    f"{module} still exists but its tests were dispositioned away",
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


class LegacyRemovalGuard(unittest.TestCase):
    """The cutover, as assertions. These are denials, not baselines."""

    def test_the_legacy_modules_stay_deleted(self) -> None:
        for name in REMOVED_MODULES:
            with self.subTest(name):
                self.assertFalse(
                    (SRC / name).exists(),
                    f"{name} is back; the legacy engine was deleted in M6",
                )

    def test_the_legacy_assets_stay_deleted(self) -> None:
        for asset in REMOVED_ASSETS:
            with self.subTest(asset):
                self.assertFalse((SRC / asset).exists())

    def test_the_legacy_test_modules_stay_deleted(self) -> None:
        for name in DELETED_TEST_FILES:
            with self.subTest(name):
                self.assertFalse((TESTS / name).exists())

    def test_production_code_never_opens_a_legacy_database(self) -> None:
        """The restricted-path sentinel rule.

        `messages.db` and `.dev_loop` may appear only in the one function that
        stats them for the upgrade prompt. Anywhere else — and especially near
        an open() or a database connection — they would mean the runtime had
        started reading abandoned state again.
        """

        sentinel = SRC / "platform" / "projects.py"
        offenders: list[str] = []
        for path in SRC.rglob("*.py"):
            if path == sentinel:
                continue
            text = path.read_text(encoding="utf-8")
            for literal in ("messages.db", ".dev_loop"):
                if literal in text:
                    offenders.append(f"{path.relative_to(ROOT)}: {literal}")
        self.assertEqual(
            [], offenders,
            "legacy paths must stay inside legacy_database_candidates()",
        )

    def test_the_sentinel_only_stats_legacy_paths(self) -> None:
        """It may ask whether the file exists. It may not open it.

        Scoped to the legacy functions rather than the whole module: the
        project index legitimately opens its own lock file, and a
        module-wide ban would only teach people to route around the guard.
        """

        sentinel = SRC / "platform" / "projects.py"
        tree = ast.parse(sentinel.read_text(encoding="utf-8"), filename=str(sentinel))
        legacy_functions = {
            "legacy_database_candidates", "legacy_engine_db_path",
            "legacy_database_warning", "warn_about_legacy_database",
        }
        forbidden = {
            "open", "connect", "connect_workflow_database", "copy", "copyfile",
            "read_text", "read_bytes", "unlink", "rename",
        }
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name not in legacy_functions:
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                name = (
                    call.func.id if isinstance(call.func, ast.Name)
                    else call.func.attr if isinstance(call.func, ast.Attribute)
                    else None
                )
                if name in forbidden:
                    offenders.append(f"{node.name} calls {name}")
        self.assertEqual([], offenders, "the legacy sentinel must only stat")

    def test_the_cli_advertises_no_retired_command(self) -> None:
        """A help snapshot, not a string search: `run` and `runner` differ."""

        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "orbit", "--help"],
            capture_output=True, text=True, cwd=str(ROOT),
            env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(0, result.returncode, result.stderr)
        advertised = set(
            re.findall(r"^\s{4}(\w[\w-]*)", result.stdout, flags=re.MULTILINE)
        )
        for command in REMOVED_COMMANDS:
            with self.subTest(command=command):
                self.assertNotIn(command, advertised)
        self.assertIn("serve", advertised)
        self.assertIn("run", advertised)

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

    def test_nothing_in_the_package_imports_the_legacy_engine(self) -> None:
        """Including the CLI entry point, which used to be the last holdout."""

        offenders: list[str] = []
        for path in SRC.rglob("*.py"):
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
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
