from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from dev_loop.project_index import (
    browser_host,
    list_projects,
    project_id,
    server_url,
    upsert_project,
)


class ProjectIndexTests(unittest.TestCase):
    def test_project_id_is_stable_for_resolved_root(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            child = root / "pkg"
            child.mkdir(parents=True)
            (root / ".git").mkdir()

            self.assertEqual(project_id(root), project_id(child.parent))

    def test_browser_host_prefers_loopback_for_wildcard_binds(self):
        self.assertEqual("127.0.0.1", browser_host("0.0.0.0"))
        self.assertEqual("127.0.0.1", browser_host("::"))
        self.assertEqual("localhost", browser_host("localhost"))
        self.assertEqual("http://127.0.0.1:9000", server_url("0.0.0.0", 9000))

    def test_upsert_project_writes_and_replaces_index_entry(self):
        with TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "index.json"
            root = Path(tmp) / "repo"
            root.mkdir()
            (root / "pyproject.toml").write_text("[project]\nname='x'\n")
            db_path = Path(tmp) / "messages.db"

            first = upsert_project(root, db_path, "127.0.0.1", 8848, index_path)
            second = upsert_project(root, db_path, "127.0.0.1", 9000, index_path)

            self.assertEqual(first["id"], second["id"])
            projects = list_projects(
                current_project_id=second["id"],
                index_path=index_path,
                online_checker=lambda project: False,
            )
            self.assertEqual(1, len(projects))
            self.assertEqual("http://127.0.0.1:9000", projects[0]["server_url"])
            self.assertTrue(projects[0]["current"])
            self.assertTrue(projects[0]["online"])

    def test_list_projects_marks_non_current_with_checker(self):
        with TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "index.json"
            root_a = Path(tmp) / "a"
            root_b = Path(tmp) / "b"
            root_a.mkdir()
            root_b.mkdir()
            current = upsert_project(root_a, Path(tmp) / "a.db", "127.0.0.1", 8848, index_path)
            other = upsert_project(root_b, Path(tmp) / "b.db", "127.0.0.1", 9000, index_path)

            projects = list_projects(
                current_project_id=current["id"],
                index_path=index_path,
                online_checker=lambda project: project["id"] == other["id"],
            )

            by_id = {project["id"]: project for project in projects}
            self.assertTrue(by_id[current["id"]]["online"])
            self.assertTrue(by_id[current["id"]]["current"])
            self.assertTrue(by_id[other["id"]]["online"])
            self.assertFalse(by_id[other["id"]]["current"])


if __name__ == "__main__":
    unittest.main()
