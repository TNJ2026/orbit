"""M1A: project discovery, runtime database paths and the legacy sentinel.

Replaces `tests/test_project_index.py` (disposition: rewrite @ M1A) and pins
the migration rules that the plan's M1A gate calls out:

* a fresh project only ever produces `runtime.db`;
* a pre-migration database triggers exactly one warning and is never opened;
* `orbit serve`, `orbit workflow publish` and `orbit db check` agree on the
  default path.
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from orbit.platform import projects


class ProjectResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        self.nested = self.root / "src" / "deep"
        self.nested.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_subdirectory_resolves_to_the_project_root(self) -> None:
        self.assertEqual(self.root, projects.resolve_project_root(self.nested))

    def test_unmarked_directory_resolves_to_itself(self) -> None:
        with tempfile.TemporaryDirectory() as bare:
            path = Path(bare).resolve()
            self.assertEqual(path, projects.resolve_project_root(path))

    def test_state_dir_has_no_legacy_fallback(self) -> None:
        """`.dev_loop` must not resurrect itself as a state directory."""

        (self.root / ".dev_loop").mkdir()
        self.assertEqual(self.root / ".orbit", projects.project_state_dir(self.root))

    def test_project_id_is_stable_and_path_specific(self) -> None:
        first = projects.project_id(self.root)
        self.assertEqual(first, projects.project_id(self.nested.parent.parent))
        self.assertEqual(12, len(first))
        with tempfile.TemporaryDirectory() as other:
            self.assertNotEqual(first, projects.project_id(other))


class RuntimeDatabasePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        self.state = self.root / "state"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_default_database_is_runtime_db(self) -> None:
        path = projects.project_db_path(self.root, base_dir=self.state)
        self.assertEqual("runtime.db", path.name)
        self.assertNotIn("messages.db", str(path))

    def test_same_leaf_name_does_not_collide(self) -> None:
        with tempfile.TemporaryDirectory() as other_parent:
            twin = Path(other_parent) / self.root.name
            twin.mkdir()
            (twin / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            self.assertNotEqual(
                projects.project_db_path(self.root, base_dir=self.state),
                projects.project_db_path(twin, base_dir=self.state),
            )

    def test_path_is_stable_across_calls(self) -> None:
        self.assertEqual(
            projects.project_db_path(self.root, base_dir=self.state),
            projects.project_db_path(self.root, base_dir=self.state),
        )


class LegacySentinelTests(unittest.TestCase):
    """The legacy paths exist to warn, never to read."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        self.state = self.root / "state"
        self.legacy = (
            projects.project_db_dir(self.root, base_dir=self.state) / "messages.db"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _create_legacy_database(self) -> None:
        self.legacy.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.legacy)
        connection.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY)")
        connection.commit()
        connection.close()

    def test_silent_when_no_legacy_database_exists(self) -> None:
        messages: list[str] = []
        warned = projects.warn_about_legacy_database(
            self.root, base_dir=self.state, emit=messages.append
        )
        self.assertFalse(warned)
        self.assertEqual([], messages)

    def test_warns_once_and_refuses_to_import(self) -> None:
        self._create_legacy_database()
        messages: list[str] = []
        warned = projects.warn_about_legacy_database(
            self.root, base_dir=self.state, emit=messages.append
        )
        self.assertTrue(warned)
        self.assertEqual(1, len(messages))
        text = messages[0]
        self.assertIn(str(self.legacy), text)
        self.assertIn("NOT imported", text)
        # An import/copy hint would resurrect the dual-state problem the
        # cutover exists to remove.
        for forbidden in ("--db", "cp ", "import it", "migrate it"):
            self.assertNotIn(forbidden, text)

    def test_warning_path_never_opens_the_database(self) -> None:
        self._create_legacy_database()
        opened: list[object] = []
        real_open = builtins.open

        def tracking_open(*args, **kwargs):
            opened.append(args[0] if args else None)
            return real_open(*args, **kwargs)

        with mock.patch("sqlite3.connect", side_effect=AssertionError("opened db")):
            with mock.patch.object(builtins, "open", tracking_open):
                projects.warn_about_legacy_database(
                    self.root, base_dir=self.state, emit=lambda _: None
                )
        self.assertNotIn(self.legacy, [Path(item) for item in opened if item])

    def test_candidates_report_only_existing_paths(self) -> None:
        self.assertEqual(
            (), projects.legacy_database_candidates(self.root, base_dir=self.state)
        )
        self._create_legacy_database()
        self.assertEqual(
            (self.legacy,),
            projects.legacy_database_candidates(self.root, base_dir=self.state),
        )


class ProjectIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        self.index = self.root / "index.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_upsert_replaces_the_entry_for_the_same_project(self) -> None:
        projects.upsert_project(
            project_root=self.root, db_path="/tmp/a.db", port=8848,
            index_path=self.index,
        )
        projects.upsert_project(
            project_root=self.root, db_path="/tmp/b.db", port=9999,
            index_path=self.index,
        )
        entries = json.loads(self.index.read_text(encoding="utf-8"))["projects"]
        self.assertEqual(1, len(entries))
        self.assertEqual("/tmp/b.db", entries[0]["db_path"])
        self.assertEqual(9999, entries[0]["port"])

    def test_online_probe_uses_the_runtime_readiness_endpoint(self) -> None:
        requested: list[str] = []

        class Response:
            status = 200

            def __enter__(self): return self
            def __exit__(self, *exc): return False

        def fake_urlopen(url, timeout=0.0):
            requested.append(url)
            return Response()

        with mock.patch.object(projects, "urlopen", fake_urlopen):
            online = projects.is_project_online({"server_url": "http://127.0.0.1:8848"})

        self.assertTrue(online)
        self.assertEqual(["http://127.0.0.1:8848/health/ready"], requested)

    def test_listing_marks_the_current_project_online_without_probing(self) -> None:
        projects.upsert_project(
            project_root=self.root, db_path="/tmp/a.db", index_path=self.index
        )
        identifier = projects.project_id(self.root)

        def refuse(_project):
            raise AssertionError("the current project must not be probed")

        listed = projects.list_projects(
            current_project_id=identifier,
            index_path=self.index,
            online_checker=refuse,
        )
        self.assertEqual(1, len(listed))
        self.assertTrue(listed[0]["current"])
        self.assertTrue(listed[0]["online"])


class PlatformBoundaryTests(unittest.TestCase):
    """M1A gate: the platform layer knows nothing about any engine."""

    def test_platform_does_not_import_engine_or_domain(self) -> None:
        import ast

        root = Path(projects.__file__).parent
        offenders: list[str] = []
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if any(
                        part in {"server", "store", "workflow"}
                        for part in name.split(".")
                    ):
                        offenders.append(f"{path.name}:{node.lineno}:{name}")
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
