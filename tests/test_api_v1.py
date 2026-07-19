"""M3: the versioned HTTP surface.

Gate M3: DTO/cursor/error contracts, read and write authorisation, idempotency,
version conflict, pagination, and no state-changing route outside /api/v1.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from orbit.web.api_v1 import Authorizer, READ_SCOPE, SENSITIVE_SCOPE, WRITE_SCOPE
from orbit.web.app import create_app
from orbit.workflow.api.dto import (
    CursorError, decode_cursor, encode_cursor, envelope, page_size,
)
from orbit.workflow.application.budget_service import BudgetService
from orbit.workflow.application.human_service import HumanTaskService
from orbit.workflow.domain.human import HumanTaskKind
from orbit.workflow.domain.ids import EntityId
from tests.test_web_composition import (
    AsgiHarness, SCHEMAS, publish_linear_workflow, transform_registration,
)


class CursorTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        cursor = encode_cursor({"position": 42})
        self.assertEqual({"position": 42}, decode_cursor(cursor))

    def test_cursor_is_opaque(self) -> None:
        cursor = encode_cursor({"position": 42})
        self.assertNotIn("42", cursor)
        self.assertNotIn("position", cursor)

    def test_garbage_cursor_is_rejected(self) -> None:
        for bad in ("not-base64!!", encode_cursor({}) + "@@", "eyJhIjo="):
            with self.subTest(bad=bad):
                try:
                    decode_cursor(bad)
                except CursorError:
                    continue
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"wrong error type: {type(exc).__name__}: {exc}")

    def test_empty_cursor_is_the_start(self) -> None:
        self.assertEqual({}, decode_cursor(None))
        self.assertEqual({}, decode_cursor(""))


class PageSizeTests(unittest.TestCase):
    def test_default_and_bounds(self) -> None:
        self.assertEqual(50, page_size(None))
        self.assertEqual(10, page_size("10"))
        for bad in ("0", "-1", "201", "abc"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    page_size(bad)


class EnvelopeTests(unittest.TestCase):
    def test_shape_is_stable(self) -> None:
        payload = envelope({"x": 1}, projection_version=7, next_cursor="abc")
        self.assertEqual(
            {"schema_version", "projection_version", "data", "next_cursor"},
            set(payload),
        )
        self.assertEqual("1.0", payload["schema_version"])


class ApiTestCase(unittest.TestCase):
    """Boots the real composition root with a scriptable authenticator."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "runtime.db"
        self.scopes = {
            "reader": [READ_SCOPE],
            "writer": [READ_SCOPE, WRITE_SCOPE],
            "second-writer": [READ_SCOPE, WRITE_SCOPE],
            "sensitive": [READ_SCOPE, SENSITIVE_SCOPE],
            "nobody": [],
        }
        self.app = create_app(
            self.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            worker_count=1, poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: self.scopes.get(actor, [])),
        )
        publish_linear_workflow(self.db)

    def tearDown(self) -> None:
        self.temp.cleanup()


class ReadAuthTests(ApiTestCase):
    def test_missing_credentials_are_rejected(self) -> None:
        with AsgiHarness(self.app) as client:
            response = client.get("/api/v1/runs")
            self.assertEqual(401, response.status_code)
            self.assertEqual("unauthenticated", response.json()["error"]["code"])

    def test_actor_without_scope_is_forbidden(self) -> None:
        with AsgiHarness(self.app) as client:
            response = client.get("/api/v1/runs", actor="nobody")
            self.assertEqual(403, response.status_code)
            self.assertEqual("forbidden", response.json()["error"]["code"])

    def test_reader_can_list_runs(self) -> None:
        with AsgiHarness(self.app) as client:
            response = client.get("/api/v1/runs", actor="reader")
            self.assertEqual(200, response.status_code, response.text)
            body = response.json()
            self.assertEqual("1.0", body["schema_version"])
            self.assertEqual([], body["data"]["runs"])

    def test_reader_cannot_write(self) -> None:
        with AsgiHarness(self.app) as client:
            response = client.post(
                "/api/v1/runs", actor="reader", key="k1",
                body={"workflow_id": "workflow:linear"},
            )
            self.assertEqual(403, response.status_code)


class RunLifecycleTests(ApiTestCase):
    def _start(self, client, key="start-1", **overrides):
        body = {"workflow_id": "workflow:linear", "input": {"value": 0}}
        body.update(overrides)
        return client.post("/api/v1/runs", actor="writer", key=key, body=body)

    def test_start_run_returns_a_run_id(self) -> None:
        with AsgiHarness(self.app) as client:
            response = self._start(client)
            self.assertEqual(200, response.status_code, response.text)
            data = response.json()["data"]
            self.assertTrue(data["run_id"].startswith("run:"))
            self.assertEqual(1, data["workflow_version"])

    def test_same_key_replays_rather_than_starting_twice(self) -> None:
        with AsgiHarness(self.app) as client:
            first = self._start(client, key="dup")
            second = self._start(client, key="dup")
            self.assertEqual(200, second.status_code)
            self.assertEqual(
                first.json()["data"]["run_id"], second.json()["data"]["run_id"]
            )
            listed = client.get("/api/v1/runs", actor="reader").json()
            self.assertEqual(1, len(listed["data"]["runs"]))

    def test_same_key_with_a_different_body_conflicts(self) -> None:
        with AsgiHarness(self.app) as client:
            self._start(client, key="clash")
            response = self._start(client, key="clash", input={"value": 9})
            self.assertEqual(409, response.status_code)
            self.assertEqual(
                "idempotency_conflict", response.json()["error"]["code"]
            )

    def test_missing_idempotency_key_is_rejected(self) -> None:
        with AsgiHarness(self.app) as client:
            response = client.post(
                "/api/v1/runs", actor="writer", key=None,
                body={"workflow_id": "workflow:linear"},
            )
            self.assertEqual(400, response.status_code)

    def test_unknown_workflow_is_a_client_error(self) -> None:
        with AsgiHarness(self.app) as client:
            response = self._start(client, key="missing", workflow_id="workflow:nope")
            self.assertEqual(409, response.status_code)
            self.assertEqual("invalid_command", response.json()["error"]["code"])

    def test_summary_and_responsibilities_are_readable(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id = self._start(client, key="detail").json()["data"]["run_id"]

            summary = client.get(f"/api/v1/runs/{run_id}", actor="reader")
            self.assertEqual(200, summary.status_code, summary.text)
            self.assertIsNotNone(summary.json()["projection_version"])

            responsibilities = client.get(
                f"/api/v1/runs/{run_id}/responsibilities", actor="reader"
            )
            self.assertEqual(200, responsibilities.status_code)
            items = responsibilities.json()["data"]["responsibilities"]
            # Every action the UI may offer has to come from the server.
            for item in items:
                for command in item["allowed_commands"]:
                    self.assertIn("expected_version", command)
                    self.assertIn("target_aggregate_id", command)

    def test_cancel_requires_the_current_version(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id = self._start(client, key="cancel").json()["data"]["run_id"]
            stale = client.post(
                f"/api/v1/runs/{run_id}/cancel", actor="writer", key="c1",
                body={"expected_version": 999},
            )
            self.assertEqual(409, stale.status_code)

    def test_unknown_run_is_404(self) -> None:
        with AsgiHarness(self.app) as client:
            response = client.get("/api/v1/runs/run:missing", actor="reader")
            self.assertEqual(404, response.status_code)


class PaginationTests(ApiTestCase):
    def test_run_list_pages_with_an_opaque_cursor(self) -> None:
        with AsgiHarness(self.app) as client:
            for index in range(3):
                client.post(
                    "/api/v1/runs", actor="writer", key=f"page-{index}",
                    body={"workflow_id": "workflow:linear", "input": {"value": index}},
                )
            first = client.get("/api/v1/runs?limit=2", actor="reader").json()
            self.assertEqual(2, len(first["data"]["runs"]))
            self.assertIsNotNone(first["next_cursor"])

            second = client.get(
                f"/api/v1/runs?limit=2&cursor={first['next_cursor']}", actor="reader"
            ).json()
            self.assertEqual(1, len(second["data"]["runs"]))
            self.assertIsNone(second["next_cursor"])

            seen = [item["run_id"] for item in first["data"]["runs"]]
            seen += [item["run_id"] for item in second["data"]["runs"]]
            self.assertEqual(3, len(set(seen)))

    def test_bad_cursor_is_rejected(self) -> None:
        with AsgiHarness(self.app) as client:
            response = client.get("/api/v1/runs?cursor=%21%21bad", actor="reader")
            self.assertEqual(400, response.status_code)
            self.assertEqual("invalid_cursor", response.json()["error"]["code"])

    def test_oversized_limit_is_rejected(self) -> None:
        with AsgiHarness(self.app) as client:
            response = client.get("/api/v1/runs?limit=9999", actor="reader")
            self.assertEqual(400, response.status_code)

    def test_timeline_pages(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id = client.post(
                "/api/v1/runs", actor="writer", key="tl",
                body={"workflow_id": "workflow:linear", "input": {"value": 0}},
            ).json()["data"]["run_id"]
            page = client.get(
                f"/api/v1/runs/{run_id}/timeline?limit=1", actor="reader"
            ).json()
            self.assertEqual(1, len(page["data"]["items"]))
            self.assertIsNotNone(page["next_cursor"])
            self.assertIn("correlation_id", page["data"]["items"][0])


class PlanApiTests(ApiTestCase):
    def _run(self, client):
        return client.post(
            "/api/v1/runs", actor="writer", key="plan-run",
            body={"workflow_id": "workflow:linear", "input": {"value": 0}},
        ).json()["data"]["run_id"]

    def test_definition_and_overlay_are_separate_endpoints(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id = self._run(client)

            definition = client.get(f"/api/v1/runs/{run_id}/plan", actor="reader")
            self.assertEqual(200, definition.status_code, definition.text)
            nodes = definition.json()["data"]["nodes"]
            self.assertTrue(nodes)
            for node in nodes:
                self.assertNotIn("status", node)

            overlay = client.get(
                f"/api/v1/runs/{run_id}/plan/overlay", actor="reader"
            )
            self.assertEqual(200, overlay.status_code, overlay.text)
            for node in overlay.json()["data"]["nodes"]:
                self.assertIn("status", node)
                self.assertNotIn("handler_name", node)

    def test_overlay_names_the_plan_version_it_describes(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id = self._run(client)
            overlay = client.get(
                f"/api/v1/runs/{run_id}/plan/overlay", actor="reader"
            ).json()["data"]
            definition = client.get(
                f"/api/v1/runs/{run_id}/plan", actor="reader"
            ).json()["data"]
            self.assertEqual(definition["plan_version"], overlay["plan_version"])

    def test_a_diff_needs_both_versions(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id = self._run(client)
            response = client.get(f"/api/v1/runs/{run_id}/plan/diff", actor="reader")
            self.assertEqual(400, response.status_code)

    def test_a_run_diffed_against_itself_is_identical(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id = self._run(client)
            response = client.get(
                f"/api/v1/runs/{run_id}/plan/diff?base_version=1&target_version=1",
                actor="reader",
            )
            self.assertEqual(200, response.status_code, response.text)
            self.assertTrue(response.json()["data"]["identical"])

    def test_plan_reads_require_a_scope(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id = self._run(client)
            self.assertEqual(
                403,
                client.get(f"/api/v1/runs/{run_id}/plan", actor="nobody").status_code,
            )

    def test_an_unknown_run_has_no_plan(self) -> None:
        with AsgiHarness(self.app) as client:
            response = client.get("/api/v1/runs/run:missing/plan", actor="reader")
            self.assertEqual(404, response.status_code)


class DataApiTests(ApiTestCase):
    def test_run_data_is_paged_and_lineage_is_run_scoped(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id = client.post(
                "/api/v1/runs", actor="writer", key="data-run",
                body={"workflow_id": "workflow:linear", "input": {"value": 7}},
            ).json()["data"]["run_id"]
            response = client.get(
                f"/api/v1/runs/{run_id}/data?limit=1", actor="sensitive"
            )
            self.assertEqual(200, response.status_code, response.text)
            items = response.json()["data"]["items"]
            self.assertEqual(1, len(items))
            self.assertEqual("value", items[0]["kind"])
            self.assertNotIn("blob_key", items[0])

            lineage = client.get(
                f"/api/v1/runs/{run_id}/data/{items[0]['data_id']}/lineage",
                actor="sensitive",
            )
            self.assertEqual(200, lineage.status_code, lineage.text)
            self.assertEqual(items[0]["data_id"], lineage.json()["data"]["data_id"])

    def test_data_reads_require_scope_and_matching_run(self) -> None:
        with AsgiHarness(self.app) as client:
            self.assertEqual(
                403,
                client.get("/api/v1/runs/run:missing/data", actor="reader").status_code,
            )
            self.assertEqual(
                404,
                client.get("/api/v1/runs/run:missing/data", actor="sensitive").status_code,
            )


class CatalogTests(ApiTestCase):
    def test_handler_catalog_exposes_identity_not_commands(self) -> None:
        with AsgiHarness(self.app) as client:
            response = client.get("/api/v1/handler-catalog", actor="reader")
            self.assertEqual(200, response.status_code, response.text)
            handlers = response.json()["data"]["handlers"]
            self.assertEqual(1, len(handlers))
            entry = handlers[0]
            self.assertEqual("transform", entry["name"])
            self.assertIn("manifest_fingerprint", entry)
            # Nothing here may be pasteable into a shell.
            serialised = repr(entry)
            for forbidden in ("command", "argv", "path", "secret_value"):
                self.assertNotIn(forbidden, serialised.lower())

    def test_workflow_catalog_advertises_start_only_to_writers(self) -> None:
        with AsgiHarness(self.app) as client:
            reader = client.get("/api/v1/workflows", actor="reader")
            self.assertEqual(200, reader.status_code, reader.text)
            self.assertEqual([], reader.json()["data"]["workflows"][0]["allowed_commands"])

            writer = client.get("/api/v1/workflows", actor="writer")
            entry = writer.json()["data"]["workflows"][0]
            command = entry["allowed_commands"][0]
            self.assertEqual("run.start", command["command"])
            started = client.post(
                command["href"], actor="writer", key="catalog-start",
                body={
                    "workflow_id": entry["workflow_id"],
                    "workflow_version": entry["latest_version"],
                    "expected_version": command["expected_version"],
                    "input": {"value": 3},
                },
            )
            self.assertEqual(200, started.status_code, started.text)


class CapabilityTests(ApiTestCase):
    def test_capabilities_declare_absence_with_a_reason(self) -> None:
        """Plan API-7: the client never learns 'not provided' from a 404."""
        with AsgiHarness(self.app) as client:
            self.assertEqual(401, client.get("/api/v1/capabilities").status_code)
            response = client.get("/api/v1/capabilities", actor="reader")
            self.assertEqual(200, response.status_code, response.text)
            caps = response.json()["data"]["capabilities"]
            self.assertTrue(caps["static_graph"]["available"])
            self.assertTrue(caps["human_tasks"]["available"])
            # This composition runs without discovery: absent features carry
            # their reason instead of silently missing keys.
            self.assertFalse(caps["planner"]["available"])
            self.assertEqual(
                "agent_discovery_disabled", caps["planner"]["reason"]
            )
            self.assertFalse(caps["foreach"]["available"])
            self.assertEqual(
                "not_reachable_from_dsl", caps["foreach"]["reason"]
            )


class InboxTests(ApiTestCase):
    def test_inbox_is_readable_and_empty_without_human_tasks(self) -> None:
        with AsgiHarness(self.app) as client:
            response = client.get("/api/v1/inbox", actor="reader")
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual([], response.json()["data"]["items"])


class HumanTaskCommandTests(ApiTestCase):
    def _run_with_task(self, client):
        run_id = client.post(
            "/api/v1/runs", actor="writer", key="human-run",
            body={"workflow_id": "workflow:linear", "input": {"value": 0}},
        ).json()["data"]["run_id"]
        task_id, token = HumanTaskService(self.db).create(
            EntityId.parse(run_id), HumanTaskKind.APPROVAL, {"question": "ship?"},
            actor="writer", now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            participants=["writer"],
        )
        return run_id, str(task_id), token

    def test_approval_flows_through_submit(self) -> None:
        with AsgiHarness(self.app) as client:
            _run, task_id, token = self._run_with_task(client)
            response = client.post(
                f"/api/v1/human-tasks/{task_id}/submit", actor="writer", key="s1",
                body={
                    "submission_token": token, "decision": "approve",
                    "expected_version": 1,
                },
            )
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual("completed", response.json()["data"]["status"])

    def test_wrong_token_is_forbidden_not_a_bad_request(self) -> None:
        with AsgiHarness(self.app) as client:
            _run, task_id, _token = self._run_with_task(client)
            response = client.post(
                f"/api/v1/human-tasks/{task_id}/submit", actor="writer", key="s2",
                body={
                    "submission_token": "guessed", "decision": "approve",
                    "expected_version": 1,
                },
            )
            self.assertEqual(403, response.status_code)

    def test_stale_version_is_rejected(self) -> None:
        with AsgiHarness(self.app) as client:
            _run, task_id, token = self._run_with_task(client)
            response = client.post(
                f"/api/v1/human-tasks/{task_id}/submit", actor="writer", key="s3",
                body={
                    "submission_token": token, "decision": "approve",
                    "expected_version": 99,
                },
            )
            self.assertEqual(409, response.status_code)

    def test_missing_expected_version_is_rejected(self) -> None:
        with AsgiHarness(self.app) as client:
            _run, task_id, token = self._run_with_task(client)
            response = client.post(
                f"/api/v1/human-tasks/{task_id}/submit", actor="writer", key="s4",
                body={"submission_token": token, "decision": "approve"},
            )
            self.assertEqual(409, response.status_code)

    def test_claim_requires_write_scope(self) -> None:
        with AsgiHarness(self.app) as client:
            _run, task_id, _token = self._run_with_task(client)
            response = client.post(
                f"/api/v1/human-tasks/{task_id}/claim", actor="reader", key="c",
                body={"expected_version": 1},
            )
            self.assertEqual(403, response.status_code)

    def test_reissue_rotates_the_token_and_bumps_the_version(self) -> None:
        with AsgiHarness(self.app) as client:
            _run, task_id, original = self._run_with_task(client)
            reissued = client.post(
                f"/api/v1/human-tasks/{task_id}/token", actor="writer", key="t1",
                body={"expected_version": 1},
            )
            self.assertEqual(200, reissued.status_code, reissued.text)
            data = reissued.json()["data"]
            self.assertEqual(2, data["expected_version"])
            self.assertNotEqual(original, data["submission_token"])

            # The original token died the moment the new one was minted.
            stale = client.post(
                f"/api/v1/human-tasks/{task_id}/submit", actor="writer", key="t2",
                body={
                    "submission_token": original, "decision": "approve",
                    "expected_version": 2,
                },
            )
            self.assertEqual(403, stale.status_code)

            fresh = client.post(
                f"/api/v1/human-tasks/{task_id}/submit", actor="writer", key="t3",
                body={
                    "submission_token": data["submission_token"],
                    "decision": "approve", "expected_version": 2,
                },
            )
            self.assertEqual(200, fresh.status_code, fresh.text)
            self.assertEqual("completed", fresh.json()["data"]["status"])

    def test_reissue_is_refused_for_a_stranger(self) -> None:
        # "reader" holds only the read scope, so use a second writer-scoped
        # actor who is neither participant, assignee, claimer nor creator.
        with AsgiHarness(self.app) as client:
            _run, task_id, _token = self._run_with_task(client)
            response = client.post(
                f"/api/v1/human-tasks/{task_id}/token", actor="second-writer",
                key="t4", body={"expected_version": 1},
            )
            self.assertEqual(403, response.status_code, response.text)

    def test_inbox_advertises_the_token_command(self) -> None:
        with AsgiHarness(self.app) as client:
            self._run_with_task(client)
            items = client.get("/api/v1/inbox", actor="writer").json()["data"]["items"]
            human = next(item for item in items if item["kind"] == "human")
            commands = {command["command"] for command in human["allowed_commands"]}
            self.assertIn("human.token", commands)

    def test_a_run_parked_on_a_person_can_still_be_cancelled(self) -> None:
        """Answering an approval and abandoning the run are different acts.

        Without cancel on a human responsibility, a run waiting for someone who
        will never answer has no exit at all.
        """

        with AsgiHarness(self.app) as client:
            run_id, _task_id, _token = self._run_with_task(client)
            items = client.get(
                f"/api/v1/runs/{run_id}/responsibilities", actor="writer"
            ).json()["data"]["responsibilities"]
            human = next(item for item in items if item["kind"] == "human")
            commands = {command["command"] for command in human["allowed_commands"]}
            self.assertIn("run.cancel", commands)
            self.assertIn("human.submit.approve", commands)

    def test_the_cancel_command_targets_the_run_not_the_task(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id, _task_id, _token = self._run_with_task(client)
            items = client.get(
                f"/api/v1/runs/{run_id}/responsibilities", actor="writer"
            ).json()["data"]["responsibilities"]
            human = next(item for item in items if item["kind"] == "human")
            cancel = next(
                c for c in human["allowed_commands"] if c["command"] == "run.cancel"
            )
            self.assertEqual(run_id, cancel["target_aggregate_id"])
            self.assertNotEqual(human["expected_version"], cancel["expected_version"])

    def test_task_appears_in_the_inbox(self) -> None:
        with AsgiHarness(self.app) as client:
            _run, task_id, _token = self._run_with_task(client)
            items = client.get("/api/v1/inbox", actor="reader").json()["data"]["items"]
            self.assertIn(task_id, [item["task_id"] for item in items])

    def test_read_only_actors_are_not_shown_write_commands(self) -> None:
        """Plan B1: a button that will 403 must never be advertised.

        Readers still see the responsibilities themselves — visibility is a
        read concern — but every command list they receive is empty.
        """
        with AsgiHarness(self.app) as client:
            run_id, _task_id, _token = self._run_with_task(client)

            inbox = client.get("/api/v1/inbox", actor="reader").json()["data"]["items"]
            self.assertTrue(inbox)
            self.assertTrue(all(item["allowed_commands"] == [] for item in inbox))

            items = client.get(
                f"/api/v1/runs/{run_id}/responsibilities", actor="reader"
            ).json()["data"]["responsibilities"]
            self.assertTrue(items)
            self.assertTrue(all(item["allowed_commands"] == [] for item in items))

            # The same projections offer the full command set to a writer.
            writer_inbox = client.get(
                "/api/v1/inbox", actor="writer"
            ).json()["data"]["items"]
            human = next(item for item in writer_inbox if item["kind"] == "human")
            commands = {c["command"] for c in human["allowed_commands"]}
            self.assertIn("human.submit.approve", commands)
            self.assertIn("human.token", commands)


class BudgetCommandTests(ApiTestCase):
    def _run_with_account(self, client):
        run_id = client.post(
            "/api/v1/runs", actor="writer", key="budget-run",
            body={"workflow_id": "workflow:linear", "input": {"value": 0}},
        ).json()["data"]["run_id"]
        self.account = BudgetService(self.db).open_account(
            EntityId.parse(run_id), 1_000, actor="writer",
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        return run_id

    def grant(self, client, run_id, *, key, amount=500, version=None):
        return client.post(
            f"/api/v1/runs/{run_id}/budget", actor="writer", key=key,
            body={
                "amount_microunits": amount,
                "expected_version": (
                    self.account.version.value if version is None else version
                ),
            },
        )

    def test_grant_reports_the_unit_with_the_numbers(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id = self._run_with_account(client)
            response = self.grant(client, run_id, key="b1")
            self.assertEqual(200, response.status_code, response.text)
            budget = response.json()["data"]["budget"]
            self.assertEqual(1_500, budget["total_microunits"])
            self.assertEqual("microunits", budget["unit"])

    def test_a_retried_grant_tops_up_once(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id = self._run_with_account(client)
            self.grant(client, run_id, key="b2")
            again = self.grant(client, run_id, key="b2")
            self.assertEqual(200, again.status_code)
            self.assertEqual(1_500, again.json()["data"]["budget"]["total_microunits"])

    def test_a_grant_without_a_version_is_refused(self) -> None:
        """The contract requires it; silently defaulting would defeat the point."""

        with AsgiHarness(self.app) as client:
            run_id = self._run_with_account(client)
            response = client.post(
                f"/api/v1/runs/{run_id}/budget", actor="writer", key="b3",
                body={"amount_microunits": 500},
            )
            self.assertEqual(409, response.status_code)
            self.assertIn("expected_version", response.json()["error"]["message"])

    def test_a_stale_version_is_a_version_conflict(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id = self._run_with_account(client)
            self.grant(client, run_id, key="b4")
            second = self.grant(client, run_id, key="b5")
            self.assertEqual(409, second.status_code)
            self.assertEqual("version_conflict", second.json()["error"]["code"])

    def test_the_advertised_command_carries_the_account_version(self) -> None:
        """Not the run's — they are different aggregates on different clocks."""

        with AsgiHarness(self.app) as client:
            run_id = self._run_with_account(client)
            # Exhaust it so the budget responsibility is advertised at all.
            budget = BudgetService(self.db)
            reservation = budget.reserve(
                EntityId.parse(run_id), EntityId("attempt", "f" * 64), 1_000,
                actor="writer", now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            budget.report_usage(
                reservation.reservation_id, 1, 1_000, actor="writer",
                now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )

            items = client.get(
                f"/api/v1/runs/{run_id}/responsibilities", actor="writer"
            ).json()["data"]["responsibilities"]
            entry = next(item for item in items if item["kind"] == "budget")
            command = next(
                c for c in entry["allowed_commands"] if c["command"] == "budget.add"
            )
            self.assertEqual(f"budget_account:{run_id}", command["target_aggregate_id"])

            # Using exactly what was advertised must work.
            applied = client.post(
                f"/api/v1/runs/{run_id}/budget", actor="writer", key="b6",
                body={
                    "amount_microunits": 250,
                    "expected_version": command["expected_version"],
                },
            )
            self.assertEqual(200, applied.status_code, applied.text)


class RecoveryCommandTests(ApiTestCase):
    def apply(self, client, action_ids, *, actor="writer", key="r"):
        return client.post(
            "/api/v1/recovery/apply", actor=actor, key=key,
            body={"action_ids": action_ids},
        )

    def test_scan_is_a_read_and_apply_is_a_write(self) -> None:
        with AsgiHarness(self.app) as client:
            scan = client.get("/api/v1/recovery", actor="reader")
            self.assertEqual(200, scan.status_code, scan.text)
            self.assertIn("findings", scan.json()["data"])
            self.assertTrue(all(
                not item["allowed_commands"]
                for item in scan.json()["data"]["findings"]
            ))

            denied = self.apply(client, ["X:y:1"], actor="reader", key="r1")
            self.assertEqual(403, denied.status_code)

    def test_applying_a_whole_scan_is_refused(self) -> None:
        """The operator judged a list they saw; a rescan is a different list."""

        with AsgiHarness(self.app) as client:
            for body in ({}, {"action_ids": []}, {"limit": 100}):
                with self.subTest(body=body):
                    response = client.post(
                        "/api/v1/recovery/apply", actor="writer", key=str(body),
                        body=body,
                    )
                    self.assertEqual(409, response.status_code)
                    self.assertIn("action_ids", response.json()["error"]["message"])

    def test_a_finding_that_no_longer_exists_is_stale_not_applied(self) -> None:
        with AsgiHarness(self.app) as client:
            response = self.apply(client, ["UNKNOWN_ATTEMPT:attempt:x:7"], key="r2")
            self.assertEqual(200, response.status_code, response.text)
            results = response.json()["data"]["results"]
            self.assertEqual(1, len(results))
            self.assertEqual("stale", results[0]["outcome"])

    def test_each_selection_is_reported_separately(self) -> None:
        with AsgiHarness(self.app) as client:
            response = self.apply(client, ["A:b:1", "C:d:2"], key="r3")
            outcomes = [item["action_id"] for item in response.json()["data"]["results"]]
            self.assertEqual(["A:b:1", "C:d:2"], outcomes)

    def test_malformed_selections_are_refused(self) -> None:
        with AsgiHarness(self.app) as client:
            for selection in ([""], [None], ["ok", 7], "not-a-list"):
                with self.subTest(selection=selection):
                    response = client.post(
                        "/api/v1/recovery/apply", actor="writer",
                        key=str(selection), body={"action_ids": selection},
                    )
                    self.assertEqual(409, response.status_code)

    def test_every_selection_is_audited_including_refusals(self) -> None:
        """"Why was this not recovered" is the next question an operator asks."""

        from orbit.workflow.persistence.database import connect_workflow_database

        with AsgiHarness(self.app) as client:
            self.apply(client, ["GONE:entity:1"], key="r4")

        with connect_workflow_database(self.db, read_only=True) as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT target_id, decision FROM audit_records"
                    " WHERE action = 'recovery.apply'"
                )
            ]
        self.assertEqual(
            [{"target_id": "GONE:entity:1", "decision": "stale"}], rows
        )


class SurfaceTests(ApiTestCase):
    def test_no_mutating_route_outside_api_v1(self) -> None:
        mutating = []
        for route in self.app.routes:
            methods = getattr(route, "methods", set()) or set()
            if methods & {"POST", "PUT", "PATCH", "DELETE"}:
                mutating.append(route.path)
        self.assertTrue(mutating)
        for path in mutating:
            # /mcp is the one other command surface, and it reaches the runtime
            # through the same application services and the same authorizer.
            self.assertTrue(
                path.startswith("/api/v1") or path == "/mcp",
                f"{path} can change state but is outside /api/v1",
            )


if __name__ == "__main__":
    unittest.main()
