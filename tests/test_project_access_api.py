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
from orbit.web.api_v1 import (
    OPS_WRITE_SCOPE, READ_SCOPE, WRITE_SCOPE, Authorizer,
)
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
        (self.project / "tracked.txt").write_text("before\n")

    def app(self, **kwargs):
        app = create_app(
            self.db, handlers=[transform_registration()], schemas=SCHEMAS,
            langgraph_state_directory=self.root / "langgraph",
            workspace_path=self.project,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(
                lambda actor: [READ_SCOPE] if actor == "reader"
                else [READ_SCOPE, WRITE_SCOPE, OPS_WRITE_SCOPE]
            ),
            single_goal_mode=False, **kwargs,
        )
        from orbit.platform.project_occupancy import ProjectOccupancyRegistry
        access = app.state.runtime.langgraph_service.project_access
        if access is not None:
            access.registry = ProjectOccupancyRegistry(self.root / "occupancy")
        return app

    def test_corrupt_record_is_reported_instead_of_500(self):
        from orbit.platform.project_occupancy import ProjectOccupancyRegistry
        app = self.app(discover_agents=True, agent_project_write=True)
        access = app.state.runtime.langgraph_service.project_access
        access.registry = ProjectOccupancyRegistry(self.root / "occupancy")
        claim = access.registry.claim(self.project, run_id="broken")
        record = next((self.root / "occupancy").glob("*.json"))
        record.write_text("{")
        claim._lock.release()
        with AsgiHarness(app) as client:
            response = client.get("/api/v1/project-access", actor="local")
            self.assertEqual(200, response.status_code)
            self.assertEqual(record.name, response.json()["data"]["corrupt_records"][0]["record_id"])

    def test_an_abandoned_claim_can_be_resolved_from_the_page(self) -> None:
        """The other half of §7: seeing it and being able to clear it.

        The registry could always resolve one, but only from a Python prompt
        — which is not somewhere the operator reading the page can go.
        """

        app = self.app(discover_agents=True, agent_project_write=True)
        access = app.state.runtime.langgraph_service.project_access
        from orbit.workflow.langgraph_runtime.project_access import ProjectAccessNeed

        access.acquire("run-1", ProjectAccessNeed(required=True, write=True))
        with AsgiHarness(app) as client:
            # While a Runtime still holds it, no confirmation clears it.
            access_held = client.post(
                "/api/v1/project-access/resolve", key="a", actor="local",
                body={"run_id": "run-1", "processes_stopped": True},
            )
            self.assertEqual(409, access_held.status_code)

            access.abandon("run-1")  # as a stopped Runtime leaves it
            commands = client.get(
                "/api/v1/project-access", actor="local",
            ).json()["data"]["allowed_commands"]
            self.assertEqual(1, len(commands))
            command = commands[0]
            self.assertEqual("POST", command["method"])
            self.assertEqual("project_access.resolve", command["command"])
            self.assertEqual("explicit", command["confirmation"])
            self.assertEqual(
                {"run_id": "run-1", "processes_stopped": False,
                 "claim_token": command["payload"]["claim_token"]},
                command["payload"],
            )
            unconfirmed = client.post(
                "/api/v1/project-access/resolve", key="b", actor="local",
                body={"run_id": "run-1"},
            )
            self.assertEqual(409, unconfirmed.status_code)
            self.assertIn("processes_stopped", unconfirmed.json()["error"]["message"])

            unnamed = client.post(
                "/api/v1/project-access/resolve", key="b2", actor="local",
                body={"run_id": "run-1", "processes_stopped": True},
            )
            self.assertEqual(409, unnamed.status_code)
            self.assertIn("claim_token", unnamed.json()["error"]["message"])

            resolved = client.post(
                command["href"], key="c", actor="local",
                body={**command["payload"], "processes_stopped": True},
            )
            self.assertEqual(["run-1"], resolved.json()["data"]["resolved"])
            after = client.get("/api/v1/project-access", actor="local").json()["data"]
        self.assertEqual([], after["occupancies"])
        self.assertFalse(after["recovery_required"])
        self.assertEqual([], after["allowed_commands"])

    def test_a_confirmation_read_before_a_new_hold_does_not_clear_it(self) -> None:
        """The page was read about one hold and answered about another.

        The run is resolved, recovers, takes the project again, and that
        Runtime stops too — same run id, same record file, and an Agent
        nobody has been to check. The advertised payload of the first read
        must not clear the second hold.
        """

        app = self.app(discover_agents=True, agent_project_write=True)
        access = app.state.runtime.langgraph_service.project_access
        from orbit.workflow.langgraph_runtime.project_access import ProjectAccessNeed

        need = ProjectAccessNeed(required=True, write=True)
        access.acquire("run-1", need)
        access.abandon("run-1")
        with AsgiHarness(app) as client:
            stale = client.get(
                "/api/v1/project-access", actor="local",
            ).json()["data"]["allowed_commands"][0]["payload"]

            access.registry.resolve(
                "run-1", expected_claim=stale["claim_token"],
                processes_stopped=True,
            )
            access.acquire("run-1", need)      # the second hold
            access.abandon("run-1")

            refused = client.post(
                "/api/v1/project-access/resolve", key="a", actor="local",
                body={**stale, "processes_stopped": True},
            )
            self.assertEqual(409, refused.status_code)
            self.assertIn("changed since", refused.json()["error"]["message"])
            after = client.get("/api/v1/project-access", actor="local").json()["data"]
        self.assertEqual(["run-1"], [item["run_id"] for item in after["occupancies"]])

    def test_reader_cannot_discover_or_execute_repair_commands(self) -> None:
        app = self.app(discover_agents=True, agent_project_write=True)
        access = app.state.runtime.langgraph_service.project_access
        with AsgiHarness(app) as client:
            for corrupt in (False, True):
                with self.subTest(corrupt=corrupt):
                    claim = access.registry.claim(self.project, run_id="repair")
                    claim._lock.release()
                    if corrupt:
                        next((self.root / "occupancy").glob("*.json")).write_text("{")
                    admin = client.get("/api/v1/project-access", actor="local").json()["data"]
                    command = admin["allowed_commands"][0]
                    reader = client.get("/api/v1/project-access", actor="reader").json()["data"]
                    self.assertTrue(reader["recovery_required"])
                    self.assertEqual([], reader["allowed_commands"])
                    denied = client.post(command["href"], key=f"reader-{corrupt}",
                        actor="reader", body={**command["payload"], "processes_stopped": True})
                    self.assertEqual(403, denied.status_code)
                    # An advertised payload must still require a real confirmation.
                    unconfirmed = client.post(command["href"], key=f"unconfirmed-{corrupt}",
                        actor="local", body=command["payload"])
                    self.assertEqual(409, unconfirmed.status_code)
                    access.registry.resolve(
                        "repair", processes_stopped=True,
                        expected_claim=command["payload"]["claim_token"],
                    )

    def test_a_corrupt_record_can_be_resolved_by_its_file_name(self) -> None:
        app = self.app(discover_agents=True, agent_project_write=True)
        access = app.state.runtime.langgraph_service.project_access
        claim = access.registry.claim(self.project, run_id="broken")
        record = next((self.root / "occupancy").glob("*.json"))
        record.write_text("{")
        claim._lock.release()
        with AsgiHarness(app) as client:
            listed = client.get("/api/v1/project-access", actor="local").json()["data"]
            self.assertTrue(listed["recovery_required"])
            command = listed["allowed_commands"][0]
            self.assertEqual(record.name, command["payload"]["record_id"])
            self.assertNotIn("run_id", command["payload"])
            response = client.post(
                command["href"], key="a", actor="local",
                body={**command["payload"], "processes_stopped": True},
            )
        self.assertEqual(200, response.status_code)
        self.assertFalse(record.exists())
        access.registry.claim(self.project, run_id="next").release()

    def test_it_says_so_when_the_runtime_grants_nothing(self) -> None:
        with AsgiHarness(self.app()) as client:
            payload = client.get("/api/v1/project-access", actor="local").json()["data"]

        self.assertFalse(payload["enabled"])
        self.assertEqual([], payload["allowed_commands"])
        self.assertIn("grants no project-directory access", payload["reason"])

    def test_it_reports_the_project_and_what_was_granted(self) -> None:
        app = self.app(discover_agents=True, agent_project_write=True)
        with AsgiHarness(app) as client:
            payload = client.get("/api/v1/project-access", actor="local").json()["data"]

        self.assertTrue(payload["enabled"])
        self.assertTrue(payload["write_granted"])
        self.assertEqual(str(self.project.resolve()), payload["project_root"])
        self.assertEqual([], payload["occupancies"])
        self.assertEqual([], payload["allowed_commands"])

    def test_a_held_project_names_its_holder_and_its_way_back(self) -> None:
        app = self.app(discover_agents=True, agent_project_write=True)
        access = app.state.runtime.langgraph_service.project_access
        from orbit.workflow.langgraph_runtime.project_access import ProjectAccessNeed

        access.acquire("run-1", ProjectAccessNeed(required=True, write=True))
        self.addCleanup(access.release, "run-1", "completed")

        with AsgiHarness(app) as client:
            payload = client.get("/api/v1/project-access", actor="local").json()["data"]

        held = payload["occupancies"][0]
        self.assertEqual([], payload["allowed_commands"])
        self.assertEqual("run-1", held["run_id"])
        self.assertTrue(held["holder_live"])
        self.assertEqual("unprotected_direct", held["recovery"]["kind"])
        self.assertIn("no automatic rollback", held["recovery"]["uncovered"][0])
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

    def test_a_non_git_run_reports_that_no_complete_diff_is_available(self) -> None:
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
        self.assertEqual("unprotected_direct", summary["kind"])
        self.assertEqual([], summary["content"])
        self.assertIn("no automatic", summary["error"])
        self.assertFalse(payload["complete_record"])
        self.assertIn("not a complete filesystem diff", payload["note"])


if __name__ == "__main__":
    unittest.main()
