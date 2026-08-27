"""M3: the versioned HTTP surface.

Gate M3: DTO/cursor/error contracts, read and write authorisation, idempotency,
version conflict, pagination, and no state-changing route outside /api/v1.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import inspect
import tempfile
import unittest

from orbit.web.api_v1 import (
    OPS_READ_SCOPE, OPS_WRITE_SCOPE, READ_SCOPE, SENSITIVE_SCOPE, WRITE_SCOPE,
    Authorizer,
)
from orbit.web.app import HandlerRegistration, create_app
from orbit.web.local_identity import LOCAL_ACTOR
from orbit.workflow.api.routes import RateLimiter
from orbit.workflow.api.dto import (
    CursorError, decode_cursor, encode_cursor, envelope, page_size,
)
from orbit.workflow.artifacts.local_cas import LocalCASBackend
from orbit.workflow.api.workflow_catalog import WorkflowCatalogReadModelService
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

    def test_goal_readiness_accepts_one_goal_and_one_legacy_result(self) -> None:
        ir = {
            "entry": ["analyze"],
            "terminals": ["done"],
            "nodes": [
                {
                    "id": "analyze", "kind": "action",
                    "handler": {"name": "agent.claude", "version": "1.0.0"},
                    "outputs": [{"id": "result"}],
                },
                {
                    "id": "done", "kind": "terminal",
                    "inputs": [{"id": "result"}],
                },
            ],
            "edges": [{
                "source_node": "analyze", "source_port": "result",
                "target_node": "done", "target_port": "result",
            }],
        }
        inputs = [{
            "id": "prompt", "required": True, "has_default": False,
            "schema": {"type": "object"}, "transport": "inline",
        }]
        binding = WorkflowCatalogReadModelService._goal_binding(ir, inputs)

        self.assertEqual(
            ("ready", None),
            WorkflowCatalogReadModelService._goal_readiness(
                ir, inputs, binding, source_available=True,
            ),
        )

    def test_unready_workflow_is_upgradeable_only_when_source_exists(self) -> None:
        ir = {"entry": [], "terminals": [], "nodes": [], "edges": []}
        self.assertEqual(
            ("needs_upgrade", "goal_binding_missing"),
            WorkflowCatalogReadModelService._goal_readiness(
                ir, [], None, source_available=True,
            ),
        )
        self.assertEqual(
            ("needs_migration", "goal_binding_missing"),
            WorkflowCatalogReadModelService._goal_readiness(
                ir, [], None, source_available=False,
            ),
        )

    def test_required_non_goal_input_must_have_a_default(self) -> None:
        ir = {
            "entry": ["analyze"], "terminals": ["done"],
            "nodes": [
                {
                    "id": "analyze", "kind": "action",
                    "handler": {"name": "agent.claude", "version": "1.0.0"},
                    "outputs": [{"id": "result"}],
                },
                {"id": "done", "kind": "terminal", "inputs": [{"id": "result"}]},
            ],
            "edges": [{
                "source_node": "analyze", "source_port": "result",
                "target_node": "done", "target_port": "result",
            }],
        }
        inputs = [
            {
                "id": "prompt", "required": True, "has_default": False,
                "schema": {"type": "object"}, "transport": "inline",
            },
            {
                "id": "region", "required": True, "has_default": False,
                "schema": {"type": "string"}, "transport": "inline",
            },
        ]
        binding = WorkflowCatalogReadModelService._goal_binding(ir, inputs)

        self.assertIsNotNone(binding)
        self.assertEqual(
            ("needs_upgrade", "required_input_without_default"),
            WorkflowCatalogReadModelService._goal_readiness(
                ir, inputs, binding, source_available=True,
            ),
        )
        inputs[1]["has_default"] = True
        self.assertEqual(
            ("ready", None),
            WorkflowCatalogReadModelService._goal_readiness(
                ir, inputs, binding, source_available=True,
            ),
        )


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
        # A SQLite connection opened during the test may still be waiting to be
        # collected when the directory goes, and closing it writes `-wal` back
        # into a directory `rmtree` has already walked. That made roughly one
        # run in ten fail in `tearDown`, in whichever test happened to be last —
        # never in the assertion, and never the same test twice.
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self.temp.name) / "runtime.db"
        self.artifact_backend = LocalCASBackend(Path(self.temp.name) / "artifacts")
        self.scopes = {
            "reader": [READ_SCOPE],
            "writer": [READ_SCOPE, WRITE_SCOPE, OPS_READ_SCOPE, OPS_WRITE_SCOPE],
            "second-writer": [READ_SCOPE, WRITE_SCOPE, OPS_READ_SCOPE, OPS_WRITE_SCOPE],
            "ops-reader": [READ_SCOPE, OPS_READ_SCOPE],
            "sensitive": [READ_SCOPE, SENSITIVE_SCOPE],
            "other-sensitive": [READ_SCOPE, SENSITIVE_SCOPE],
            # Authors a workflow and may read the Agent console it produced.
            "author": [READ_SCOPE, WRITE_SCOPE, SENSITIVE_SCOPE],
            "nobody": [],
        }
        self.app = create_app(
            self.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: self.scopes.get(actor, [])),
            artifact_backend=self.artifact_backend,
            single_goal_mode=False,
            langgraph_state_directory=Path(self.temp.name) / "langgraph",
        )
        publish_linear_workflow(self.db)

    def tearDown(self) -> None:
        self.temp.cleanup()


class RateLimitTests(unittest.TestCase):
    """The limit protects a shared deployment, never the local operator."""

    def build(self, **extra):
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        db = Path(temp.name) / "runtime.db"
        app = create_app(
            db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: [READ_SCOPE]),
            rate_limiter=RateLimiter(requests=2, window_seconds=60),
            **extra,
        )
        publish_linear_workflow(db)
        return app

    def test_an_ordinary_actor_is_throttled(self) -> None:
        with AsgiHarness(self.build()) as client:
            codes = [
                client.get("/api/v1/workflows", actor="reader").status_code
                for _ in range(3)
            ]
            self.assertEqual([200, 200, 429], codes)

    def test_a_vouched_for_actor_is_not(self) -> None:
        app = self.build(unlimited_actors=(LOCAL_ACTOR,))
        with AsgiHarness(app) as client:
            codes = [
                client.get("/api/v1/workflows", actor=LOCAL_ACTOR).status_code
                for _ in range(5)
            ]
            self.assertEqual([200] * 5, codes)
            # The exemption is per actor, not a switch that disables the limit.
            self.assertEqual(
                [200, 200, 429],
                [client.get("/api/v1/workflows", actor="reader").status_code
                 for _ in range(3)],
            )


class ReadAuthTests(ApiTestCase):
    def test_missing_credentials_are_rejected(self) -> None:
        with AsgiHarness(self.app) as client:
            response = client.get("/api/v1/workflows")
            self.assertEqual(401, response.status_code)
            self.assertEqual("unauthenticated", response.json()["error"]["code"])

    def test_actor_without_scope_is_forbidden(self) -> None:
        with AsgiHarness(self.app) as client:
            response = client.get("/api/v1/workflows", actor="nobody")
            self.assertEqual(403, response.status_code)
            self.assertEqual("forbidden", response.json()["error"]["code"])

    def test_reader_can_read(self) -> None:
        with AsgiHarness(self.app) as client:
            response = client.get("/api/v1/workflows", actor="reader")
            self.assertEqual(200, response.status_code, response.text)
            body = response.json()
            self.assertEqual("1.0", body["schema_version"])

    def test_reader_cannot_write(self) -> None:
        with AsgiHarness(self.app) as client:
            response = client.post(
                "/api/v1/workflows/workflow:linear/versions",
                actor="reader", key="k1",
                body={"source": "{}", "expected_version": 1},
            )
            self.assertEqual(403, response.status_code)


class HandlerDriftTests(unittest.TestCase):
    """A published plan pins a Handler build; upgrading the build strands it."""

    DRIFTED = {
        "dsl_version": "1.2",
        "metadata": {"id": "drifted", "name": "Drifted"},
        "nodes": [
            {
                "id": "work", "kind": "action",
                "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
                "outputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
                "handler": {"name": "transform", "version": "0.9.0"},
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

    def setUp(self) -> None:
        import json as json_module

        from orbit.workflow.catalogs import InMemoryHandlerCatalog, InMemorySchemaCatalog
        from orbit.workflow.dsl import compile_source
        from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore

        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "runtime.db"
        self.app = create_app(
            self.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: [READ_SCOPE, WRITE_SCOPE]),
            single_goal_mode=False,
            langgraph_state_directory=Path(self.temp.name) / "langgraph",
        )
        # A version whose plan pins transform@0.9.0, kept coherent with its own
        # source. The running registry has transform@1.0.0 — the exact drift an
        # upgraded Agent CLI produces.
        stale = self.manifest_at("0.9.0")
        source = json_module.dumps(self.DRIFTED)
        compiled = compile_source(
            source, InMemoryHandlerCatalog([stale]),
            InMemorySchemaCatalog(dict(SCHEMAS)), source_format="json",
        )
        SQLiteWorkflowVersionStore(self.db).publish(
            compiled, expected_latest_version=0, source_format="json",
            source_text=source, actor="drift-test",
        )

    @staticmethod
    def manifest_at(version: str) -> HandlerManifest:
        return HandlerManifest(
            "transform", version, ("action",),
            {"value": "example://integer/1.0"},
            {"value": "example://integer/1.0"},
            {"type": "object"}, ExecutionSafety.REPLAY_SAFE,
            ResourceProfile(100, 100, 5, 60, 1_000_000, "test"),
            "schema://object/1.0", (), (), True, True,
        )

    def detail(self, client):
        return client.get(
            "/api/v1/workflows/workflow:drifted", actor="writer"
        ).json()["data"]

    def test_the_stale_binding_is_named_not_buried(self) -> None:
        with AsgiHarness(self.app) as client:
            data = self.detail(client)
            drift = data["handler_drift"]
            self.assertEqual(1, len(drift))
            self.assertEqual(
                ("transform", "0.9.0", "1.0.0", "version_changed"),
                (drift[0]["handler_name"], drift[0]["pinned_version"],
                 drift[0]["available_version"], drift[0]["status"]),
            )
            self.assertIn(
                "workflow.rebind",
                [c["command"] for c in data["allowed_commands"]],
            )

    def test_starting_the_stranded_version_says_so_and_does_not_ask_to_retry(self) -> None:
        with AsgiHarness(self.app) as client:
            response = client.post(
                "/api/v1/langgraph-runs", actor="writer", key="run-stranded",
                body={
                    "workflow_id": "workflow:drifted", "workflow_version": 1,
                    "input": {"value": 1},
                },
            )
            self.assertEqual(409, response.status_code, response.text)
            self.assertIn(
                "0.9.0", response.json()["error"]["message"],
            )

    def test_rebind_moves_every_node_to_the_installed_build(self) -> None:
        with AsgiHarness(self.app) as client:
            before = self.detail(client)
            rebind = next(
                c for c in before["allowed_commands"] if c["command"] == "workflow.rebind"
            )
            result = client.post(
                rebind["href"], actor="writer", key="rebind-1",
                body={"expected_version": rebind["expected_version"]},
            )
            self.assertEqual(200, result.status_code, result.text)
            data = result.json()["data"]
            self.assertEqual(2, data["version"])
            self.assertEqual(
                [("work", "0.9.0", "1.0.0")],
                [(m["node_id"], m["from"], m["to"]) for m in data["rebound"]],
            )
            # What the catalog now serves is clean, and the run it refused
            # starts.
            self.assertEqual([], self.detail(client)["handler_drift"])
            started = client.post(
                "/api/v1/langgraph-runs", actor="writer", key="run-after-rebind",
                body={"workflow_id": "workflow:drifted", "input": {"value": 1}},
            )
            self.assertEqual(200, started.status_code, started.text)

    def test_a_version_without_source_cannot_be_rebound(self) -> None:
        from orbit.workflow.catalogs import InMemoryHandlerCatalog, InMemorySchemaCatalog
        from orbit.workflow.dsl import compile_source
        from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
        import json as json_module

        source = json_module.dumps({**self.DRIFTED, "metadata": {"id": "sourceless", "name": "Sourceless"}})
        compiled = compile_source(
            source, InMemoryHandlerCatalog([self.manifest_at("0.9.0")]),
            InMemorySchemaCatalog(dict(SCHEMAS)), source_format="json",
        )
        SQLiteWorkflowVersionStore(self.db).publish(
            compiled, expected_latest_version=0, source_format="json",
            source_text=None, actor="drift-test",
        )
        with AsgiHarness(self.app) as client:
            data = client.get(
                "/api/v1/workflows/workflow:sourceless", actor="writer"
            ).json()["data"]
            # Drift is still reported — the operator must know — but a rebind
            # that has no source to rewrite is not offered.
            self.assertTrue(data["handler_drift"])
            self.assertNotIn(
                "workflow.rebind", [c["command"] for c in data["allowed_commands"]]
            )


class CatalogTests(ApiTestCase):
    def test_writer_can_delete_workflow_from_advertised_card_command(self) -> None:
        with AsgiHarness(self.app) as client:
            catalog = client.get("/api/v1/workflows", actor="writer").json()["data"]
            item = next(
                value for value in catalog["workflows"]
                if value["workflow_id"] == "workflow:linear"
            )
            command = next(
                value for value in item["allowed_commands"]
                if value["command"] == "workflow.delete"
            )
            denied = client.request(
                command["method"], command["href"], actor="reader",
                headers={"idempotency-key": "delete-denied"},
                body={"expected_version": command["expected_version"]},
            )
            self.assertEqual(403, denied.status_code)

            stale = client.request(
                command["method"], command["href"], actor="writer",
                headers={"idempotency-key": "delete-stale"},
                body={"expected_version": 0},
            )
            self.assertEqual(409, stale.status_code)

            deleted = client.request(
                command["method"], command["href"], actor="writer",
                headers={"idempotency-key": "delete-linear"},
                body={"expected_version": command["expected_version"]},
            )
            self.assertEqual(200, deleted.status_code, deleted.text)
            self.assertTrue(deleted.json()["data"]["deleted"])
            remaining = client.get(
                "/api/v1/workflows", actor="writer"
            ).json()["data"]["workflows"]
            self.assertNotIn(
                "workflow:linear", [value["workflow_id"] for value in remaining]
            )
            self.assertEqual(
                404,
                client.get("/api/v1/workflows/workflow:linear", actor="writer").status_code,
            )
            stale_start = client.post(
                "/api/v1/langgraph-runs", actor="writer", key="start-deleted",
                body={
                    "workflow_id": "workflow:linear",
                    "workflow_version": command["expected_version"],
                    "input": {"value": 1},
                },
            )
            # A card whose workflow was deleted between render and click. The
            # engine refuses the start rather than running a definition whose
            # id the catalog has retired — it used to run it.
            self.assertEqual(409, stale_start.status_code)
            self.assertIn("deleted", stale_start.json()["error"]["message"])

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
            self.assertEqual(0, entry["attempt_count"])
            self.assertEqual(0, entry["failed_count"])
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
            poll_seconds=0.02,
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

    def _goal_ready_app(self):
        """An app whose catalog carries one goal-ready workflow.

        run.start is advertised only for a definition whose entry step is an
        Agent taking the goal envelope on a `prompt` port, and the command
        re-checks that the bound handler is registered — so the fixture both
        publishes such a definition and boots an app carrying its handler.
        Whether the run later executes is the Runtime's concern; the catalog
        tests only read the advertisement and the acceptance.
        """
        from orbit.workflow.domain.definitions import (
            CompiledWorkflow, IREdge, IRHandlerRef, IRNode, IRPort, WorkflowIR,
        )
        from orbit.workflow.domain.serialization import definition_hash
        from orbit.workflow.persistence.workflow_versions import (
            SQLiteWorkflowVersionStore,
        )

        manifest = HandlerManifest(
            "agent.test", "1.0.0", ("action",),
            {"prompt": "schema://object/1.0"},
            {"value": "schema://object/1.0"},
            {"type": "object"},
            ExecutionSafety.REPLAY_SAFE,
            ResourceProfile(100, 100, 5, 60, 1_000_000, "test"),
            "schema://object/1.0", (), (), True, True,
        )
        prompt = IRPort("prompt", "schema://object/1.0", True, False, None, "")
        value = IRPort("value", "schema://object/1.0", True, False, None, "")
        ref = IRHandlerRef(
            manifest.name, manifest.version, manifest.fingerprint,
        )
        ir = WorkflowIR(
            "1.1", "workflow:research", "Research", "", {}, (), (),
            (
                IRNode(
                    # The engine runs the node now rather than queueing it, so
                    # the fixture handler has to answer on the port the node
                    # declares instead of echoing its input.
                    "ask", "action", (prompt,), (value,), ref,
                    {"operation": "build_object", "value": {"value": {}}},
                    (), None, label="Ask",
                ),
                IRNode(
                    "done", "terminal", (value,), (), None, {}, (), None,
                    label="Finish",
                ),
            ),
            (
                IREdge(
                    "ask_done", "ask", "value", "done", "value", "success",
                    {"op": "literal", "value": True},
                    {"op": "identity", "schema_id": "schema://object/1.0"},
                ),
            ),
            ("ask",), ("done",), (), (), {},
        )
        SQLiteWorkflowVersionStore(self.db).publish(
            CompiledWorkflow(ir, definition_hash(ir), "1.0", manifest.fingerprint),
            expected_latest_version=0, source_format="json", source_text=None,
            actor="test",
        )
        return create_app(
            self.db,
            handlers=[
                transform_registration(),
                HandlerRegistration(manifest, TransformHandler(), "agent.test@1.0.0"),
            ],
            schemas=SCHEMAS,
            poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: self.scopes.get(actor, [])),
            artifact_backend=self.artifact_backend,
            single_goal_mode=False,
            langgraph_state_directory=self.db.parent / "langgraph",
        )

    def test_workflow_catalog_advertises_start_only_to_writers(self) -> None:
        app = self._goal_ready_app()
        with AsgiHarness(app) as client:
            reader = client.get("/api/v1/workflows", actor="reader")
            self.assertEqual(200, reader.status_code, reader.text)
            reader_entries = {
                item["workflow_id"]: item
                for item in reader.json()["data"]["workflows"]
            }
            self.assertEqual([], reader_entries["workflow:research"]["allowed_commands"])

            writer = client.get("/api/v1/workflows", actor="writer")
            entries = {
                item["workflow_id"]: item
                for item in writer.json()["data"]["workflows"]
            }
            linear = entries["workflow:linear"]
            self.assertEqual("Linear", linear["name"])
            self.assertEqual("structured", linear["input_mode"])
            self.assertIsNone(linear["goal_binding"])
            self.assertEqual("value", linear["inputs"][0]["id"])
            self.assertEqual("integer", linear["inputs"][0]["schema"]["type"])
            self.assertEqual(4, linear["summary"]["node_count"])
            # Structured input is startable: the engine takes an input object,
            # so "goal-ready" is no longer what gates the offer. What gates it
            # is whether the definition compiles for the engine at all.
            self.assertIn(
                "langgraph_run.start",
                [value["command"] for value in linear["allowed_commands"]],
            )
            entry = entries["workflow:research"]
            command = entry["allowed_commands"][0]
            self.assertEqual("langgraph_run.start", command["command"])
            started = client.post(
                command["href"], actor="writer", key="catalog-start",
                body={
                    "workflow_id": entry["workflow_id"],
                    "workflow_version": entry["latest_version"],
                    "expected_version": command["expected_version"],
                    "input": {"prompt": {"goal": "research a topic"}},
                },
            )
            self.assertEqual(200, started.status_code, started.text)

    def test_catalog_reports_when_a_definition_was_last_used(self) -> None:
        """Ordering a catalog by "recently used" is a fact about runs."""

        app = self._goal_ready_app()
        with AsgiHarness(app) as client:
            entry = next(
                item
                for item in client.get(
                    "/api/v1/workflows", actor="writer"
                ).json()["data"]["workflows"]
                if item["workflow_id"] == "workflow:research"
            )
            self.assertIsNone(entry["last_run_at"])
            self.assertEqual(0, entry["run_count"])

            command = entry["allowed_commands"][0]
            started = client.post(
                command["href"], actor="writer", key="catalog-usage",
                body={
                    "workflow_id": entry["workflow_id"],
                    "workflow_version": entry["latest_version"],
                    "expected_version": command["expected_version"],
                    "input": {"prompt": {"goal": "research a topic"}},
                },
            )
            self.assertEqual(200, started.status_code, started.text)

            used = next(
                item
                for item in client.get(
                    "/api/v1/workflows", actor="writer"
                ).json()["data"]["workflows"]
                if item["workflow_id"] == "workflow:research"
            )
            self.assertIsNotNone(used["last_run_at"])
            self.assertEqual(1, used["run_count"])

    def test_workflow_definition_read_is_current_only_and_actor_shaped(self) -> None:
        with AsgiHarness(self.app) as client:
            reader = client.get("/api/v1/workflows/workflow:linear", actor="reader")
            self.assertEqual(200, reader.status_code, reader.text)
            detail = reader.json()["data"]
            self.assertEqual("workflow:linear", detail["workflow_id"])
            self.assertEqual(1, detail["latest_version"])
            self.assertEqual("workflow:linear", detail["definition"]["workflow_id"])
            self.assertEqual([], detail["allowed_commands"])
            # Superseded definitions are not addressable: no selector, and no
            # per-version payload for a caller to walk back through.
            self.assertNotIn("versions", detail)
            self.assertNotIn("selected_version", detail)

            selected = client.get(
                "/api/v1/workflows/workflow:linear?version=1", actor="writer"
            )
            self.assertEqual(404, selected.status_code)
            self.assertEqual("not_found", selected.json()["error"]["code"])

            missing = client.get("/api/v1/workflows/workflow:absent", actor="writer")
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

    def test_validate_compiles_a_draft_without_publishing_it(self) -> None:
        """The endpoint an author leans on before committing to a version.

        It compiles and answers, and it must change nothing: a draft that
        validates is still a draft, and the catalog should not have heard of
        it. Nothing had tested it at all.
        """

        import json as json_module

        source = json_module.dumps(self.GENERATED)
        with AsgiHarness(self.app) as client:
            response = client.post(
                "/api/v1/workflows/validate", actor="writer", key="val-1",
                body={"source": source, "expected_version": 0},
            )
            self.assertEqual(200, response.status_code, response.text)
            data = response.json()["data"]
            self.assertEqual("workflow:prompted", data["workflow_id"])
            self.assertEqual(0, data["latest_version"])
            self.assertTrue(data["definition_hash"].startswith("sha256:"))
            self.assertGreaterEqual(data["node_count"], 1)
            self.assertEqual(
                {"workflow.publish", "workflow.validate"},
                {item["command"] for item in data["allowed_commands"]},
            )

            entries = client.get(
                "/api/v1/workflows", actor="reader"
            ).json()["data"]["workflows"]
            self.assertNotIn(
                "workflow:prompted", [item["workflow_id"] for item in entries]
            )

    def test_validate_reports_diagnostics_rather_than_a_bare_refusal(self) -> None:
        """An author fixes what the compiler names; a refusal with none is a wall."""

        import json as json_module

        with AsgiHarness(self.app) as client:
            response = client.post(
                "/api/v1/workflows/validate", actor="writer", key="val-2",
                body={"source": '{"dsl_version": "1.2"}', "expected_version": 0},
            )
            self.assertEqual(409, response.status_code)
            payload = json_module.loads(response.json()["error"]["message"])
            self.assertEqual("workflow source failed validation", payload["message"])
            self.assertTrue(payload["diagnostics"])
            self.assertTrue(all("code" in item for item in payload["diagnostics"]))

    def test_validate_refuses_an_empty_or_absent_source(self) -> None:
        """409 rather than 400: this boundary answers every ValueError alike.

        A missing argument is a request error and a stale version is a state
        error, and both arrive as `invalid_command` with a 409. Asserted as it
        behaves rather than as it reads, so that changing the mapping is a
        deliberate act with a test to update.
        """

        with AsgiHarness(self.app) as client:
            for index, body in enumerate((
                {"expected_version": 0},
                {"source": "   ", "expected_version": 0},
                {"source": 17, "expected_version": 0},
            )):
                with self.subTest(body=body):
                    response = client.post(
                        "/api/v1/workflows/validate", actor="writer",
                        key=f"val-empty-{index}", body=body,
                    )
                    self.assertEqual(409, response.status_code)
                    self.assertIn("source is required", response.json()["error"]["message"])

    def test_validate_answers_a_stale_expected_version_with_the_conflict(self) -> None:
        """Validating against a version that moved would approve a stale edit."""

        import json as json_module

        source = json_module.dumps(self.GENERATED)
        with AsgiHarness(self.app) as client:
            response = client.post(
                "/api/v1/workflows/validate", actor="writer", key="val-stale",
                body={"source": source, "expected_version": 3},
            )
            self.assertEqual(409, response.status_code)
            self.assertIn("draft version conflict", response.json()["error"]["message"])

    def test_validate_is_a_write_scope_operation(self) -> None:
        import json as json_module

        with AsgiHarness(self.app) as client:
            response = client.post(
                "/api/v1/workflows/validate", actor="reader", key="val-denied",
                body={"source": json_module.dumps(self.GENERATED), "expected_version": 0},
            )
            self.assertEqual(403, response.status_code)

    def test_publish_rejects_a_source_that_names_a_different_workflow(self) -> None:
        import json as json_module

        source = json_module.dumps(self.GENERATED)
        with AsgiHarness(self.app) as client:
            response = client.post(
                "/api/v1/workflows/workflow:someone-else/versions",
                actor="writer", key="pub-2",
                body={"source": source, "expected_version": 0},
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

        source = json_module.dumps(self.GENERATED)
        with AsgiHarness(self.app) as client:
            stale = client.post(
                "/api/v1/workflows/workflow:prompted/versions",
                actor="writer", key="pub-3",
                body={"source": source, "expected_version": 7},
            )
            self.assertEqual(409, stale.status_code)
            denied = client.post(
                "/api/v1/workflows/workflow:prompted/versions",
                actor="reader", key="pub-4",
                body={"source": source, "expected_version": 0},
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
            poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: self.scopes.get(actor, [])),
            workflow_generator=generate,
            single_goal_mode=False,
        )

    def _app_without_reviser(self):
        return create_app(
            self.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: self.scopes.get(actor, [])),
            single_goal_mode=False,
        )

    @staticmethod
    def _agent_registration(name="agent.codex", version="1.0.0"):
        manifest = HandlerManifest(
            name, version, ("action",),
            {"value": "example://integer/1.0"},
            {"value": "example://integer/1.0"},
            {"type": "object"}, ExecutionSafety.REPLAY_SAFE,
            ResourceProfile(100_000, 100_000, 0, 300, 0, "agent"),
            "schema://object/1.0", ("agent.invoke",), (), True, True,
        )
        return HandlerRegistration(manifest, TransformHandler(), f"{name}@{version}")

    def _app_with_action_agents(self):
        return create_app(
            self.db,
            handlers=[
                transform_registration(), self._agent_registration(),
                self._agent_registration("agent.claude", "2.0.0"),
            ],
            schemas=SCHEMAS, poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: self.scopes.get(actor, [])),
            single_goal_mode=False,
        )

    def test_an_edit_need_not_restate_which_agent_runs_the_step(self) -> None:
        """Changing the wording is not a decision about the Agent.

        `handler` was required, so editing a label meant restating a binding
        the editor had no business changing — and where the Runtime binds
        every Agent step itself there is no honest value to restate, because
        the Agent the definition names need not be installed at all.
        """

        with AsgiHarness(self._app_with_action_agents()) as client:
            detail = client.get(
                "/api/v1/workflows/workflow:draftable", actor="writer",
            ).json()["data"]
            editor = detail["action_editors"]["work"]
            before = next(
                node for node in detail["definition"]["nodes"] if node["id"] == "work"
            )["handler"]

            response = client.post(
                editor["allowed_command"]["href"], actor="writer", key="wording-only",
                body={
                    "expected_version": editor["allowed_command"]["expected_version"],
                    "label": "Search the web",
                    "prompt": "Find reliable primary sources.",
                },
            )

            self.assertEqual(200, response.status_code, response.json())
            self.assertEqual(
                ["label", "prompt"], response.json()["data"]["changed_fields"],
            )
            updated = client.get(
                "/api/v1/workflows/workflow:draftable", actor="writer",
            ).json()["data"]
            work = next(
                node for node in updated["definition"]["nodes"] if node["id"] == "work"
            )
            self.assertEqual("Search the web", work["label"])
            self.assertEqual(before, work["handler"])

    def test_action_editor_updates_only_action_fields_as_a_new_version(self) -> None:
        with AsgiHarness(self._app_with_action_agents()) as client:
            detail = client.get(
                "/api/v1/workflows/workflow:draftable", actor="writer",
            ).json()["data"]
            self.assertEqual({"work"}, set(detail["action_editors"]))
            editor = detail["action_editors"]["work"]
            self.assertEqual(
                [
                    {"name": "agent.claude", "version": "2.0.0"},
                    {"name": "agent.codex", "version": "1.0.0"},
                ],
                editor["handlers"],
            )
            response = client.post(
                editor["allowed_command"]["href"], actor="writer", key="action-edit-1",
                body={
                    "expected_version": editor["allowed_command"]["expected_version"],
                    "label": "Search web",
                    "handler": {"name": "agent.codex", "version": "1.0.0"},
                    "prompt": "Find reliable primary sources.",
                },
            )
            self.assertEqual(200, response.status_code, response.json())
            self.assertEqual(2, response.json()["data"]["version"])
            updated = client.get(
                "/api/v1/workflows/workflow:draftable", actor="writer",
            ).json()["data"]
            work = next(node for node in updated["definition"]["nodes"] if node["id"] == "work")
            self.assertEqual("Search web", work["label"])
            self.assertEqual("agent.codex", work["handler"]["name"])
            self.assertEqual("1.0.0", work["handler"]["version"])
            self.assertEqual("Find reliable primary sources.", work["config"]["prompt"])
            self.assertEqual(["done"], updated["definition"]["terminals"])

    def test_action_editor_is_not_offered_to_readers_or_terminal_nodes(self) -> None:
        with AsgiHarness(self._app_with_action_agents()) as client:
            reader = client.get(
                "/api/v1/workflows/workflow:draftable", actor="reader",
            ).json()["data"]
            self.assertEqual({}, reader["action_editors"])
            denied = client.post(
                "/api/v1/workflows/workflow:draftable/actions/done",
                actor="writer", key="action-edit-terminal",
                body={
                    "expected_version": 1, "label": "Not allowed",
                    "handler": {"name": "agent.codex", "version": "1.0.0"},
                    "prompt": "No.",
                },
            )
            self.assertEqual(409, denied.status_code)

    def test_agent_cli_version_change_is_not_reported_as_handler_drift(self) -> None:
        import json as json_module
        from orbit.workflow.application.workflows import (
            WorkflowCatalogs, WorkflowDefinitionService,
        )
        from orbit.workflow.catalogs import InMemoryHandlerCatalog, InMemorySchemaCatalog
        from orbit.workflow.catalogs.extensions import InMemoryExtensionRegistry
        from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
        from tests.test_workflow_drafts import dsl as editable_dsl

        source = editable_dsl(workflow_id="agent-drift", name="Agent drift")
        source["nodes"][0]["handler"] = {
            "name": "agent.codex", "version": "1.1.5",
        }
        old_agent = self._agent_registration("agent.codex", "1.1.5").manifest
        WorkflowDefinitionService(
            WorkflowCatalogs(
                InMemoryHandlerCatalog([old_agent]),
                InMemorySchemaCatalog(SCHEMAS), InMemoryExtensionRegistry(),
            ),
            SQLiteWorkflowVersionStore(self.db),
        ).publish_workflow(
            json_module.dumps(source), source_name="<agent-drift>",
            source_format="json", expected_latest_version=0, actor="fixture",
        )
        app = create_app(
            self.db,
            handlers=[self._agent_registration("agent.codex", "1.1.7")],
            schemas=SCHEMAS, poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: self.scopes.get(actor, [])),
            single_goal_mode=False,
        )
        with AsgiHarness(app) as client:
            detail = client.get(
                "/api/v1/workflows/workflow:agent-drift", actor="writer",
            ).json()["data"]
            # Not drift: an Agent CLI release is an operational upgrade, and
            # the step is carried to the build that is installed rather than
            # sent for a recompile. `rebound` rather than `current`, because
            # the engine's registry is pinned to the exact build — reporting
            # "current" said no repair was needed while the run refused to
            # start for a version nobody had moved off.
            self.assertEqual([], detail["handler_drift"])
            binding = detail["handler_bindings"][0]
            self.assertEqual("rebound", binding["status"])
            self.assertEqual("agent.codex", binding["rebound_to"])
            self.assertEqual("1.1.7", binding["available_version"])
            self.assertNotIn(
                "workflow.rebind",
                [command["command"] for command in detail["allowed_commands"]],
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

    def test_catalog_advertises_card_editing_only_to_writers_with_source(self) -> None:
        with AsgiHarness(self.app) as client:
            writer = client.get("/api/v1/workflows", actor="writer").json()["data"]
            reader = client.get("/api/v1/workflows", actor="reader").json()["data"]
            writer_items = {item["workflow_id"]: item for item in writer["workflows"]}
            reader_items = {item["workflow_id"]: item for item in reader["workflows"]}
            self.assertTrue(writer_items["workflow:draftable"]["editing_available"])
            self.assertFalse(writer_items["workflow:linear"]["editing_available"])
            self.assertFalse(reader_items["workflow:draftable"]["editing_available"])

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
            # Editing again edits what was just published: the next draft is
            # based on the definition the catalog serves, never an older one.
            create = next(
                item for item in refreshed["allowed_commands"]
                if item["command"] == "workflow.draft.create"
            )
            self.assertEqual(2, create["expected_version"])

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

    def _app_with_named_agents(self, generators):
        return create_app(
            self.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: self.scopes.get(actor, [])),
            workflow_generators=generators,
            single_goal_mode=False,
        )

    def test_generation_names_the_agent_that_writes_the_dsl(self) -> None:
        import json as json_module
        from tests.test_workflow_drafts import dsl as editable_dsl

        asked = []

        def writer(name):
            def generate(_prompt):
                asked.append(name)
                return json_module.dumps(editable_dsl(name=f"By {name}"))
            return generate

        app = self._app_with_named_agents(
            {"codex": writer("codex"), "claude": writer("claude")}
        )
        with AsgiHarness(app) as client:
            caps = client.get(
                "/api/v1/capabilities", actor="writer"
            ).json()["data"]["capabilities"]
            self.assertEqual(
                ["claude", "codex"], caps["workflow_generation"]["agents"]
            )
            response = client.post(
                "/api/v1/workflows/generate", actor="writer", key="gen-codex",
                body={"instruction": "a flow", "agent": "codex"},
            )
            self.assertEqual(200, response.status_code, response.json())
            job = response.json()["data"]
            self.assertEqual("codex", job["requested_agent"])
            for _ in range(200):
                if job["status"] not in ("queued", "running"):
                    break
                import time
                time.sleep(0.02)
                job = client.get(job["href"], actor="writer").json()["data"]
            self.assertEqual("done", job["status"], job.get("error"))
            self.assertEqual(["codex"], asked)

    def test_http_mounts_the_authoring_routes(self) -> None:
        app = create_app(
            self.db, handlers=[transform_registration(), self._agent_registration()],
            schemas=SCHEMAS,
            poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: self.scopes.get(actor, [])),
            workflow_generators={"app:codex": lambda _prompt: "{}"}, single_goal_mode=False,
        )
        # The mode decides how many Agents an author picks between, not which
        # endpoints exist. A Runtime that advertised generation in its
        # capabilities and then had no route for it was reporting a promise it
        # could not keep.
        self.assertIn(
            "workflow_generate",
            {route.name for route in app.routes if getattr(route, "name", None)},
        )
        with AsgiHarness(app) as client:
            response = client.post(
                "/api/v1/workflows/generate", actor="writer", key="single-codex",
                body={
                    "prompt": "implement the requested change",
                    "agent": "app:codex",
                },
            )

        self.assertEqual(200, response.status_code, response.text)

    def test_multi_agent_generation_prompt_is_unchanged(self) -> None:
        prompts = []

        def writer(prompt):
            prompts.append(prompt)
            return "{}"

        app = self._app_with_named_agents({"codex": writer})
        with AsgiHarness(app) as client:
            response = client.post(
                "/api/v1/workflows/generate", actor="writer", key="multi-codex",
                body={
                    "prompt": "use several specialist agents",
                    "agent": "codex",
                },
            )
            self.assertEqual(200, response.status_code, response.json())
            job = response.json()["data"]
            import time
            for _ in range(100):
                job = client.get(job["href"], actor="writer").json()["data"]
                if job["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.01)

        self.assertTrue(prompts)
        self.assertNotIn("ORBIT_SINGLE_AGENT", prompts[0])
        self.assertIn("use several specialist agents", prompts[0])

    def test_authoring_job_keeps_the_agent_cli_console(self) -> None:
        """A job that thinks for a minute must not be a black box.

        What the CLI printed is read back as a tail, in order, through the
        address the job itself advertises.
        """
        import json as json_module
        import time
        from orbit.workflow.authoring import active_scope
        from tests.test_workflow_drafts import dsl as editable_dsl

        def writer(_prompt):
            scope = active_scope()
            scope.on_output("stderr", "planning the flow\n")
            scope.on_output("stderr", "writing nodes\n")
            return json_module.dumps(
                editable_dsl(workflow_id="talkative", name="Talkative")
            )

        app = self._app_with_named_agents({"chatty": writer})
        with AsgiHarness(app) as client:
            job = client.post(
                "/api/v1/workflows/generate", actor="author", key="gen-console",
                body={"instruction": "a flow"},
            ).json()["data"]
            for _ in range(200):
                job = client.get(job["href"], actor="author").json()["data"]
                if job["status"] not in ("queued", "running"):
                    break
                time.sleep(0.02)
            self.assertEqual("done", job["status"], job.get("error"))
            output = client.get(job["output_href"], actor="author")
            self.assertEqual(200, output.status_code, output.json())
            chunks = output.json()["data"]["chunks"]
        self.assertEqual(
            ["planning the flow\n", "writing nodes\n"],
            [
                chunk["text"] for chunk in chunks
                if not chunk["text"].startswith("\x1eorbit-progress:")
            ],
        )
        self.assertEqual({"stderr"}, {chunk["stream"] for chunk in chunks})

    def test_capabilities_name_the_agent_used_when_none_is_requested(self) -> None:
        """The advertised default is the one an unnamed request really gets.

        The agent list is sorted for display, so its first entry is not the
        fallback; a UI that preselected it would name the wrong writer.
        """
        import json as json_module
        from tests.test_workflow_drafts import dsl as editable_dsl

        asked = []

        def writer(name):
            def generate(_prompt):
                asked.append(name)
                return json_module.dumps(editable_dsl(name=f"By {name}"))
            return generate

        app = self._app_with_named_agents(
            {"codex": writer("codex"), "claude": writer("claude")}
        )
        with AsgiHarness(app) as client:
            caps = client.get(
                "/api/v1/capabilities", actor="writer"
            ).json()["data"]["capabilities"]
            self.assertEqual(
                ["claude", "codex"], caps["workflow_generation"]["agents"]
            )
            self.assertEqual("codex", caps["workflow_generation"]["default_agent"])
            self.assertEqual("codex", caps["workflow_editing"]["default_agent"])
            response = client.post(
                "/api/v1/workflows/generate", actor="writer", key="gen-default",
                body={"instruction": "a flow"},
            )
            self.assertEqual(200, response.status_code, response.json())
            job = response.json()["data"]
            for _ in range(200):
                if job["status"] not in ("queued", "running"):
                    break
                import time
                time.sleep(0.02)
                job = client.get(job["href"], actor="writer").json()["data"]
            self.assertEqual("done", job["status"], job.get("error"))
        self.assertEqual(["codex"], asked)

    def test_quick_modify_names_the_agent_that_revises_the_workflow(self) -> None:
        import json as json_module
        import time
        from tests.test_workflow_drafts import dsl as editable_dsl

        asked = []

        def codex(_prompt):
            asked.append("codex")
            return json_module.dumps(editable_dsl(name="Modified by Codex"))

        app = self._app_with_named_agents({"codex": codex, "claude": lambda _p: "{}"})
        with AsgiHarness(app) as client:
            response = client.post(
                "/api/v1/workflows/workflow:draftable/modify",
                actor="writer", key="modify-codex",
                body={"prompt": "rename it", "mode": "modify", "agent": "codex"},
            )
            self.assertEqual(200, response.status_code, response.json())
            self.assertEqual("codex", response.json()["data"]["requested_agent"])
            job = response.json()["data"]
            for _ in range(200):
                if asked or job["status"] not in ("queued", "running"):
                    break
                time.sleep(0.02)
                job = client.get(job["href"], actor="writer").json()["data"]
            # Every attempt of both revision passes — operations first, a
            # whole document if that fails — went to the Agent that was asked
            # for. The count is not the subject; who was called is.
            self.assertEqual({"codex"}, set(asked))

    def test_quick_modify_rejects_an_unknown_agent_before_queueing(self) -> None:
        app = self._app_with_named_agents({"codex": lambda _prompt: "{}"})
        with AsgiHarness(app) as client:
            response = client.post(
                "/api/v1/workflows/workflow:draftable/modify",
                actor="writer", key="modify-unknown",
                body={"prompt": "rename it", "mode": "modify", "agent": "gpt-9"},
            )
            self.assertEqual(400, response.status_code)
            jobs = client.get(
                "/api/v1/workflow-authoring-jobs", actor="writer"
            ).json()["data"]["jobs"]
            self.assertEqual([], jobs)

    def test_a_revision_records_the_agent_the_author_chose(self) -> None:
        import json as json_module
        from tests.test_workflow_drafts import dsl as editable_dsl

        revised = editable_dsl(name="Agent revised")
        app = self._app_with_named_agents(
            {"codex": lambda _prompt: json_module.dumps(revised)}
        )
        with AsgiHarness(app) as client:
            command, _ = self._edit_command(client)
            draft = client.post(
                command["href"], actor="writer", key="agent-create", body={},
            ).json()["data"]
            revise = next(
                item for item in draft["allowed_commands"]
                if item["command"] == "workflow.draft.revise"
            )
            queued = client.post(
                revise["href"], actor="writer", key="agent-revise",
                body={
                    "instruction": "rename it", "agent": "codex",
                    "expected_version": draft["revision"],
                },
            ).json()["data"]
            self.assertEqual("codex", queued["pending_revision"]["requested_agent"])
            settled = self._settle(client, draft["draft_id"])
            self.assertEqual("pending", settled["pending_revision"]["status"])

    def test_a_revision_naming_an_unknown_agent_never_queues(self) -> None:
        app = self._app_with_named_agents({"codex": lambda _prompt: "{}"})
        with AsgiHarness(app) as client:
            command, _ = self._edit_command(client)
            draft = client.post(
                command["href"], actor="writer", key="unknown-create", body={},
            ).json()["data"]
            revise = next(
                item for item in draft["allowed_commands"]
                if item["command"] == "workflow.draft.revise"
            )
            response = client.post(
                revise["href"], actor="writer", key="unknown-revise",
                body={
                    "instruction": "rename it", "agent": "gpt-9",
                    "expected_version": draft["revision"],
                },
            )
            self.assertEqual(400, response.status_code)
            after = client.get(
                f"/api/v1/workflow-drafts/{draft['draft_id']}", actor="writer",
            ).json()["data"]
            self.assertIsNone(after["pending_revision"])

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

    def test_draft_carries_the_same_graph_the_catalog_draws(self) -> None:
        """The editor draws a draft with the published renderer, so the draft
        read model must speak the same dialect and use the same layout."""

        with AsgiHarness(self.app) as client:
            command, detail = self._edit_command(client)
            draft = client.post(
                command["href"], actor="writer", key="graph-create", body={},
            ).json()["data"]
            self.assertEqual(detail["graph"]["nodes"], draft["graph"]["nodes"])
            self.assertEqual(detail["graph"]["edges"], draft["graph"]["edges"])
            self.assertEqual(detail["graph"]["layout"], draft["graph"]["layout"])

    def test_a_proposed_revision_is_drawable_before_it_is_accepted(self) -> None:
        import json as json_module
        from tests.test_workflow_drafts import dsl as editable_dsl

        revised = editable_dsl(name="Agent revised")
        revised["nodes"][0]["id"] = "renamed"
        revised["edges"][0]["from"]["node"] = "renamed"
        revised["entry"] = ["renamed"]
        app = self._app_with_reviser(lambda _prompt: json_module.dumps(revised))
        with AsgiHarness(app) as client:
            command, _ = self._edit_command(client)
            draft = client.post(
                command["href"], actor="writer", key="graph-revise-create", body={},
            ).json()["data"]
            revise = next(
                item for item in draft["allowed_commands"]
                if item["command"] == "workflow.draft.revise"
            )
            client.post(
                revise["href"], actor="writer", key="graph-revise",
                body={
                    "instruction": "rename the first node",
                    "expected_version": revise["expected_version"],
                },
            )
            data = self._settle(client, draft["draft_id"])
            candidate = data["pending_revision"]
            self.assertEqual(
                ["done", "renamed"],
                sorted(node["node_id"] for node in candidate["graph"]["nodes"]),
            )
            self.assertEqual(
                "renamed", candidate["graph"]["edges"][0]["from"],
            )
            # The before/after pair is what the reviewer compares.
            self.assertEqual(
                data["graph"]["nodes"], candidate["previous_graph"]["nodes"],
            )

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


class PublicWorkflowLibraryTests(unittest.TestCase):
    def test_workflow_published_in_one_project_runs_from_another(self) -> None:
        import json as json_module
        from tests.test_workflow_drafts import dsl as shared_dsl

        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        shared = root / "workflows" / "library.db"
        project_a = root / "projects" / "a" / "runtime.db"
        project_b = root / "projects" / "b" / "runtime.db"
        authorizer = Authorizer(lambda _actor: [READ_SCOPE, WRITE_SCOPE])

        def app(path):
            return create_app(
                path, workflow_db_path=shared,
                handlers=[transform_registration()], schemas=SCHEMAS,
                poll_seconds=0.02,
                authenticator=lambda request: request.headers.get("x-orbit-actor"),
                authorizer=authorizer, single_goal_mode=False,
                langgraph_state_directory=Path(path).parent / "langgraph",
            )

        app_a, app_b = app(project_a), app(project_b)
        source = json_module.dumps(
            shared_dsl(workflow_id="shared-workflow", name="Shared workflow")
        )
        with AsgiHarness(app_a) as client_a:
            response = client_a.post(
                "/api/v1/workflows/workflow:shared-workflow/versions",
                actor="writer", key="publish-shared",
                body={"source": source, "expected_version": 0},
            )
            self.assertEqual(200, response.status_code, response.text)
        with AsgiHarness(app_b) as client_b:
            catalog = client_b.get("/api/v1/workflows", actor="writer").json()["data"]
            self.assertIn("workflow:shared-workflow", [
                item["workflow_id"] for item in catalog["workflows"]
            ])
            response = client_b.post(
                "/api/v1/langgraph-runs", actor="writer", key="run-shared",
                body={"workflow_id": "workflow:shared-workflow", "input": {"value": 1}},
            )
            self.assertEqual(200, response.status_code, response.text)
        # The definition is shared; the runs are not. Each project's engine
        # keeps its own run store beside its own database.
        import sqlite3

        def run_count(project: Path) -> int:
            store = project.parent / "langgraph" / "langgraph-runs.sqlite3"
            with sqlite3.connect(store) as db:
                return int(db.execute(
                    "SELECT COUNT(*) FROM langgraph_runs"
                ).fetchone()[0])

        self.assertEqual(0, run_count(project_a))
        self.assertEqual(1, run_count(project_b))
        with connect_workflow_database(shared, read_only=True) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM workflow_versions").fetchone()[0])


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
            self.assertEqual(
                {"single_goal_mode": False, "agent_fallback": None},
                data["product_mode"],
            )
            caps = data["capabilities"]
            self.assertTrue(caps["static_graph"]["available"])
            self.assertTrue(caps["human_tasks"]["available"])
            # Absent features carry their reason instead of silently missing
            # keys. Neither node kind is one the engine compiles or an author
            # may draw, so both say so rather than claiming availability.
            self.assertFalse(caps["foreach"]["available"])
            self.assertEqual(
                "not_supported_by_engine", caps["foreach"]["reason"]
            )
            self.assertFalse(caps["subflow"]["available"])
            self.assertTrue(caps["history_overlay"]["available"])
            writer = client.get("/api/v1/capabilities", actor="writer").json()["data"]
            self.assertTrue(writer["permissions"]["start_run"])
            self.assertTrue(writer["permissions"]["ops_read"])
            self.assertTrue(writer["permissions"]["ops_write"])

    def test_a_runtime_with_no_engine_makes_no_single_goal_promise(self) -> None:
        """Asked for, and still reported false: nothing would keep it.

        The report used to come from the request that turned it on rather
        than from anything that acts on it, so a composition with no engine
        at all still told clients one goal ran at a time.
        """

        app = create_app(
            self.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: self.scopes.get(actor, [])),
            single_goal_mode=True,
        )
        with AsgiHarness(app) as client:
            data = client.get(
                "/api/v1/capabilities", actor="reader"
            ).json()["data"]
            self.assertFalse(data["product_mode"]["single_goal_mode"])

    def test_capabilities_report_single_goal_product_mode(self) -> None:
        app = create_app(
            self.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: self.scopes.get(actor, [])),
            artifact_backend=self.artifact_backend,
            single_goal_mode=True,
            langgraph_state_directory=Path(self.temp.name) / "langgraph",
        )
        with AsgiHarness(app) as client:
            data = client.get(
                "/api/v1/capabilities", actor="reader"
            ).json()["data"]
            self.assertEqual(
                {"single_goal_mode": True, "agent_fallback": None},
                data["product_mode"],
            )
            catalog = client.get(
                "/api/v1/workflows", actor="writer"
            ).json()["data"]["workflows"]
            self.assertTrue(catalog)
            self.assertTrue(all(
                item["goal_readiness"] in {
                    "ready", "needs_upgrade", "needs_migration",
                }
                for item in catalog
            ))
            for item in catalog:
                if item["goal_readiness"] != "ready":
                    self.assertNotIn(
                        "run.start",
                        {command["command"] for command in item["allowed_commands"]},
                    )


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
            self.assertIn("running_runs", data["capacity"])
            # Counted by the engine that runs the work, not by a job table
            # belonging to one that no longer exists.
            self.assertIn("runs_by_status", data["engine"])
            self.assertIn("timers_by_status", data["engine"])
            self.assertEqual(0.02, data["server_config"]["poll_seconds"])

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
                "/api/v1/langgraph-runs", actor="writer", key="live-cursor-run",
                body={"workflow_id": "workflow:linear", "input": {"value": 1}},
            )
            self.assertEqual(200, started.status_code, started.text)
            changed = client.get(
                f"/api/v1/live?cursor={initial['cursor']}", actor="reader"
            ).json()["data"]
            self.assertTrue(changed["changed"])


class RunGoalTests(ApiTestCase):
    """The sentence a person typed, kept beside the run it started."""

    def start(self, client, key: str, **extra):
        return client.post(
            "/api/v1/langgraph-runs", actor="author", key=key,
            body={
                "workflow_id": "workflow:linear", "input": {"value": 1},
                **extra,
            },
        )

    def test_a_goal_survives_the_start_and_comes_back_on_the_run(self) -> None:
        with AsgiHarness(self.app) as client:
            started = self.start(client, "goal-1", goal="Summarise the quarter")
            self.assertEqual(200, started.status_code, started.text)
            run = started.json()["data"]["run"]
            self.assertEqual("Summarise the quarter", run["goal"])

            detail = client.get(
                f"/api/v1/langgraph-runs/{run['run_id']}", actor="author",
            )
            self.assertEqual("Summarise the quarter", detail.json()["data"]["goal"])
            listed = client.get("/api/v1/langgraph-runs", actor="author")
            self.assertEqual(
                ["Summarise the quarter"],
                [item["goal"] for item in listed.json()["data"]["runs"]],
            )

    def test_a_run_started_without_one_reads_as_empty_not_absent(self) -> None:
        """The field is always there, so a client never branches on its absence."""

        with AsgiHarness(self.app) as client:
            started = self.start(client, "goal-none")
            self.assertEqual("", started.json()["data"]["run"]["goal"])

    def test_one_key_with_two_goals_is_refused_at_the_command_boundary(self) -> None:
        """Over HTTP the idempotency key guards the whole body, goal included."""

        with AsgiHarness(self.app) as client:
            first = self.start(client, "goal-same", goal="Summarise the quarter")
            self.assertEqual(200, first.status_code, first.text)
            again = self.start(client, "goal-same", goal="Something else")
            self.assertEqual(409, again.status_code, again.text)

    def test_the_history_row_is_told_how_many_artifacts_a_run_produced(self) -> None:
        """The row shows a count, and it used to be absent from the projection.

        A field the client reads and the server never sends renders as zero,
        so every finished goal reported "No artifacts" whatever it had made.
        """

        with AsgiHarness(self.app) as client:
            started = self.start(client, "goal-artifacts", goal="Make something")
            run_id = started.json()["data"]["run"]["run_id"]
            listed = client.get("/api/v1/langgraph-runs", actor="author")
            row = next(
                item for item in listed.json()["data"]["runs"]
                if item["run_id"] == run_id
            )
            self.assertIn("artifact_count", row)
            self.assertEqual(0, row["artifact_count"])

    def test_history_search_reaches_the_engine(self) -> None:
        """The box collected a query the list route had no parameter for."""

        with AsgiHarness(self.app) as client:
            self.start(client, "goal-search-1", goal="Summarise the quarter")
            self.start(client, "goal-search-2", goal="Draft the release notes")
            found = client.get(
                "/api/v1/langgraph-runs?q=quarter", actor="author",
            )
            self.assertEqual(200, found.status_code, found.text)
            self.assertEqual(
                ["Summarise the quarter"],
                [item["goal"] for item in found.json()["data"]["runs"]],
            )
            # A wildcard is a character a person typed, not a pattern.
            empty = client.get("/api/v1/langgraph-runs?q=%25", actor="author")
            self.assertEqual([], empty.json()["data"]["runs"])

    def test_a_goal_too_long_to_store_is_refused_not_trimmed(self) -> None:
        """A person who pasted a page should be told, not quietly abridged."""

        with AsgiHarness(self.app) as client:
            response = self.start(client, "goal-long", goal="x" * 4001)
            self.assertEqual(409, response.status_code, response.text)
            self.assertIn("4000", response.json()["error"]["message"])


class SingleGoalApiTests(unittest.TestCase):
    """The refusal, in the shape the client is written against."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "runtime.db"
        self.app = create_app(
            self.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda _actor: [READ_SCOPE, WRITE_SCOPE]),
            single_goal_mode=True,
            langgraph_state_directory=Path(self.temp.name) / "langgraph",
        )
        publish_human_workflow(self.db)

    def start(self, client, key: str, **extra):
        return client.post(
            "/api/v1/langgraph-runs", actor="author", key=key,
            body={
                "workflow_id": "workflow:human", "input": {"value": 1}, **extra,
            },
        )

    def test_the_report_and_the_refusal_agree(self) -> None:
        """The capability said this before anything enforced it."""

        with AsgiHarness(self.app) as client:
            reported = client.get(
                "/api/v1/capabilities", actor="author",
            ).json()["data"]["product_mode"]["single_goal_mode"]
            self.assertTrue(reported)

            first = self.start(client, "goal-1", goal="Review the draft")
            self.assertEqual(200, first.status_code, first.text)
            second = self.start(client, "goal-2", goal="Something else")
            self.assertEqual(409, second.status_code, second.text)
            failure = second.json()["error"]
            self.assertEqual("active_goal_exists", failure["code"])
            # The client takes the person to this run, so it has to be there.
            active = failure["details"]["active_goal"]
            self.assertEqual(
                first.json()["data"]["run"]["run_id"], active["run_id"],
            )
            self.assertEqual("Review the draft", active["goal"])

    def test_the_report_cannot_be_told_something_the_engine_does_not_do(self) -> None:
        """There is one answer, and it comes from whoever keeps the rule.

        The API layer used to carry its own `single_goal_mode`, left over from
        an engine that enforced there. Anything could be passed into it, and
        for a long time something was: it said `true` beside an engine that
        started as many goals as it was asked to.
        """

        from starlette.applications import Starlette

        from orbit.web.api_v1 import build_api_v1
        from orbit.workflow.langgraph_runtime import build_service

        self.assertNotIn(
            "single_goal_mode",
            inspect.signature(build_api_v1).parameters,
            "the API layer can be told again",
        )

        for enforced in (False, True):
            with self.subTest(single_goal=enforced):
                root = Path(self.temp.name) / f"derived-{enforced}"
                root.mkdir()
                database = root / "runtime.db"
                publish_human_workflow(database)
                engine = build_service(
                    database, [transform_registration()],
                    state_directory=root / "langgraph", single_goal=enforced,
                )
                app = Starlette(routes=build_api_v1(
                    database, langgraph_service=engine,
                    authenticator=lambda request: "author",
                    authorizer=Authorizer(
                        lambda _actor: [READ_SCOPE, WRITE_SCOPE, OPS_READ_SCOPE]
                    ),
                ))
                with AsgiHarness(app) as client:
                    reported = client.get(
                        "/api/v1/capabilities", actor="author",
                    ).json()["data"]["product_mode"]["single_goal_mode"]
                    self.assertEqual(enforced, reported)
                    body = {
                        "workflow_id": "workflow:human", "input": {"value": 1},
                    }
                    client.post(
                        "/api/v1/langgraph-runs", actor="author", key="a",
                        body=body,
                    )
                    second = client.post(
                        "/api/v1/langgraph-runs", actor="author", key="b",
                        body=body,
                    )
                self.assertEqual(409 if enforced else 200, second.status_code)

    def test_the_slot_is_released_by_cancelling(self) -> None:
        with AsgiHarness(self.app) as client:
            first = self.start(client, "goal-1", goal="Review the draft")
            run = first.json()["data"]["run"]
            cancelled = client.post(
                f"/api/v1/langgraph-runs/{run['run_id']}/cancel",
                actor="author", key="cancel-1",
                body={"expected_version": run["revision"]},
            )
            self.assertEqual(200, cancelled.status_code, cancelled.text)
            self.assertEqual(
                200, self.start(client, "goal-2", goal="Next").status_code,
            )


class LiveMarkerTests(ApiTestCase):
    def test_the_cursor_moves_while_a_run_is_still_working(self) -> None:
        """A run row is written once at the start and once at the end.

        A marker made of those two alone cannot move between them, so a page
        watching a long run polled a frozen cursor until it was over. The
        engine's event position moves as each external step begins and ends,
        which is the granularity where the wait is long enough to need it.
        """

        from orbit.workflow.langgraph_runtime.service import append_event

        with AsgiHarness(self.app) as client:
            started = client.post(
                "/api/v1/langgraph-runs", actor="writer", key="marker-1",
                body={"workflow_id": "workflow:linear", "input": {"value": 1}},
            )
            self.assertEqual(200, started.status_code, started.text)
            run_id = started.json()["data"]["run"]["run_id"]
            before = client.get("/api/v1/live", actor="reader").json()["data"]

            # One node event and nothing else: the run row is untouched, which
            # is exactly the state a working run is in.
            engine = self.app.state.langgraph_service
            with engine._connect() as connection:
                append_event(
                    connection, run_id, "langgraph_node.started",
                    node_id="work", attempt_id="langgraph_attempt:work:1",
                )
                connection.commit()

            after = client.get(
                f"/api/v1/live?cursor={before['cursor']}", actor="reader",
            ).json()["data"]
            self.assertTrue(after["changed"], "a step finishing moved nothing")


class RunStepsApiTests(unittest.TestCase):
    """Reading where a run got to."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "runtime.db"
        self.app = create_app(
            self.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(
                lambda actor: [READ_SCOPE, WRITE_SCOPE]
                if actor == "author" else [READ_SCOPE]
            ),
            single_goal_mode=False,
            langgraph_state_directory=Path(self.temp.name) / "langgraph",
        )
        publish_human_workflow(self.db)

    def start(self, client):
        started = client.post(
            "/api/v1/langgraph-runs", actor="author", key="steps-1",
            body={"workflow_id": "workflow:human", "input": {"value": 1}},
        )
        self.assertEqual(200, started.status_code, started.text)
        return started.json()["data"]["run"]

    def test_the_steps_of_a_waiting_run_read_as_the_engine_derived_them(self) -> None:
        with AsgiHarness(self.app) as client:
            run = self.start(client)
            response = client.get(
                f"/api/v1/langgraph-runs/{run['run_id']}/steps", actor="author",
            )
            self.assertEqual(200, response.status_code, response.text)
            body = response.json()
            self.assertEqual(run["revision"], body["projection_version"])
            self.assertEqual(
                [("transform", "succeeded"), ("approve", "waiting"),
                 ("done", "not_reached")],
                [(step["node_id"], step["status"]) for step in body["data"]["steps"]],
            )

    def test_steps_are_the_ordinary_read_scope(self) -> None:
        """A step says which node ran, never what it produced.

        What a node printed is behind `/output` and what it made is an
        Artifact; both are sensitive. Where the run got to is not.
        """

        with AsgiHarness(self.app) as client:
            run = self.start(client)
            path = f"/api/v1/langgraph-runs/{run['run_id']}/steps"
            for step in client.get(path, actor="author").json()["data"]["steps"]:
                self.assertNotIn("output", step)
                self.assertNotIn("error", step)

    def test_a_run_started_by_somebody_else_is_still_this_Workspace_s(self) -> None:
        """Visibility is which Runtime you reached, not who started the Run.

        This asserted the opposite while `/mcp` let the same caller ask for the
        Workspace's work and get it — two rules over one database.
        """

        with AsgiHarness(self.app) as client:
            run = self.start(client)
            self.assertEqual(200, client.get(
                f"/api/v1/langgraph-runs/{run['run_id']}/steps", actor="reader",
            ).status_code)

    def test_an_unknown_run_and_an_unknown_parameter_are_told_apart(self) -> None:
        with AsgiHarness(self.app) as client:
            run = self.start(client)
            self.assertEqual(404, client.get(
                "/api/v1/langgraph-runs/langgraph_run:nope/steps", actor="author",
            ).status_code)
            self.assertEqual(400, client.get(
                f"/api/v1/langgraph-runs/{run['run_id']}/steps?limit=5",
                actor="author",
            ).status_code)

    def test_the_edge_report_says_which_way_a_waiting_run_went(self) -> None:
        with AsgiHarness(self.app) as client:
            run = self.start(client)
            response = client.get(
                f"/api/v1/langgraph-runs/{run['run_id']}/edges", actor="author",
            )
            self.assertEqual(200, response.status_code, response.text)
            body = response.json()
            self.assertEqual(run["revision"], body["projection_version"])
            edges = body["data"]["edges"]
            self.assertTrue(edges, "the fixture has edges to report on")
            taken = [item for item in edges if item["status"] == "taken"]
            self.assertEqual(
                [("transform", "approve")],
                [(item["source_node"], item["target_node"]) for item in taken],
            )
            # The run is interrupted at `approve`, so nothing below it has
            # decided anything yet.
            self.assertEqual(
                {"not_reached"},
                {
                    item["status"] for item in edges
                    if item["source_node"] == "approve"
                },
            )

    def test_the_edge_report_carries_no_values(self) -> None:
        """Same scope as steps, so it must be as quiet as steps."""

        with AsgiHarness(self.app) as client:
            run = self.start(client)
            for item in client.get(
                f"/api/v1/langgraph-runs/{run['run_id']}/edges", actor="author",
            ).json()["data"]["edges"]:
                self.assertNotIn("mapping", item)
                self.assertNotIn("condition", item)
                self.assertNotIn("value", item)

    def test_an_unknown_run_and_an_unknown_parameter_are_told_apart_on_edges(
        self,
    ) -> None:
        with AsgiHarness(self.app) as client:
            run = self.start(client)
            self.assertEqual(404, client.get(
                "/api/v1/langgraph-runs/langgraph_run:nope/edges", actor="author",
            ).status_code)
            # A second reader is not a second Workspace.
            self.assertEqual(200, client.get(
                f"/api/v1/langgraph-runs/{run['run_id']}/edges", actor="reader",
            ).status_code)
            self.assertEqual(400, client.get(
                f"/api/v1/langgraph-runs/{run['run_id']}/edges?limit=5",
                actor="author",
            ).status_code)


class WorkflowBranchHistoryApiTests(ApiTestCase):
    """The tally, served on the definition rather than on a run."""

    def setUp(self) -> None:
        super().setUp()
        from orbit.web.api_v1 import Authorizer, READ_SCOPE, WRITE_SCOPE
        from orbit.web.app import create_app
        from tests.test_web_composition import SCHEMAS, transform_registration

        self.db = Path(self.temp.name) / "runtime.db"
        self.app = create_app(
            self.db,
            handlers=[transform_registration()], schemas=SCHEMAS,
            poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(
                lambda actor: [READ_SCOPE, WRITE_SCOPE]
                if actor == "author" else [READ_SCOPE]
            ),
            single_goal_mode=False,
            langgraph_state_directory=Path(self.temp.name) / "langgraph",
        )
        publish_human_workflow(self.db)

    def test_it_counts_the_runs_behind_every_verdict(self) -> None:
        with AsgiHarness(self.app) as client:
            for index in range(3):
                started = client.post(
                    "/api/v1/langgraph-runs", actor="author", key=f"b{index}",
                    body={"workflow_id": "workflow:human", "input": {"value": 1}},
                )
                self.assertEqual(200, started.status_code, started.text)
            response = client.get(
                "/api/v1/workflows/workflow:human/branches", actor="author",
            )
            self.assertEqual(200, response.status_code, response.text)
            data = response.json()["data"]
            self.assertEqual(3, data["runs"])
            self.assertEqual("workflow:human", data["workflow_id"])
            for edge in data["edges"]:
                self.assertIn(
                    edge["verdict"], ("taken", "never_taken", "no_evidence"),
                )
                self.assertEqual(
                    edge["decided"],
                    edge["taken"] + edge["not_taken"] + edge["shadowed"],
                )

    def test_another_actor_counts_none_of_them(self) -> None:
        with AsgiHarness(self.app) as client:
            client.post(
                "/api/v1/langgraph-runs", actor="author", key="mine",
                body={"workflow_id": "workflow:human", "input": {"value": 1}},
            )
            data = client.get(
                "/api/v1/workflows/workflow:human/branches", actor="reader",
            ).json()["data"]
            self.assertEqual(0, data["runs"])
            self.assertEqual([], data["edges"])

    def test_an_unknown_workflow_and_a_bad_limit_are_told_apart(self) -> None:
        with AsgiHarness(self.app) as client:
            self.assertEqual(404, client.get(
                "/api/v1/workflows/workflow:nope/branches", actor="author",
            ).status_code)
            self.assertEqual(400, client.get(
                "/api/v1/workflows/workflow:human/branches?limit=0",
                actor="author",
            ).status_code)
            self.assertEqual(400, client.get(
                "/api/v1/workflows/workflow:human/branches?since=today",
                actor="author",
            ).status_code)


class HandlerConsoleApiTests(ApiTestCase):
    """Reading what a run's Handlers printed."""

    def start(self, client, key: str) -> str:
        started = client.post(
            "/api/v1/langgraph-runs", actor="author", key=key,
            body={"workflow_id": "workflow:linear", "input": {"value": 1}},
        )
        self.assertEqual(200, started.status_code, started.text)
        return started.json()["data"]["run"]["run_id"]

    def test_console_is_content_and_needs_the_scope_for_content(self) -> None:
        """A subprocess prints whatever it was working on.

        It is the same material as an Artifact's title, so it is behind the
        same scope: a reader who may see that a run happened is not thereby
        allowed to read what it said.
        """

        with AsgiHarness(self.app) as client:
            run_id = self.start(client, "console-scope")
            path = f"/api/v1/langgraph-runs/{run_id}/output"
            self.assertEqual(200, client.get(path, actor="author").status_code)
            self.assertEqual(403, client.get(path, actor="reader").status_code)

    def test_a_run_that_printed_nothing_reads_as_empty_not_missing(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id = self.start(client, "console-empty")
            response = client.get(
                f"/api/v1/langgraph-runs/{run_id}/output", actor="author",
            )
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(
                {"chunks": [], "after": 0, "has_more": False},
                response.json()["data"],
            )

    def test_an_unknown_run_and_a_bad_cursor_are_told_apart(self) -> None:
        with AsgiHarness(self.app) as client:
            run_id = self.start(client, "console-errors")
            self.assertEqual(404, client.get(
                "/api/v1/langgraph-runs/langgraph_run:nope/output", actor="author",
            ).status_code)
            self.assertEqual(400, client.get(
                f"/api/v1/langgraph-runs/{run_id}/output?after=yesterday",
                actor="author",
            ).status_code)
            self.assertEqual(400, client.get(
                f"/api/v1/langgraph-runs/{run_id}/output?tail=1", actor="author",
            ).status_code)


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


class AuthoringSchemaApiTests(ApiTestCase):
    """The authoring contract, served rather than re-declared by the editor."""

    def _schema(self, client):
        response = client.get("/api/v1/workflows/authoring-schema", actor="reader")
        self.assertEqual(200, response.status_code, response.text)
        return response.json()["data"]

    def test_the_contract_is_readable_and_names_its_dsl_version(self) -> None:
        with AsgiHarness(self.app) as client:
            data = self._schema(client)
        self.assertEqual("1.3", data["dsl_version"])
        self.assertEqual(
            ["action", "decision", "human", "join", "terminal"], data["node_kinds"]
        )

    def test_the_served_schema_is_the_one_the_compiler_binds_to(self) -> None:
        from orbit.workflow.dsl import authoring_json_schema

        with AsgiHarness(self.app) as client:
            data = self._schema(client)
        self.assertEqual(authoring_json_schema(), data["schema"])

    def test_edge_endpoints_arrive_under_their_dsl_names(self) -> None:
        """An editor drawing `source`/`target` would emit an invalid document."""

        with AsgiHarness(self.app) as client:
            edge = self._schema(client)["schema"]["$defs"]["Edge"]["properties"]
        self.assertIn("from", edge)
        self.assertIn("to", edge)
        self.assertNotIn("source", edge)

    def test_the_contract_never_offers_a_handler_fingerprint(self) -> None:
        """An editor must not be able to choose what a node binds to."""

        with AsgiHarness(self.app) as client:
            handler = self._schema(client)["schema"]["$defs"]["HandlerRef"]
        self.assertEqual(["name", "version"], sorted(handler["properties"]))

    def test_reading_the_contract_still_needs_credentials(self) -> None:
        with AsgiHarness(self.app) as client:
            self.assertEqual(
                401, client.get("/api/v1/workflows/authoring-schema").status_code
            )
            self.assertEqual(
                403,
                client.get(
                    "/api/v1/workflows/authoring-schema", actor="nobody"
                ).status_code,
            )

    def test_the_literal_path_is_not_captured_as_a_workflow_id(self) -> None:
        """Route order: /authoring-schema must not resolve as /{workflow_id}."""

        with AsgiHarness(self.app) as client:
            data = self._schema(client)
        self.assertIn("schema", data)


class WorkflowViewerMountTests(unittest.TestCase):
    """The read-only graph viewer is served only as an embedded UI asset."""

    def build(self, **extra):
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        return create_app(
            Path(temp.name) / "runtime.db",
            handlers=[transform_registration()], schemas=SCHEMAS,
            poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: [READ_SCOPE]),
            serve_ui=True,
            **extra,
        )

    def _mounted(self, **extra):
        return {
            route.name
            for route in self.build(**extra).routes
            if hasattr(route, "name")
        }

    @staticmethod
    def _built() -> bool:
        from importlib import resources

        return resources.files("orbit").joinpath(
            "static/workflow-editor/index.html"
        ).is_file()

    def test_the_viewer_is_mounted_when_it_has_been_built(self) -> None:
        mounted = self._mounted()
        self.assertEqual(self._built(), "workflow-viewer" in mounted)
        self.assertIn("ui", mounted)


    def test_the_viewer_is_static_files_and_needs_no_credentials(self) -> None:
        """Its graph arrives from the authenticated parent page by message."""

        if not self._built():
            self.skipTest("viewer bundle is not built in this checkout")
        with AsgiHarness(self.build()) as client:
            response = client.get("/viewer/")
        self.assertEqual(200, response.status_code)
        self.assertIn("<div id=\"root\">", response.text)

    def test_the_editor_page_is_not_served(self) -> None:
        with AsgiHarness(self.build()) as client:
            response = client.get("/editor/")
        self.assertEqual(404, response.status_code)


class WorkflowCatalogSurfaceTests(ApiTestCase):
    """Every authoring route is mounted, unconditionally.

    There were two authoring products and a flag choosing between them, and
    this class existed to prove the flag never withheld an endpoint. The flag
    is gone; what it was defending is worth keeping — the catalog and the
    authoring surface belong to the Runtime, not to a product.
    """

    def build(self):
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        return create_app(
            Path(temp.name) / "runtime.db",
            handlers=[transform_registration()], schemas=SCHEMAS,
            poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: self.scopes.get(actor, [])),
        )

    def test_the_catalog_is_readable(self) -> None:
        with AsgiHarness(self.build()) as client:
            response = client.get("/api/v1/workflows", actor="reader")
        self.assertEqual(200, response.status_code, response.text)
        self.assertIn("workflows", response.json()["data"])

    def test_the_authoring_contract_is_served(self) -> None:
        """The editor fetches this before it draws anything."""

        with AsgiHarness(self.build()) as client:
            response = client.get(
                "/api/v1/workflows/authoring-schema", actor="reader"
            )
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("1.3", response.json()["data"]["dsl_version"])

    def test_the_authoring_routes_are_mounted(self) -> None:
        # Compared on the mounted route names: what is being asked is which
        # endpoints exist, and a status code cannot tell "not mounted" from
        # "mounted and refused".
        names = {
            route.name for route in self.build().routes
            if getattr(route, "name", None)
        }
        self.assertLessEqual(
            {
                "workflow_catalog", "workflow_detail", "workflow_validate",
                "workflow_publish", "workflow_authoring_schema",
                "workflow_generate", "workflow_modify", "workflow_delete",
                "workflow_draft_create", "authoring_job_list",
            },
            names,
        )


class WorkspaceScopedReadsTests(ApiTestCase):
    """A Runtime serves one Workspace, and that is the whole of visibility.

    It used to be two rules at once: `/mcp` let a caller ask for the
    Workspace's work and the panel did, while `/api/v1` had no way to ask and
    every loopback caller is `local`. One database, and Orbit's own UI showing
    the Runs it had started with no sign that the others existed.

    An actor is still recorded on everything and still scopes every write —
    who cancelled a Run is the point of recording who cancelled it. What it no
    longer does is decide who may look.
    """

    def _started_by(self, client, actor, key):
        return client.post(
            "/api/v1/langgraph-runs", actor=actor, key=key,
            body={"workflow_id": "workflow:linear", "input": {"value": 0}},
        ).json()["data"]["run"]

    def test_a_read_sees_the_Workspace_whoever_is_asking(self) -> None:
        with AsgiHarness(self.app) as client:
            started = self._started_by(client, "writer", "theirs")
            listed = client.get("/api/v1/langgraph-runs", actor="reader").json()["data"]["runs"]
            self.assertEqual([started["run_id"]], [run["run_id"] for run in listed])
            # And what is listed can be opened, or the list is links to 404s.
            for path in ("", "/steps", "/edges", "/graph"):
                answered = client.get(
                    f"/api/v1/langgraph-runs/{started['run_id']}{path}", actor="reader",
                )
                self.assertEqual(200, answered.status_code, path)

    def test_asking_for_an_owner_is_asking_for_what_already_happens(self) -> None:
        """The parameter is gone rather than accepted and ignored.

        A query string that quietly changes nothing is worse than one that is
        refused: it reads as a control somebody is relying on.
        """

        with AsgiHarness(self.app) as client:
            answered = client.get("/api/v1/langgraph-runs?owner=workspace", actor="reader")
        self.assertEqual(400, answered.status_code)
        self.assertIn("unknown query parameter", answered.json()["error"]["message"])

    def test_a_write_is_still_the_record_of_who_acted(self) -> None:
        with AsgiHarness(self.app) as client:
            started = self._started_by(client, "writer", "not-yours")
            refused = client.post(
                f"/api/v1/langgraph-runs/{started['run_id']}/cancel",
                actor="reader", key="steal",
                body={"expected_version": started["revision"]},
            )
        self.assertIn(refused.status_code, (400, 403, 404))
