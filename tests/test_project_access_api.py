"""The project-access surface a person can actually look at (§7).

Everything these endpoints report already existed — in a coordinator's
memory, in an occupancy record on disk, in a git ref — and nowhere anybody
could see it. A project held by a run somebody has to go and answer is the
single thing most worth being able to look up.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.test_web_composition import (
    AsgiHarness, SCHEMAS, publish_linear_workflow, transform_registration,
)
from orbit.web.api_v1 import READ_SCOPE, WRITE_SCOPE, Authorizer
from orbit.web.app import create_app


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)


class ProjectAccessEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "runtime.db"
        publish_linear_workflow(self.db)
        self.project = self.root / "project"
        self.project.mkdir()
        git(self.project, "init", "--initial-branch=main")
        git(self.project, "config", "user.email", "t@e.com")
        git(self.project, "config", "user.name", "T")
        (self.project / "tracked.txt").write_text("before\n")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-m", "init")

    def app(self, **kwargs):
        return create_app(
            self.db, handlers=[transform_registration()], schemas=SCHEMAS,
            langgraph_state_directory=self.root / "langgraph",
            workspace_path=self.project,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: [READ_SCOPE, WRITE_SCOPE]),
            single_goal_mode=False, **kwargs,
        )

    def test_it_says_so_when_the_runtime_grants_nothing(self) -> None:
        with AsgiHarness(self.app()) as client:
            payload = client.get("/api/v1/project-access", actor="local").json()["data"]

        self.assertFalse(payload["enabled"])
        self.assertIn("grants no project-directory access", payload["reason"])

    def test_it_reports_the_project_and_what_was_granted(self) -> None:
        app = self.app(discover_agents=True, agent_project_write=True)
        with AsgiHarness(app) as client:
            payload = client.get("/api/v1/project-access", actor="local").json()["data"]

        self.assertTrue(payload["enabled"])
        self.assertTrue(payload["write_granted"])
        self.assertEqual(str(self.project.resolve()), payload["project_root"])
        self.assertEqual([], payload["occupancies"])

    def test_a_held_project_names_its_holder_and_its_way_back(self) -> None:
        app = self.app(discover_agents=True, agent_project_write=True)
        access = app.state.runtime.langgraph_service.project_access
        from orbit.workflow.langgraph_runtime.project_access import ProjectAccessNeed

        access.acquire("run-1", ProjectAccessNeed(required=True, write=True))
        self.addCleanup(access.release, "run-1", "completed")

        with AsgiHarness(app) as client:
            payload = client.get("/api/v1/project-access", actor="local").json()["data"]

        held = payload["occupancies"][0]
        self.assertEqual("run-1", held["run_id"])
        self.assertTrue(held["holder_live"])
        self.assertEqual("git", held["recovery"]["kind"])
        self.assertIn(
            "untracked files git is not ignoring", held["recovery"]["covered"],
        )
        self.assertEqual([], payload["needs_recovery"])

    def test_a_claim_whose_runtime_is_gone_is_flagged_for_recovery(self) -> None:
        """Not "free": §4 requires somebody to resolve it, and the page is
        where they would find out it needs resolving."""

        app = self.app(discover_agents=True, agent_project_write=True)
        access = app.state.runtime.langgraph_service.project_access
        from orbit.workflow.langgraph_runtime.project_access import ProjectAccessNeed

        access.acquire("run-1", ProjectAccessNeed(required=True, write=True))
        access.abandon("run-1")  # as a stopped Runtime leaves it

        with AsgiHarness(app) as client:
            payload = client.get("/api/v1/project-access", actor="local").json()["data"]

        self.assertEqual(["run-1"], payload["needs_recovery"])
        self.assertFalse(payload["occupancies"][0]["holder_live"])


class RunChangesEndpointTests(ProjectAccessEndpointTests):
    def start_run(self, client):
        return client.post(
            "/api/v1/langgraph-runs", key="k", actor="local",
            body={
                "workflow_id": "workflow:linear", "input": {"value": 1},
                "wait": True,
            },
        ).json()["data"]["run"]

    def test_an_unknown_run_is_not_found(self) -> None:
        with AsgiHarness(self.app()) as client:
            response = client.get("/api/v1/langgraph-runs/nope/changes", actor="local")
        self.assertEqual(404, response.status_code)

    def test_a_run_that_held_no_project_reports_no_git_comparison(self) -> None:
        with AsgiHarness(self.app()) as client:
            run = self.start_run(client)
            payload = client.get(
                f"/api/v1/langgraph-runs/{run['run_id']}/changes", actor="local",
            ).json()["data"]

        self.assertIsNone(payload["git"])
        # Always, and on purpose: this endpoint never claims to be a complete
        # filesystem diff, whatever it managed to compare.
        self.assertFalse(payload["complete_record"])

    def test_a_run_that_held_the_project_reports_what_git_saw(self) -> None:
        app = self.app(discover_agents=True, agent_project_write=True)
        access = app.state.runtime.langgraph_service.project_access
        from orbit.workflow.langgraph_runtime.project_access import ProjectAccessNeed

        with AsgiHarness(app) as client:
            run = self.start_run(client)
            access.acquire(run["run_id"], ProjectAccessNeed(required=True, write=True))
            self.addCleanup(access.release, run["run_id"], "completed")
            (self.project / "tracked.txt").write_text("AGENT WROTE\n")
            (self.project / "made.txt").write_text("new\n")

            payload = client.get(
                f"/api/v1/langgraph-runs/{run['run_id']}/changes", actor="local",
            ).json()["data"]

        summary = payload["git"]
        self.assertEqual("run_cumulative", summary["scope"])
        self.assertIn(
            {"path": "tracked.txt", "status": "modified"}, summary["content"],
        )
        self.assertIn({"path": "made.txt", "status": "added"}, summary["content"])
        self.assertFalse(payload["complete_record"])
        self.assertIn("not a complete filesystem diff", payload["note"])


if __name__ == "__main__":
    unittest.main()
