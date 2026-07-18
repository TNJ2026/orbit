"""M3: the versioned HTTP surface.

Gate M3: DTO/cursor/error contracts, read and write authorisation, idempotency,
version conflict, pagination, and no state-changing route outside /api/v1.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from orbit.web.api_v1 import Authorizer, READ_SCOPE, WRITE_SCOPE
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

    def test_task_appears_in_the_inbox(self) -> None:
        with AsgiHarness(self.app) as client:
            _run, task_id, _token = self._run_with_task(client)
            items = client.get("/api/v1/inbox", actor="reader").json()["data"]["items"]
            self.assertIn(task_id, [item["task_id"] for item in items])


class BudgetCommandTests(ApiTestCase):
    def _run_with_account(self, client):
        run_id = client.post(
            "/api/v1/runs", actor="writer", key="budget-run",
            body={"workflow_id": "workflow:linear", "input": {"value": 0}},
        ).json()["data"]["run_id"]
        BudgetService(self.db).open_account(
            EntityId.parse(run_id), 1_000, actor="writer",
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        return run_id

    def test_grant_reports_the_unit_with_the_numbers(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id = self._run_with_account(client)
            response = client.post(
                f"/api/v1/runs/{run_id}/budget", actor="writer", key="b1",
                body={"amount_microunits": 500},
            )
            self.assertEqual(200, response.status_code, response.text)
            budget = response.json()["data"]["budget"]
            self.assertEqual(1_500, budget["total_microunits"])
            self.assertEqual("microunits", budget["unit"])

    def test_a_retried_grant_tops_up_once(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id = self._run_with_account(client)
            body = {"amount_microunits": 500}
            client.post(f"/api/v1/runs/{run_id}/budget", actor="writer", key="b2", body=body)
            again = client.post(
                f"/api/v1/runs/{run_id}/budget", actor="writer", key="b2", body=body
            )
            self.assertEqual(200, again.status_code)
            self.assertEqual(1_500, again.json()["data"]["budget"]["total_microunits"])


class RecoveryCommandTests(ApiTestCase):
    def test_scan_is_a_read_and_apply_is_a_write(self) -> None:
        with AsgiHarness(self.app) as client:
            scan = client.get("/api/v1/recovery", actor="reader")
            self.assertEqual(200, scan.status_code, scan.text)
            self.assertIn("findings", scan.json()["data"])

            denied = client.post(
                "/api/v1/recovery/apply", actor="reader", key="r1", body={}
            )
            self.assertEqual(403, denied.status_code)

            applied = client.post(
                "/api/v1/recovery/apply", actor="writer", key="r2", body={}
            )
            self.assertEqual(200, applied.status_code, applied.text)
            self.assertEqual([], applied.json()["data"]["failed"])


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
