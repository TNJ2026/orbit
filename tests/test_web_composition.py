"""M2: the production composition root.

Gate M2 in the migration plan:
  * `orbit serve` creates a fresh database and starts;
  * a database carrying legacy tables is refused;
  * a static workflow runs StartRun -> Job -> Handler -> CompleteRun;
  * jobs, leases, timers and unfinished runs survive a restart;
  * shutting down leaves no running handler subprocess.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from orbit.web.app import RuntimeComposition, HandlerRegistration, create_app
from orbit.web.schema_guard import (
    LEGACY_TABLES, MixedSchemaError, assert_runtime_schema, table_names,
)
from orbit.workflow.catalogs import (
    HandlerManifest, InMemoryHandlerCatalog, InMemorySchemaCatalog,
)
from orbit.workflow.domain.definitions import CompiledWorkflow
from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.envelopes import CommandEnvelope
from orbit.workflow.domain.handlers import ResourceProfile
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.serialization import definition_hash
from orbit.workflow.domain.states import WorkflowRunStatus
from orbit.workflow.domain.versions import AggregateVersion
from orbit.workflow.handlers import TransformHandler
from orbit.workflow.domain.definitions import (
    IREdge, IRHandlerRef, IRNode, IRPort, WorkflowIR,
)
from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
from orbit.workflow.dsl import compile_source


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


class AsgiHarness:
    """Drive lifespan and HTTP without pulling in an HTTP client dependency."""

    def __init__(self, app) -> None:
        self.app = app
        self._loop = asyncio.new_event_loop()
        self._receive: asyncio.Queue | None = None
        self._task = None

    def __enter__(self) -> "AsgiHarness":
        async def boot():
            self._receive = asyncio.Queue()
            self._sent = asyncio.Queue()

            async def receive():
                return await self._receive.get()

            async def send(message):
                await self._sent.put(message)

            self._task = asyncio.ensure_future(
                self.app({"type": "lifespan"}, receive, send)
            )
            await self._receive.put({"type": "lifespan.startup"})
            message = await asyncio.wait_for(self._sent.get(), timeout=10)
            assert message["type"] == "lifespan.startup.complete", message

        self._loop.run_until_complete(boot())
        return self

    def __exit__(self, *exc) -> None:
        async def shutdown():
            await self._receive.put({"type": "lifespan.shutdown"})
            message = await asyncio.wait_for(self._sent.get(), timeout=30)
            assert message["type"] == "lifespan.shutdown.complete", message
            await asyncio.wait_for(self._task, timeout=10)

        try:
            self._loop.run_until_complete(shutdown())
        finally:
            self._loop.close()

    def get(self, path: str, *, actor: str | None = None):
        return self.request("GET", path, actor=actor)

    def post(self, path: str, *, actor=None, key=None, body=None):
        """POST with the two headers every write on /api/v1 requires."""

        headers = {} if key is None else {"idempotency-key": key}
        return self.request("POST", path, actor=actor, headers=headers, body=body)

    def request(self, method: str, path: str, *, actor=None, headers=None, body=None):
        raw = b"" if body is None else json.dumps(body).encode()
        header_map = dict(headers or {})
        if actor is not None:
            header_map["x-orbit-actor"] = actor
        if body is not None:
            header_map["content-type"] = "application/json"
            header_map["content-length"] = str(len(raw))
        target, _, query = path.partition("?")

        async def call():
            messages = []
            delivered = False

            async def receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": raw, "more_body": False}
                # StreamingResponse cancels its disconnect listener when the
                # body finishes. Blocking here models a connected client and
                # avoids starving the body task with repeated request frames.
                await asyncio.Event().wait()

            async def send(message):
                messages.append(message)

            await self.app(
                {
                    "type": "http", "http_version": "1.1", "method": method,
                    "path": target, "raw_path": target.encode(),
                    "query_string": query.encode(),
                    "headers": [
                        (name.lower().encode(), str(value).encode())
                        for name, value in header_map.items()
                    ],
                    "client": ("127.0.0.1", 12345),
                    "server": ("127.0.0.1", 8848), "scheme": "http",
                },
                receive, send,
            )
            status = next(m["status"] for m in messages if m["type"] == "http.response.start")
            start = next(m for m in messages if m["type"] == "http.response.start")
            response_headers = {
                key.decode().lower(): value.decode()
                for key, value in start.get("headers", [])
            }
            body = b"".join(
                m.get("body", b"") for m in messages if m["type"] == "http.response.body"
            )
            return SimpleNamespace(
                status_code=status,
                # Not every response is text: Artifact content is served as
                # the bytes it was stored as, image or otherwise.
                text=body.decode(errors="replace"), content=body,
                headers=response_headers,
                json=lambda: json.loads(body.decode()),
            )

        return self._loop.run_until_complete(call())

SCHEMAS = {
    "schema://object/1.0": {"type": "object"},
    "example://integer/1.0": {"type": "integer"},
}


def transform_registration() -> HandlerRegistration:
    """The built-in deterministic handler the E2E workflow runs on."""

    manifest = HandlerManifest(
        "transform", "1.0.0", ("action",),
        {"value": "example://integer/1.0"},
        {"value": "example://integer/1.0"},
        {"type": "object"},
        ExecutionSafety.REPLAY_SAFE,
        ResourceProfile(100, 100, 5, 60, 1_000_000, "test"),
        "schema://object/1.0", (), (), True, True,
    )
    return HandlerRegistration(manifest, TransformHandler(), "transform@1.0.0")


def linear_ir_for(manifest) -> WorkflowIR:
    """A three-node chain bound to a handler that is actually registered.

    The shared `tests.test_workflow_runtime.linear_ir` fixture uses a
    placeholder manifest fingerprint, which is fine for tests that do not wire
    an execution registry. The composition root deliberately does wire one, so
    the workflow it runs has to name a handler the sealed registry can resolve.
    """

    port = IRPort("value", "example://integer/1.0", True, False, None, "")
    ref = IRHandlerRef(manifest.name, manifest.version, manifest.fingerprint)
    node_ids = ("collect", "transform", "publish")
    # Labels are what a reader sees; the ids stay internal on purpose so tests
    # can tell the two apart.
    labels = {
        "collect": "Collect the data", "transform": "Tidy it up",
        "publish": "Write the report",
    }
    nodes = tuple(
        IRNode(
            node_id, "action", (port,), (port,), ref, {}, (), None,
            label=labels[node_id],
        )
        for node_id in node_ids
    ) + (
        IRNode(
            "done", "terminal", (port,), (), None, {}, (), None, label="Finish",
        ),
    )
    chain = (*node_ids, "done")
    edges = tuple(
        IREdge(
            f"{source}_{target}", source, "value", target, "value", "success",
            {"op": "literal", "value": True},
            {"op": "identity", "schema_id": "example://integer/1.0"},
        )
        for source, target in zip(chain, chain[1:])
    )
    return WorkflowIR(
        "1.1", "workflow:linear", "Linear", "", {}, (), (), nodes, edges,
        ("collect",), ("done",), (), (), {},
    )


def publish_linear_workflow(
    db_path: Path,
    *,
    clock=None,
) -> tuple[str, object]:
    ir = linear_ir_for(transform_registration().manifest)
    digest = definition_hash(ir)
    SQLiteWorkflowVersionStore(db_path, clock=clock).publish(
        CompiledWorkflow(ir, digest, "1.0", "sha256:" + "e" * 64),
        expected_latest_version=0, source_format="json", source_text=None,
        actor="m2-test",
    )
    return "workflow:linear", digest


def publish_human_workflow(db_path: Path) -> tuple[str, object]:
    """Published action -> HumanTask -> terminal workflow used by M7."""

    dsl = {
        "dsl_version": "1.2",
        "metadata": {"id": "human", "name": "Human approval"},
        "nodes": [
            {
                "id": "transform", "kind": "action",
                "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
                "outputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
                "handler": {"name": "transform", "version": "1.0.0"},
            },
            {
                "id": "approve", "kind": "human",
                "inputs": [{"id": "value", "schema_id": "example://integer/1.0"}],
                "outputs": [{"id": "result", "schema_id": "schema://object/1.0"}],
                "config": {
                    "task_kind": "approval", "participants": ["local"],
                    "quorum": "any",
                },
            },
            {
                "id": "done", "kind": "terminal",
                "inputs": [{"id": "result", "schema_id": "schema://object/1.0"}],
            },
        ],
        "edges": [
            {
                "id": "transformed", "from": {"node": "transform", "port": "value"},
                "to": {"node": "approve", "port": "value"},
            },
            {
                "id": "approved", "from": {"node": "approve", "port": "result"},
                "to": {"node": "done", "port": "result"},
            },
        ],
        "entry": ["transform"], "terminals": ["done"],
    }
    registration = transform_registration()
    compiled = compile_source(
        json.dumps(dsl), InMemoryHandlerCatalog([registration.manifest]),
        InMemorySchemaCatalog(SCHEMAS), source_format="json",
    )
    SQLiteWorkflowVersionStore(db_path).publish(
        compiled, expected_latest_version=0, source_format="json",
        source_text=json.dumps(dsl), actor="m7-test",
    )
    return "workflow:human", compiled.definition_hash


def start_run_command(run_id: EntityId, digest) -> CommandEnvelope:
    return CommandEnvelope(
        EntityId("command", f"start-{run_id.value}"), "start_run", run_id, run_id,
        AggregateVersion(0), f"start-{run_id.value}", "m2-test", NOW,
        {
            "workflow_id": "workflow:linear", "workflow_version": 1,
            "definition_hash": digest.value, "input": {"value": 0},
        },
    )


class SchemaGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self.temp.name) / "runtime.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_missing_file_has_no_tables(self) -> None:
        self.assertEqual(frozenset(), table_names(self.db))

    def test_fresh_composition_creates_only_runtime_tables(self) -> None:
        composition = RuntimeComposition(self.db, schemas=SCHEMAS)
        self.assertTrue(composition.tables)
        self.assertEqual(frozenset(), composition.tables & LEGACY_TABLES)

    def test_mixed_schema_is_refused(self) -> None:
        # A development-era file: the M1A rename means a database can be called
        # runtime.db and still have been written by the legacy engine.
        RuntimeComposition(self.db, schemas=SCHEMAS)
        connection = sqlite3.connect(self.db)
        connection.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY)")
        connection.commit()
        connection.close()

        with self.assertRaises(MixedSchemaError) as caught:
            assert_runtime_schema(self.db)
        self.assertIn("tasks", str(caught.exception))
        # The message must send the operator to a clean start, not an import.
        self.assertIn("Delete it", str(caught.exception))

        with self.assertRaises(MixedSchemaError):
            RuntimeComposition(self.db, schemas=SCHEMAS)

    def test_every_legacy_table_is_detected(self) -> None:
        for table in sorted(LEGACY_TABLES):
            with self.subTest(table=table):
                with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
                    path = Path(temp) / "runtime.db"
                    connection = sqlite3.connect(path)
                    connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
                    connection.commit()
                    connection.close()
                    with self.assertRaises(MixedSchemaError):
                        assert_runtime_schema(path)


class CompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self.temp.name) / "runtime.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_registry_is_sealed_before_workers_can_run(self) -> None:
        composition = RuntimeComposition(
            self.db, handlers=[transform_registration()], schemas=SCHEMAS
        )
        self.assertTrue(composition.handler_registry.sealed)
        self.assertEqual(1, len(composition.handler_summary.handlers))

    def test_missing_schema_fails_preflight_rather_than_at_runtime(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            RuntimeComposition(self.db, handlers=[transform_registration()], schemas={})
        self.assertIn("preflight", str(caught.exception))

    def test_only_the_loops_the_runtime_actually_needs_are_started(self) -> None:
        """A Runtime with nothing to drive in the background starts nothing.

        This used to assert a fixed pool of workers, a timer and three
        reconcilers — the components of the execution engine that was
        deleted. What is left is driven by what is wired: a LangGraph service
        brings its timer loop, a reviser brings the revision loops, and a
        composition with neither runs no threads at all.
        """

        composition = RuntimeComposition(
            self.db, handlers=[transform_registration()], schemas=SCHEMAS,
        )
        composition.start()
        try:
            self.assertEqual([], [loop.name for loop in composition.loops])
        finally:
            self.assertEqual([], composition.stop())

    def test_a_langgraph_service_brings_its_timer_loop(self) -> None:
        composition = RuntimeComposition(
            self.db, handlers=[transform_registration()], schemas=SCHEMAS,
            langgraph_service=SimpleNamespace(recover_due=lambda limit: ()),
        )
        composition.start()
        try:
            self.assertEqual(
                ["langgraph-timer"], [loop.name for loop in composition.loops]
            )
        finally:
            self.assertEqual([], composition.stop())


class HealthEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self.temp.name) / "runtime.db"
        self.app = create_app(
            self.db, handlers=[transform_registration()], schemas=SCHEMAS,
            poll_seconds=0.05,
            langgraph_state_directory=Path(self.temp.name) / "langgraph",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_live_is_up_before_components_start(self) -> None:
        with AsgiHarness(self.app) as client:
            response = client.get("/health/live")
            self.assertEqual(200, response.status_code)
            self.assertEqual("live", response.json()["status"])

    def test_ready_reports_database_migrations_handlers_and_components(self) -> None:
        with AsgiHarness(self.app) as client:
            response = client.get("/health/ready")
            self.assertEqual(200, response.status_code, response.text)
            checks = response.json()["checks"]
            self.assertTrue(checks["database"]["ok"])
            self.assertTrue(checks["migrations"]["ok"])
            self.assertEqual(list(range(1, 25)), checks["migrations"]["applied"])
            self.assertTrue(checks["handlers"]["sealed"])
            self.assertTrue(checks["components"]["ok"])

    def test_ready_is_503_when_a_component_is_down(self) -> None:
        with AsgiHarness(self.app) as client:
            composition = self.app.state.runtime
            composition.loops[0].stop()
            response = client.get("/health/ready")
            self.assertEqual(503, response.status_code)
            self.assertEqual("not_ready", response.json()["status"])


class BackgroundLoopTests(unittest.TestCase):
    def test_loop_records_errors_without_dying(self) -> None:
        from orbit.web.app import BackgroundLoop

        calls: list[int] = []

        def step() -> bool:
            calls.append(1)
            raise RuntimeError("boom")

        loop = BackgroundLoop("failing", step, poll_seconds=0.01)
        loop.start()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and loop.error_count < 2:
                time.sleep(0.01)
            self.assertGreaterEqual(loop.error_count, 2)
            self.assertIn("boom", loop.last_error or "")
            self.assertTrue(loop.alive, "an error must not kill the loop")
        finally:
            self.assertTrue(loop.stop())

    def test_busy_loop_does_not_sleep_between_items(self) -> None:
        from orbit.web.app import BackgroundLoop

        remaining = [5]

        def step() -> bool:
            if remaining[0] > 0:
                remaining[0] -= 1
                return True
            return False

        # A one second poll interval would take five seconds if the loop idled
        # after every successful item.
        loop = BackgroundLoop("busy", step, poll_seconds=1.0)
        started = time.monotonic()
        loop.start()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and remaining[0] > 0:
                time.sleep(0.01)
            self.assertEqual(0, remaining[0])
            self.assertLess(time.monotonic() - started, 2.0)
        finally:
            loop.stop()


class BoundaryTests(unittest.TestCase):
    def test_composition_root_does_not_import_the_legacy_engine(self) -> None:
        import ast
        from orbit.web import app as app_module

        tree = ast.parse(Path(app_module.__file__).read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if any(part in {"server", "store"} for part in name.split(".")):
                    offenders.append(f"{node.lineno}:{name}")
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
