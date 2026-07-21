"""M3: the versioned HTTP surface.

Gate M3: DTO/cursor/error contracts, read and write authorisation, idempotency,
version conflict, pagination, and no state-changing route outside /api/v1.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from orbit.web.api_v1 import (
    OPS_READ_SCOPE, OPS_WRITE_SCOPE, READ_SCOPE, SENSITIVE_SCOPE, WRITE_SCOPE,
    Authorizer,
)
from orbit.web.app import HandlerRegistration, create_app
from orbit.workflow.api.dto import (
    CursorError, decode_cursor, encode_cursor, envelope, page_size,
)
from orbit.workflow.application.budget_service import BudgetService
from orbit.workflow.artifacts.local_cas import LocalCASBackend
from orbit.workflow.application.human_service import HumanTaskService
from orbit.workflow.api.workflow_catalog import WorkflowCatalogReadModelService
from orbit.workflow.domain.human import HumanTaskKind
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.catalogs.handlers import HandlerManifest
from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.handlers import ResourceProfile
from orbit.workflow.handlers import TransformHandler
from orbit.workflow.persistence.database import connect_workflow_database
from tests.test_web_composition import (
    AsgiHarness, SCHEMAS, publish_human_workflow, publish_linear_workflow,
    transform_registration,
)
from tests.test_ui_contract_goldens import validator as ui_contract_validator


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


class WorkflowCatalogProjectionTests(unittest.TestCase):
    def test_agent_object_prompt_advertises_goal_binding(self) -> None:
        ir = {
            "entry": ["analyze"],
            "nodes": [{
                "id": "analyze", "kind": "action",
                "handler": {"name": "agent.claude", "version": "1.0.0"},
            }],
        }
        inputs = [{
            "id": "prompt", "schema": {"type": "object"},
            "transport": "inline",
        }]

        binding = WorkflowCatalogReadModelService._goal_binding(ir, inputs)

        self.assertEqual("run.goal", binding["source"])
        self.assertEqual("analyze", binding["node_id"])
        self.assertEqual("prompt", binding["input_id"])
        self.assertEqual("goal", binding["property"])

    def test_non_agent_input_does_not_advertise_goal_binding(self) -> None:
        ir = {
            "entry": ["transform"],
            "nodes": [{
                "id": "transform", "kind": "action",
                "handler": {"name": "transform", "version": "1.0.0"},
            }],
        }
        inputs = [{
            "id": "prompt", "schema": {"type": "object"},
            "transport": "inline",
        }]
        self.assertIsNone(WorkflowCatalogReadModelService._goal_binding(ir, inputs))


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
        self.artifact_backend = LocalCASBackend(Path(self.temp.name) / "artifacts")
        self.scopes = {
            "reader": [READ_SCOPE],
            "writer": [READ_SCOPE, WRITE_SCOPE, OPS_READ_SCOPE, OPS_WRITE_SCOPE],
            "second-writer": [READ_SCOPE, WRITE_SCOPE, OPS_READ_SCOPE, OPS_WRITE_SCOPE],
            "ops-reader": [READ_SCOPE, OPS_READ_SCOPE],
            "sensitive": [READ_SCOPE, SENSITIVE_SCOPE],
            "other-sensitive": [READ_SCOPE, SENSITIVE_SCOPE],
            "nobody": [],
        }
        self.app = create_app(
            self.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            worker_count=1, poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: self.scopes.get(actor, [])),
            artifact_backend=self.artifact_backend,
            single_goal_mode=False,
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

    def test_single_goal_mode_preserves_replay_and_rejects_a_second_goal(self) -> None:
        publish_human_workflow(self.db)
        app = create_app(
            self.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            worker_count=1, poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: self.scopes.get(actor, [])),
            artifact_backend=self.artifact_backend,
            single_goal_mode=True,
        )
        with AsgiHarness(app) as client:
            first = client.post(
                "/api/v1/runs", actor="writer", key="single-first",
                body={"workflow_id": "workflow:human", "goal": "First goal", "input": {"value": 0}},
            )
            self.assertEqual(200, first.status_code, first.text)
            run_id = first.json()["data"]["run_id"]

            replay = client.post(
                "/api/v1/runs", actor="writer", key="single-first",
                body={"workflow_id": "workflow:human", "goal": "First goal", "input": {"value": 0}},
            )
            self.assertEqual(200, replay.status_code, replay.text)
            self.assertEqual(run_id, replay.json()["data"]["run_id"])

            conflict = client.post(
                "/api/v1/runs", actor="writer", key="single-second",
                body={"workflow_id": "workflow:linear", "goal": "Second goal", "input": {"value": 0}},
            )
            self.assertEqual(409, conflict.status_code, conflict.text)
            payload = conflict.json()["error"]
            self.assertEqual("active_goal_exists", payload["code"])
            self.assertEqual(run_id, payload["details"]["active_goal"]["run_id"])

            dashboard = client.get("/api/v1/dashboard", actor="writer").json()["data"]
            self.assertEqual(run_id, dashboard["active_goal"]["run_id"])
            cancel = dashboard["active_goal"]["allowed_commands"][0]
            self.assertEqual("run.cancel", cancel["command"])
            self.assertEqual(run_id, cancel["target_aggregate_id"])

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


class RunDiscoveryTests(ApiTestCase):
    def _start(self, client, key: str, goal: str) -> str:
        response = client.post(
            "/api/v1/runs", actor="writer", key=key,
            body={
                "workflow_id": "workflow:linear",
                "input": {"value": 0},
                "goal": goal,
            },
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()["data"]["run_id"]

    def test_goal_is_projected_into_summary_and_server_search(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id = self._start(
                client, "goal-search", "Market expansion brief\nEvidence backed",
            )
            summary = client.get(f"/api/v1/runs/{run_id}", actor="writer").json()["data"]
            self.assertEqual("Market expansion brief", summary["display_name"])
            self.assertEqual(
                "Market expansion brief\nEvidence backed", summary["goal"]
            )

            result = client.get(
                "/api/v1/runs?q=MARKET%20EXPANSION", actor="writer"
            ).json()["data"]["runs"]
            self.assertEqual([run_id], [item["run_id"] for item in result])
            errors = sorted(
                ui_contract_validator("run-summary.schema.json").iter_errors(result[0]),
                key=str,
            )
            self.assertEqual([], errors, f"endpoint drifted from RunSummary 2.0: {errors}")

    def test_actor_action_is_authorised_and_sorted_before_recency(self) -> None:
        with AsgiHarness(self.app) as client:
            actionable = self._start(client, "actionable", "Needs approval")
            HumanTaskService(self.db).create(
                EntityId.parse(actionable), HumanTaskKind.APPROVAL,
                {"question": "ship?"}, actor="writer",
                now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                participants=["writer"],
            )
            newer = self._start(client, "newer", "Newer ordinary run")

            writer_runs = client.get("/api/v1/runs", actor="writer").json()["data"]["runs"]
            self.assertEqual(actionable, writer_runs[0]["run_id"])
            self.assertTrue(writer_runs[0]["requires_actor_action"])

            reader_runs = client.get("/api/v1/runs", actor="reader").json()["data"]["runs"]
            self.assertTrue(all(not item["requires_actor_action"] for item in reader_runs))
            self.assertIn(newer, {item["run_id"] for item in reader_runs})

            human = client.get(
                "/api/v1/runs?responsibility=human", actor="writer"
            ).json()["data"]["runs"]
            self.assertEqual([actionable], [item["run_id"] for item in human])

    def test_cursor_is_bound_to_the_query_and_unknown_params_are_rejected(self) -> None:
        with AsgiHarness(self.app) as client:
            self._start(client, "market-one", "Market one")
            self._start(client, "market-two", "Market two")
            first = client.get(
                "/api/v1/runs?q=market&limit=1", actor="writer"
            ).json()
            self.assertIsNotNone(first["next_cursor"])
            mismatch = client.get(
                f"/api/v1/runs?q=other&limit=1&cursor={first['next_cursor']}",
                actor="writer",
            )
            self.assertEqual(400, mismatch.status_code)
            self.assertEqual("invalid_request", mismatch.json()["error"]["code"])
            unknown = client.get("/api/v1/runs?sort=client", actor="writer")
            self.assertEqual(400, unknown.status_code)

    def test_dashboard_counts_and_attention_are_actor_aware(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id = self._start(client, "dashboard", "Dashboard goal")
            HumanTaskService(self.db).create(
                EntityId.parse(run_id), HumanTaskKind.APPROVAL,
                {"question": "ship?"}, actor="writer",
                now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                participants=["writer"],
            )
            writer = client.get("/api/v1/dashboard", actor="writer").json()["data"]
            reader = client.get("/api/v1/dashboard", actor="reader").json()["data"]
            self.assertEqual(1, writer["counts"]["total"])
            self.assertEqual(1, writer["attention_count"])
            self.assertEqual(0, reader["attention_count"])
            self.assertEqual(run_id, writer["recent_runs"][0]["run_id"])


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

    def test_historical_overlay_and_dynamic_views_are_real_routes(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id = self._run(client)
            with connect_workflow_database(self.db) as db:
                head = db.execute(
                    "SELECT MAX(global_position) FROM run_events WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            historical = client.get(
                f"/api/v1/runs/{run_id}/plan/overlay?as_of_global_position={head}",
                actor="reader",
            )
            self.assertEqual(200, historical.status_code, historical.text)
            self.assertEqual(head, historical.json()["data"]["as_of_global_position"])
            # The in-process worker may append events between reading the head
            # and making this request. A one-position lead therefore races the
            # worker and intermittently becomes a valid historical cursor.
            future_position = head + 1_000_000
            future = client.get(
                f"/api/v1/runs/{run_id}/plan/overlay?as_of_global_position={future_position}",
                actor="reader",
            )
            self.assertEqual(400, future.status_code)
            for suffix in ("planner-decisions", "foreach", "subflows"):
                response = client.get(f"/api/v1/runs/{run_id}/{suffix}", actor="reader")
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual([], response.json()["data"]["items"])
            self.assertEqual(
                403,
                client.get(
                    f"/api/v1/runs/{run_id}/foreach/foreach_group:missing/items",
                    actor="reader",
                ).status_code,
            )


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


class ArtifactApiTests(ApiTestCase):
    def _artifact(self, client, *, content=b"hello artifact", subject="sensitive"):
        from orbit.workflow.persistence.database import connect_workflow_database

        run_id = client.post(
            "/api/v1/runs", actor="writer", key=f"artifact-{len(content)}",
            body={"workflow_id": "workflow:linear", "input": {"value": 7}},
        ).json()["data"]["run_id"]
        receipt = self.artifact_backend.write(content, max_size_bytes=len(content))
        artifact_id = f"artifact:{receipt.checksum.value.removeprefix('sha256:')}"
        with connect_workflow_database(self.db) as connection:
            event_id = connection.execute(
                "SELECT event_id FROM run_events WHERE run_id=? ORDER BY global_position LIMIT 1",
                (run_id,),
            ).fetchone()[0]
            now = "2026-01-01T00:00:00+00:00"
            connection.execute(
                "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact_id, run_id, "workflow:linear", "attempt", "attempt:test",
                    "node_run:test", "report", "schema:text", "text/plain",
                    receipt.checksum.value, receipt.size_bytes, receipt.blob_key,
                    "run", run_id, "committed", now, now, event_id,
                ),
            )
            connection.execute(
                "INSERT INTO artifact_acl VALUES (?,?,'read','writer',?)",
                (artifact_id, subject, now),
            )
            for kind, target in (("producer", "attempt:test"), ("consumer", "node_run:next")):
                connection.execute(
                    "INSERT INTO artifact_links VALUES (?,?,?,?,?,?,?,?)",
                    (
                        f"artifact_link:{kind}-{len(content)}", "workflow:linear", run_id,
                        artifact_id, kind, target, event_id, now,
                    ),
                )
            connection.commit()
        return run_id, artifact_id, receipt.blob_key

    def test_list_detail_and_lineage_use_the_same_acl(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id, artifact_id, _blob = self._artifact(client)
            visible = client.get("/api/v1/artifacts", actor="sensitive")
            self.assertEqual(200, visible.status_code, visible.text)
            self.assertEqual([artifact_id], [
                item["artifact_id"] for item in visible.json()["data"]["artifacts"]
            ])
            filtered = client.get(
                f"/api/v1/artifacts?run_id={run_id}&content_type=text/plain",
                actor="sensitive",
            )
            self.assertEqual(1, len(filtered.json()["data"]["artifacts"]))
            detail = client.get(f"/api/v1/artifacts/{artifact_id}", actor="sensitive")
            self.assertEqual(200, detail.status_code, detail.text)
            self.assertNotIn("blob_key", detail.json()["data"])
            lineage = client.get(
                f"/api/v1/artifacts/{artifact_id}/lineage", actor="sensitive"
            ).json()["data"]
            self.assertEqual(1, len(lineage["producers"]))
            self.assertEqual(1, len(lineage["consumers"]))

    def test_unauthorized_and_missing_ids_are_indistinguishable(self) -> None:
        from orbit.workflow.persistence.database import connect_workflow_database

        with AsgiHarness(self.app) as client:
            _run_id, artifact_id, _blob = self._artifact(client)
            denied = client.get(f"/api/v1/artifacts/{artifact_id}", actor="reader")
            missing = client.get(
                f"/api/v1/artifacts/artifact:{'f' * 64}", actor="reader"
            )
            self.assertEqual(404, denied.status_code)
            self.assertEqual(denied.json(), missing.json())
            self.assertEqual([], client.get(
                "/api/v1/artifacts", actor="reader"
            ).json()["data"]["artifacts"])
            with connect_workflow_database(self.db, read_only=True) as connection:
                audits = connection.execute(
                    "SELECT action,target_id,decision FROM audit_records"
                    " WHERE actor='reader' ORDER BY occurred_at,audit_id"
                ).fetchall()
            denied_audits = [row for row in audits if row["decision"] == "denied"]
            self.assertGreaterEqual(len(denied_audits), 2)
            self.assertTrue(all(
                row["target_id"].startswith("artifact_ref_hash:")
                for row in denied_audits
            ))

    def test_preview_is_explicit_and_blob_missing_is_visible_only_after_acl(self) -> None:
        with AsgiHarness(self.app) as client:
            _run_id, artifact_id, blob_key = self._artifact(client)
            preview = client.get(
                f"/api/v1/artifacts/{artifact_id}/content", actor="sensitive"
            )
            self.assertEqual(200, preview.status_code, preview.text)
            self.assertEqual("hello artifact", preview.text)
            self.artifact_backend.delete(blob_key)
            missing_blob = client.get(
                f"/api/v1/artifacts/{artifact_id}/content", actor="sensitive"
            )
            self.assertEqual(410, missing_blob.status_code)
            self.assertEqual("blob_missing", missing_blob.json()["error"]["code"])
            denied = client.get(
                f"/api/v1/artifacts/{artifact_id}/content", actor="other-sensitive"
            )
            public_missing = client.get(
                f"/api/v1/artifacts/artifact:{'f' * 64}/content",
                actor="other-sensitive",
            )
            self.assertEqual(404, denied.status_code)
            self.assertEqual(denied.json(), public_missing.json())

    def test_large_text_is_not_loaded_as_a_preview(self) -> None:
        with AsgiHarness(self.app) as client:
            _run_id, artifact_id, _blob = self._artifact(client, content=b"x" * 70000)
            response = client.get(
                f"/api/v1/artifacts/{artifact_id}/content", actor="sensitive"
            )
            self.assertEqual(413, response.status_code)
            self.assertEqual("preview_too_large", response.json()["error"]["code"])
            self.artifact_backend.read = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("streaming download must not call read()")
            )
            download = client.get(
                f"/api/v1/artifacts/{artifact_id}/content?download=true",
                actor="sensitive",
            )
            self.assertEqual(200, download.status_code)
            self.assertEqual(70000, len(download.text))
            self.assertEqual("70000", download.headers["content-length"])
            self.assertEqual("nosniff", download.headers["x-content-type-options"])


class CatalogTests(ApiTestCase):
    def test_handler_catalog_exposes_identity_not_commands(self) -> None:
        with AsgiHarness(self.app) as client:
            response = client.get("/api/v1/handler-catalog", actor="reader")
            self.assertEqual(200, response.status_code, response.text)
            handlers = response.json()["data"]["handlers"]
            self.assertEqual(1, len(handlers))
            entry = handlers[0]
            self.assertEqual("transform", entry["name"])
            self.assertEqual("registered", entry["registration_status"])
            self.assertIsNone(entry["recent_attempt"])
            self.assertEqual(
                "registration_only", response.json()["data"]["status_semantics"]
            )
            self.assertIn("manifest_fingerprint", entry)
            self.assertEqual(
                {"value": "example://integer/1.0"}, entry["inputs"]
            )
            self.assertEqual(
                {"value": "example://integer/1.0"}, entry["outputs"]
            )
            self.assertEqual({"type": "object"}, entry["config_schema"])
            # Nothing here may be pasteable into a shell.
            serialised = repr(entry)
            for forbidden in ("command", "argv", "path", "secret_value"):
                self.assertNotIn(forbidden, serialised.lower())

    def test_handler_catalog_serializes_nested_config_schema(self) -> None:
        registration = HandlerRegistration(
            HandlerManifest(
                "nested", "1.0.0", ("action",),
                {"value": "example://integer/1.0"},
                {"value": "example://integer/1.0"},
                {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "choices": {"type": "array", "items": {"type": "string"}},
                    },
                },
                ExecutionSafety.REPLAY_SAFE,
                ResourceProfile(100, 100, 5, 60, 1_000_000, "test"),
                "schema://object/1.0",
            ),
            TransformHandler(),
            "nested@1.0.0",
        )
        app = create_app(
            Path(self.temp.name) / "nested.db",
            handlers=[registration], schemas=SCHEMAS,
            worker_count=1, poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: self.scopes.get(actor, [])),
            single_goal_mode=False,
        )

        with AsgiHarness(app) as client:
            response = client.get("/api/v1/handler-catalog", actor="reader")

        self.assertEqual(200, response.status_code, response.text)
        schema = response.json()["data"]["handlers"][0]["config_schema"]
        self.assertEqual("string", schema["properties"]["prompt"]["type"])
        self.assertEqual(
            {"type": "string"}, schema["properties"]["choices"]["items"]
        )

    def test_workflow_catalog_advertises_start_only_to_writers(self) -> None:
        with AsgiHarness(self.app) as client:
            reader = client.get("/api/v1/workflows", actor="reader")
            self.assertEqual(200, reader.status_code, reader.text)
            self.assertEqual([], reader.json()["data"]["workflows"][0]["allowed_commands"])

            writer = client.get("/api/v1/workflows", actor="writer")
            entry = writer.json()["data"]["workflows"][0]
            self.assertEqual("Linear", entry["name"])
            self.assertEqual("structured", entry["input_mode"])
            self.assertIsNone(entry["goal_binding"])
            self.assertEqual("value", entry["inputs"][0]["id"])
            self.assertEqual("integer", entry["inputs"][0]["schema"]["type"])
            self.assertEqual(4, entry["summary"]["node_count"])
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

    def test_workflow_definition_read_is_versioned_and_actor_shaped(self) -> None:
        with AsgiHarness(self.app) as client:
            reader = client.get(
                "/api/v1/workflows/workflow:linear?version=1", actor="reader"
            )
            self.assertEqual(200, reader.status_code, reader.text)
            detail = reader.json()["data"]
            self.assertEqual("workflow:linear", detail["workflow_id"])
            self.assertEqual(1, detail["latest_version"])
            self.assertEqual("workflow:linear", detail["definition"]["workflow_id"])
            self.assertEqual([], detail["allowed_commands"])

            missing = client.get(
                "/api/v1/workflows/workflow:linear?version=99", actor="writer"
            )
            self.assertEqual(404, missing.status_code)
            self.assertEqual("not_found", missing.json()["error"]["code"])


class WorkflowAuthoringApiTests(ApiTestCase):
    """Prompt → draft → publish, all through advertised commands."""

    GENERATED = {
        "dsl_version": "1.2",
        "metadata": {"id": "prompted", "name": "Prompted"},
        "nodes": [
            {
                "id": "work", "kind": "action",
                "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
                "outputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
                "handler": {"name": "transform", "version": "1.0.0"},
            },
            {
                "id": "done", "kind": "terminal",
                "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
            },
        ],
        "edges": [{
            "id": "flow", "from": {"node": "work", "port": "value"},
            "to": {"node": "done", "port": "value"},
        }],
        "entry": ["work"], "terminals": ["done"],
    }

    def app_with_generator(self, responses):
        import json as json_module

        queue = list(responses)
        return create_app(
            self.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            worker_count=1, poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: self.scopes.get(actor, [])),
            workflow_generator=lambda prompt: queue.pop(0),
        )

    def test_generate_then_publish_through_the_advertised_command(self) -> None:
        import json as json_module

        app = self.app_with_generator([json_module.dumps(self.GENERATED)])
        with AsgiHarness(app) as client:
            catalog = client.get("/api/v1/workflows", actor="writer").json()["data"]
            generate = next(
                c for c in catalog["allowed_commands"]
                if c["command"] == "workflow.generate"
            )

            drafted = client.post(
                generate["href"], actor="writer", key="gen-1",
                body={"instruction": "one transform then done"},
            )
            self.assertEqual(200, drafted.status_code, drafted.text)
            draft = drafted.json()["data"]
            self.assertEqual("workflow:prompted", draft["workflow_id"])
            self.assertEqual(2, draft["node_count"])
            publish = draft["allowed_commands"][0]
            self.assertEqual("workflow.publish", publish["command"])
            self.assertEqual(0, publish["expected_version"])
            validate = next(
                item for item in draft["allowed_commands"]
                if item["command"] == "workflow.validate"
            )

            edited = json_module.loads(draft["source"])
            edited["metadata"]["name"] = "Edited before publish"
            validated = client.post(
                validate["href"], actor="writer", key="validate-1",
                body={
                    "source": json_module.dumps(edited),
                    "expected_version": validate["expected_version"],
                },
            )
            self.assertEqual(200, validated.status_code, validated.text)
            validated_draft = validated.json()["data"]
            self.assertNotEqual(
                draft["definition_hash"], validated_draft["definition_hash"],
            )
            publish = next(
                item for item in validated_draft["allowed_commands"]
                if item["command"] == "workflow.publish"
            )

            published = client.post(
                publish["href"], actor="writer", key="pub-1",
                body={
                    "source": validated_draft["source"],
                    "expected_version": publish["expected_version"],
                },
            )
            self.assertEqual(200, published.status_code, published.text)
            self.assertEqual(1, published.json()["data"]["version"])

            # The published workflow immediately appears in the catalog with a
            # start command — the wizard can run it with no further plumbing.
            entries = client.get(
                "/api/v1/workflows", actor="writer"
            ).json()["data"]["workflows"]
            entry = next(
                item for item in entries
                if item["workflow_id"] == "workflow:prompted"
            )
            self.assertEqual(
                "run.start", entry["allowed_commands"][0]["command"]
            )

    def test_generation_accepts_an_allowlisted_default_handler(self) -> None:
        import json as json_module

        prompts = []

        def generate(prompt):
            prompts.append(prompt)
            return json_module.dumps(self.GENERATED)

        app = create_app(
            self.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            worker_count=1, poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: self.scopes.get(actor, [])),
            workflow_generator=generate,
        )
        with AsgiHarness(app) as client:
            response = client.post(
                "/api/v1/workflows/generate", actor="writer", key="gen-default",
                body={"instruction": "flow", "default_agent": "transform"},
            )
        self.assertEqual(200, response.status_code, response.text)
        self.assertIn('"preferred_handler":"transform"', prompts[0])

    def test_generation_failure_returns_diagnostics_not_a_500(self) -> None:
        app = self.app_with_generator(["nonsense"] * 3)
        with AsgiHarness(app) as client:
            response = client.post(
                "/api/v1/workflows/generate", actor="writer", key="gen-bad",
                body={"instruction": "??"},
            )
            self.assertEqual(409, response.status_code, response.text)
            self.assertIn("GENERATION_PROTOCOL", response.json()["error"]["message"])

    def test_generate_is_absent_without_a_generator(self) -> None:
        with AsgiHarness(self.app) as client:
            catalog = client.get("/api/v1/workflows", actor="writer").json()["data"]
            self.assertEqual([], catalog["allowed_commands"])
            response = client.post(
                "/api/v1/workflows/generate", actor="writer", key="gen-off",
                body={"instruction": "flow"},
            )
            self.assertEqual(503, response.status_code)
            caps = client.get(
                "/api/v1/capabilities", actor="writer"
            ).json()["data"]["capabilities"]
            self.assertFalse(caps["workflow_generation"]["available"])

    def test_publish_rejects_a_source_that_names_a_different_workflow(self) -> None:
        import json as json_module

        app = self.app_with_generator([json_module.dumps(self.GENERATED)])
        with AsgiHarness(app) as client:
            drafted = client.post(
                "/api/v1/workflows/generate", actor="writer", key="gen-2",
                body={"instruction": "flow"},
            ).json()["data"]
            response = client.post(
                "/api/v1/workflows/workflow:someone-else/versions",
                actor="writer", key="pub-2",
                body={"source": drafted["source"], "expected_version": 0},
            )
            self.assertEqual(409, response.status_code)
            self.assertIn("route names", response.json()["error"]["message"])
            # Nothing was persisted by the refused publish.
            entries = client.get(
                "/api/v1/workflows", actor="reader"
            ).json()["data"]["workflows"]
            self.assertNotIn(
                "workflow:prompted", [item["workflow_id"] for item in entries]
            )

    def test_publish_conflict_and_reader_denial(self) -> None:
        import json as json_module

        app = self.app_with_generator([json_module.dumps(self.GENERATED)])
        with AsgiHarness(app) as client:
            drafted = client.post(
                "/api/v1/workflows/generate", actor="writer", key="gen-3",
                body={"instruction": "flow"},
            ).json()["data"]
            stale = client.post(
                "/api/v1/workflows/workflow:prompted/versions",
                actor="writer", key="pub-3",
                body={"source": drafted["source"], "expected_version": 7},
            )
            self.assertEqual(409, stale.status_code)
            denied = client.post(
                "/api/v1/workflows/workflow:prompted/versions",
                actor="reader", key="pub-4",
                body={"source": drafted["source"], "expected_version": 0},
            )
            self.assertEqual(403, denied.status_code)


class WorkflowDraftApiTests(ApiTestCase):
    """Agent instruction → compiled revision → publish."""

    def setUp(self) -> None:
        super().setUp()
        # A workflow published from real DSL, source stored — the production
        # path. The hand-built linear IR has no source and stays as the
        # degrade case.
        import json as json_module

        from orbit.workflow.application.workflows import (
            WorkflowCatalogs, WorkflowDefinitionService,
        )
        from orbit.workflow.catalogs import (
            InMemoryHandlerCatalog, InMemorySchemaCatalog,
        )
        from orbit.workflow.catalogs.extensions import InMemoryExtensionRegistry
        from orbit.workflow.persistence.workflow_versions import (
            SQLiteWorkflowVersionStore,
        )
        from tests.test_workflow_drafts import dsl as editable_dsl

        catalogs = WorkflowCatalogs(
            InMemoryHandlerCatalog([transform_registration().manifest]),
            InMemorySchemaCatalog(SCHEMAS),
            InMemoryExtensionRegistry(),
        )
        WorkflowDefinitionService(
            catalogs, SQLiteWorkflowVersionStore(self.db)
        ).publish_workflow(
            json_module.dumps(editable_dsl()), source_name="<fixture>",
            source_format="json", expected_latest_version=0, actor="fixture",
        )
        self.app = self._app_with_reviser(
            lambda _prompt: json_module.dumps(editable_dsl(name="Linear, edited"))
        )

    def _settle(self, client, draft_id, *, timeout=10.0):
        """Wait for the background revision worker to settle the job.

        The prompt is a durable job now, so the API returns while the Agent is
        still running; a test that wants the outcome waits for it exactly as
        the editor's poll does.
        """
        import time as _time

        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            data = client.get(
                f"/api/v1/workflow-drafts/{draft_id}", actor="writer",
            ).json()["data"]
            revision = data["pending_revision"]
            if revision is None or not revision["in_flight"]:
                return data
            _time.sleep(0.02)
        raise AssertionError("the revision job never settled")

    def _edit_command(self, client):
        detail = client.get(
            "/api/v1/workflows/workflow:draftable", actor="writer"
        ).json()["data"]
        return next(
            c for c in detail["allowed_commands"]
            if c["command"] == "workflow.draft.create"
        ), detail

    def _app_with_reviser(self, generate):
        return create_app(
            self.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            worker_count=1, poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: self.scopes.get(actor, [])),
            workflow_generator=generate,
            single_goal_mode=False,
        )

    def _app_without_reviser(self):
        return create_app(
            self.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            worker_count=1, poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: self.scopes.get(actor, [])),
            single_goal_mode=False,
        )

    def test_detail_advertises_editing_only_to_writers_with_source(self) -> None:
        with AsgiHarness(self.app) as client:
            command, detail = self._edit_command(client)
            self.assertTrue(detail["source_available"])
            self.assertEqual(detail["latest_version"], command["expected_version"])
            reader = client.get(
                "/api/v1/workflows/workflow:draftable", actor="reader"
            ).json()["data"]
            self.assertEqual([], [
                c for c in reader["allowed_commands"]
                if c["command"] == "workflow.draft.create"
            ])
            # The hand-built linear IR was published without source: viewable,
            # runnable, and honestly not editable.
            legacy = client.get(
                "/api/v1/workflows/workflow:linear", actor="writer"
            ).json()["data"]
            self.assertFalse(legacy["source_available"])
            self.assertEqual([], [
                c for c in legacy["allowed_commands"]
                if c["command"] == "workflow.draft.create"
            ])

    def test_detail_does_not_offer_editing_without_an_agent_reviser(self) -> None:
        with AsgiHarness(self._app_without_reviser()) as client:
            detail = client.get(
                "/api/v1/workflows/workflow:draftable", actor="writer"
            ).json()["data"]
            self.assertEqual([], [
                command for command in detail["allowed_commands"]
                if command["command"] == "workflow.draft.create"
            ])
            direct = client.post(
                "/api/v1/workflows/workflow:draftable/drafts",
                actor="writer", key="no-reviser", body={"expected_version": 1},
            )
            self.assertEqual(503, direct.status_code)
            self.assertEqual(
                "generation_unavailable", direct.json()["error"]["code"]
            )

    def test_full_edit_loop_publishes_the_next_version(self) -> None:
        with AsgiHarness(self.app) as client:
            command, detail = self._edit_command(client)
            draft = client.post(
                command["href"], actor="writer", key="d1", body={},
            ).json()["data"]
            self.assertEqual("active", draft["status"])
            self.assertEqual("dirty", draft["validation_status"])
            commands = {c["command"] for c in draft["allowed_commands"]}
            self.assertEqual({
                "workflow.draft.revise", "workflow.draft.discard",
            }, commands)
            self.assertNotIn("workflow.draft.publish", commands)

            revise = next(
                c for c in draft["allowed_commands"]
                if c["command"] == "workflow.draft.revise"
            )
            staged = client.post(
                revise["href"], actor="writer", key="d2",
                body={
                    "instruction": "rename the workflow",
                    "expected_version": revise["expected_version"],
                },
            ).json()["data"]
            self.assertEqual("dirty", staged["validation_status"])
            self.assertEqual("queued", staged["pending_revision"]["status"])
            staged = self._settle(client, draft["draft_id"])
            self.assertEqual("pending", staged["pending_revision"]["status"])
            self.assertIn("Linear, edited", staged["pending_revision"]["source"])
            accept = next(
                command for command in staged["allowed_commands"]
                if command["command"] == "workflow.draft.accept"
            )
            validated = client.post(
                accept["href"], actor="writer", key="d3",
                body={"expected_version": accept["expected_version"]},
            ).json()["data"]
            self.assertEqual("valid", validated["validation_status"])

            publish = next(
                c for c in validated["allowed_commands"]
                if c["command"] == "workflow.draft.publish"
            )
            published = client.post(
                publish["href"], actor="writer", key="d4",
                body={"expected_version": publish["expected_version"]},
            )
            self.assertEqual(200, published.status_code, published.text)
            data = published.json()["data"]
            self.assertEqual("published", data["status"])
            self.assertEqual(2, data["published"]["version"])

            refreshed = client.get(
                "/api/v1/workflows/workflow:draftable", actor="writer"
            ).json()["data"]
            self.assertEqual(2, refreshed["latest_version"])
            self.assertEqual("Linear, edited", refreshed["name"])
            historical = client.get(
                "/api/v1/workflows/workflow:draftable?version=1", actor="writer"
            ).json()["data"]
            self.assertEqual(1, historical["selected_version"])
            self.assertEqual(2, historical["latest_version"])
            self.assertEqual([2, 1], [item["version"] for item in historical["versions"]])
            create = next(
                item for item in historical["allowed_commands"]
                if item["command"] == "workflow.draft.create"
            )
            self.assertEqual(1, create["expected_version"])

    def test_invalid_source_reports_diagnostics_and_blocks_publish(self) -> None:
        with AsgiHarness(self._app_with_reviser(lambda _prompt: "{}")) as client:
            command, _ = self._edit_command(client)
            draft = client.post(
                command["href"], actor="writer", key="d5", body={},
            ).json()["data"]
            queued = client.post(
                f"/api/v1/workflow-drafts/{draft['draft_id']}/revise",
                actor="writer", key="d6",
                body={
                    "instruction": "make it invalid",
                    "expected_version": draft["revision"],
                },
            )
            # Enqueued, not judged: the verdict arrives on the job.
            self.assertEqual(200, queued.status_code, queued.text)
            self.assertEqual(
                "queued", queued.json()["data"]["pending_revision"]["status"]
            )
            settled = self._settle(client, draft["draft_id"])
            self.assertIsNone(settled["pending_revision"])
            failure = settled["revision_history"][0]
            self.assertEqual("failed", failure["status"])
            self.assertTrue(failure["error_code"])
            denied = client.post(
                f"/api/v1/workflow-drafts/{draft['draft_id']}/publish",
                actor="writer", key="d7",
                body={"expected_version": settled["revision"]},
            )
            self.assertEqual(409, denied.status_code)
            self.assertEqual(
                "draft_not_validated", denied.json()["error"]["code"]
            )

    def test_stale_revision_is_a_typed_conflict(self) -> None:
        with AsgiHarness(self.app) as client:
            command, _ = self._edit_command(client)
            draft = client.post(
                command["href"], actor="writer", key="d9", body={},
            ).json()["data"]
            stale = client.post(
                f"/api/v1/workflow-drafts/{draft['draft_id']}/revise",
                actor="writer", key="d10",
                body={"instruction": "rename it", "expected_version": 99},
            )
            self.assertEqual(409, stale.status_code)
            self.assertEqual(
                "draft_version_conflict", stale.json()["error"]["code"]
            )

    def test_drafts_are_private_to_their_actor(self) -> None:
        with AsgiHarness(self.app) as client:
            command, _ = self._edit_command(client)
            draft = client.post(
                command["href"], actor="writer", key="d11", body={},
            ).json()["data"]
            other = client.get(
                f"/api/v1/workflow-drafts/{draft['draft_id']}",
                actor="second-writer",
            )
            self.assertEqual(404, other.status_code)

    def test_reviser_is_the_only_draft_mutation_command(self) -> None:
        import json as json_module
        from tests.test_workflow_drafts import dsl as editable_dsl

        revised = editable_dsl(name="Agent revised")
        app = self._app_with_reviser(lambda _prompt: json_module.dumps(revised))
        with AsgiHarness(app) as client:
            command, _ = self._edit_command(client)
            draft = client.post(
                command["href"], actor="writer", key="revise-create", body={},
            ).json()["data"]
            commands = {item["command"] for item in draft["allowed_commands"]}
            self.assertEqual({
                "workflow.draft.revise", "workflow.draft.discard",
            }, commands)
            revise = next(
                item for item in draft["allowed_commands"]
                if item["command"] == "workflow.draft.revise"
            )
            response = client.post(
                revise["href"], actor="writer", key="revise-success",
                body={
                    "instruction": "rename it",
                    "expected_version": revise["expected_version"],
                },
            )
            self.assertEqual(200, response.status_code, response.text)
            data = response.json()["data"]
            self.assertNotIn("Agent revised", data["source"])
            # While the job is in flight the only offer is to stop it.
            self.assertEqual({
                "workflow.draft.cancel-revision", "workflow.draft.discard",
            }, {item["command"] for item in data["allowed_commands"]})

            data = self._settle(client, draft["draft_id"])
            self.assertNotIn("Agent revised", data["source"])
            self.assertIn("Agent revised", data["pending_revision"]["source"])
            self.assertEqual({
                "workflow.draft.accept", "workflow.draft.reject",
                "workflow.draft.discard",
            }, {item["command"] for item in data["allowed_commands"]})

    def test_manual_draft_mutation_routes_are_not_exposed(self) -> None:
        with AsgiHarness(self.app) as client:
            command, _ = self._edit_command(client)
            draft = client.post(
                command["href"], actor="writer", key="manual-create", body={},
            ).json()["data"]
            for action in ("save", "validate"):
                response = client.post(
                    f"/api/v1/workflow-drafts/{draft['draft_id']}/{action}",
                    actor="writer", key=f"manual-{action}",
                    body={"source": "{}", "expected_version": draft["revision"]},
                )
                self.assertEqual(404, response.status_code)

    def test_failed_revision_does_not_leave_a_pending_receipt(self) -> None:
        app = self._app_with_reviser(lambda _prompt: "{}")
        with AsgiHarness(app) as client:
            command, _ = self._edit_command(client)
            draft = client.post(
                command["href"], actor="writer", key="revise-fail-create", body={},
            ).json()["data"]
            revise = next(
                item for item in draft["allowed_commands"]
                if item["command"] == "workflow.draft.revise"
            )
            body = {
                "instruction": "make an invalid change",
                "expected_version": revise["expected_version"],
            }
            first = client.post(
                revise["href"], actor="writer", key="revise-fail", body=body,
            )
            second = client.post(
                revise["href"], actor="writer", key="revise-fail", body=body,
            )
            # Redelivery of the same intent enqueues one job, never two model
            # calls — the idempotency key is what makes a retried click safe.
            self.assertEqual(200, first.status_code, first.text)
            self.assertEqual(200, second.status_code, second.text)
            self.assertEqual(
                first.json()["data"]["pending_revision"]["revision_id"],
                second.json()["data"]["pending_revision"]["revision_id"],
            )

            settled = self._settle(client, draft["draft_id"])
            # A failed job leaves no candidate to accept, and says why.
            self.assertIsNone(settled["pending_revision"])
            failure = settled["revision_history"][0]
            self.assertEqual("failed", failure["status"])
            self.assertTrue(failure["error_code"])
            self.assertEqual(
                {"workflow.draft.revise", "workflow.draft.discard"},
                {item["command"] for item in settled["allowed_commands"]},
            )

    def test_an_in_flight_revision_can_be_cancelled(self) -> None:
        import threading

        release = threading.Event()

        def slow(_prompt):
            # Hold the Agent inside the worker so the job is observably
            # running while the operator cancels it.
            release.wait(timeout=10)
            raise AssertionError("cancelled jobs must not be settled as candidates")

        with AsgiHarness(self._app_with_reviser(slow)) as client:
            command, _ = self._edit_command(client)
            draft = client.post(
                command["href"], actor="writer", key="cancel-create", body={},
            ).json()["data"]
            revise = next(
                item for item in draft["allowed_commands"]
                if item["command"] == "workflow.draft.revise"
            )
            queued = client.post(
                revise["href"], actor="writer", key="cancel-revise",
                body={
                    "instruction": "take your time",
                    "expected_version": revise["expected_version"],
                },
            ).json()["data"]
            revision_id = queued["pending_revision"]["revision_id"]
            cancel = next(
                item for item in queued["allowed_commands"]
                if item["command"] == "workflow.draft.cancel-revision"
            )
            cancelled = client.post(
                cancel["href"], actor="writer", key="cancel-do",
                body={
                    "revision_id": revision_id,
                    "expected_version": cancel["expected_version"],
                },
            )
            self.assertEqual(200, cancelled.status_code, cancelled.text)
            release.set()

    def test_a_queued_revision_survives_a_restart(self) -> None:
        import json as json_module

        from tests.test_workflow_drafts import dsl as editable_dsl

        revised = editable_dsl(name="Survived")
        app = self._app_with_reviser(lambda _prompt: json_module.dumps(revised))
        with AsgiHarness(app) as client:
            command, _ = self._edit_command(client)
            draft = client.post(
                command["href"], actor="writer", key="restart-create", body={},
            ).json()["data"]
            revise = next(
                item for item in draft["allowed_commands"]
                if item["command"] == "workflow.draft.revise"
            )
            client.post(
                revise["href"], actor="writer", key="restart-revise",
                body={
                    "instruction": "rename it",
                    "expected_version": revise["expected_version"],
                },
            )
            settled = self._settle(client, draft["draft_id"])

        # A second composition over the same file sees the settled job: the
        # record lives in the database, not in the request that started it.
        with AsgiHarness(self._app_with_reviser(lambda _p: "{}")) as client:
            reloaded = client.get(
                f"/api/v1/workflow-drafts/{draft['draft_id']}", actor="writer",
            ).json()["data"]
            self.assertEqual(
                settled["revision_history"][0]["revision_id"],
                reloaded["revision_history"][0]["revision_id"],
            )


class CapabilityTests(ApiTestCase):
    def test_capabilities_declare_absence_with_a_reason(self) -> None:
        """Plan API-7: the client never learns 'not provided' from a 404."""
        with AsgiHarness(self.app) as client:
            self.assertEqual(401, client.get("/api/v1/capabilities").status_code)
            response = client.get("/api/v1/capabilities", actor="reader")
            self.assertEqual(200, response.status_code, response.text)
            data = response.json()["data"]
            self.assertEqual("reader", data["actor"])
            self.assertFalse(data["permissions"]["start_run"])
            self.assertFalse(data["permissions"]["ops_read"])
            self.assertFalse(data["permissions"]["ops_write"])
            caps = data["capabilities"]
            self.assertTrue(caps["static_graph"]["available"])
            self.assertTrue(caps["human_tasks"]["available"])
            # This composition runs without discovery: absent features carry
            # their reason instead of silently missing keys.
            self.assertFalse(caps["planner"]["available"])
            self.assertEqual(
                "agent_discovery_disabled", caps["planner"]["reason"]
            )
            self.assertFalse(caps["dynamic_plan_patch"]["available"])
            self.assertFalse(caps["planner_dispatcher"]["available"])
            self.assertEqual(
                "agent_discovery_disabled", caps["planner_dispatcher"]["reason"]
            )
            self.assertTrue(caps["foreach"]["available"])
            self.assertTrue(caps["subflow"]["available"])
            self.assertTrue(caps["history_overlay"]["available"])
            writer = client.get("/api/v1/capabilities", actor="writer").json()["data"]
            self.assertTrue(writer["permissions"]["start_run"])
            self.assertTrue(writer["permissions"]["ops_read"])
            self.assertTrue(writer["permissions"]["ops_write"])


class OperationsReadTests(ApiTestCase):
    def test_ops_status_has_independent_acl_and_factual_sections(self) -> None:
        with AsgiHarness(self.app) as client:
            self.assertEqual(
                403, client.get("/api/v1/ops/status", actor="reader").status_code
            )
            response = client.get("/api/v1/ops/status", actor="ops-reader")
            self.assertEqual(200, response.status_code, response.text)
            data = response.json()["data"]
            self.assertEqual("ok", data["integrity"]["status"])
            self.assertIn("ready_jobs", data["capacity"])
            self.assertFalse(data["capacity"]["benchmark"]["available"])
            self.assertIn("jobs_by_status", data["durable"])
            self.assertEqual(1, data["server_config"]["worker_count"])

            # quick_check walks the whole file, so its verdict is cached: a
            # second read within the TTL reports the same checked_at rather
            # than paying for another full scan.
            again = client.get("/api/v1/ops/status", actor="ops-reader")
            self.assertEqual(
                data["integrity"]["checked_at"],
                again.json()["data"]["integrity"]["checked_at"],
            )

    def test_live_cursor_is_opaque_and_reports_changes(self) -> None:
        with AsgiHarness(self.app) as client:
            initial = client.get("/api/v1/live", actor="reader").json()["data"]
            self.assertFalse(initial["changed"])
            self.assertNotIn("event_position", initial["cursor"])
            started = client.post(
                "/api/v1/runs", actor="writer", key="live-cursor-run",
                body={"workflow_id": "workflow:linear", "input": {"value": 1}},
            )
            self.assertEqual(200, started.status_code, started.text)
            changed = client.get(
                f"/api/v1/live?cursor={initial['cursor']}", actor="reader"
            ).json()["data"]
            self.assertTrue(changed["changed"])


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
            _run_id, task_id, _token = self._run_with_task(client)
            items = client.get("/api/v1/inbox", actor="writer").json()["data"]["items"]
            human = next(item for item in items if item["kind"] == "human")
            commands = {command["command"] for command in human["allowed_commands"]}
            self.assertIn("human.token", commands)
            token = next(
                command for command in human["allowed_commands"]
                if command["command"] == "human.token"
            )
            self.assertEqual(task_id, token["target_aggregate_id"])

    def test_inbox_does_not_advertise_human_commands_to_an_unrelated_writer(self) -> None:
        with AsgiHarness(self.app) as client:
            self._run_with_task(client)
            body = client.get("/api/v1/inbox", actor="second-writer").json()["data"]
            human = next(item for item in body["items"] if item["kind"] == "human")
            self.assertFalse(human["requires_actor_action"])
            self.assertEqual([], human["allowed_commands"])
            self.assertIn("quorum", human)
            errors = list(ui_contract_validator("inbox-item.schema.json").iter_errors(human))
            self.assertEqual([], errors)

    def test_a_run_parked_on_a_person_can_still_be_cancelled(self) -> None:
        """Answering an approval and abandoning the run are different acts.

        Without cancel on a human responsibility, a run waiting for someone who
        will never answer has no exit at all.
        """

        with AsgiHarness(self.app) as client:
            run_id, _task_id, _token = self._run_with_task(client)
            self.app.state.runtime.stop()
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
            from orbit.workflow.persistence.database import connect_workflow_database

            with connect_workflow_database(self.db, read_only=True) as connection:
                command_version = connection.execute(
                    "SELECT COALESCE(MAX(aggregate_sequence), 0) FROM run_events"
                    " WHERE aggregate_id=?", (run_id,),
                ).fetchone()[0]
            self.assertEqual(command_version, cancel["expected_version"])

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

            inbox = client.get("/api/v1/inbox", actor="writer").json()["data"]
            budget_item = next(item for item in inbox["items"] if item["kind"] == "budget")
            self.assertEqual(run_id, budget_item["run_id"])
            self.assertTrue(budget_item["requires_actor_action"])
            self.assertEqual(inbox["action_count"], inbox["total_count"])
            self.assertEqual(
                [], list(ui_contract_validator("inbox-item.schema.json").iter_errors(budget_item))
            )

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
            self.assertEqual(
                403, client.get("/api/v1/recovery", actor="reader").status_code
            )
            scan = client.get("/api/v1/recovery", actor="ops-reader")
            self.assertEqual(200, scan.status_code, scan.text)
            self.assertIn("findings", scan.json()["data"])
            self.assertTrue(all(
                not item["allowed_commands"]
                for item in scan.json()["data"]["findings"]
            ))

            denied = self.apply(client, ["X:y:1"], actor="ops-reader", key="r1")
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
