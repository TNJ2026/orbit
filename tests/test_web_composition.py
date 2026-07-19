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

            async def receive():
                return {"type": "http.request", "body": raw, "more_body": False}

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
            body = b"".join(
                m.get("body", b"") for m in messages if m["type"] == "http.response.body"
            )
            return SimpleNamespace(
                status_code=status, text=body.decode(),
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
    nodes = tuple(
        IRNode(node_id, "action", (port,), (port,), ref, {}, (), None)
        for node_id in node_ids
    ) + (IRNode("done", "terminal", (port,), (), None, {}, (), None),)
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


def publish_linear_workflow(db_path: Path) -> tuple[str, object]:
    ir = linear_ir_for(transform_registration().manifest)
    digest = definition_hash(ir)
    SQLiteWorkflowVersionStore(db_path).publish(
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
        self.temp = tempfile.TemporaryDirectory()
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
                with tempfile.TemporaryDirectory() as temp:
                    path = Path(temp) / "runtime.db"
                    connection = sqlite3.connect(path)
                    connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
                    connection.commit()
                    connection.close()
                    with self.assertRaises(MixedSchemaError):
                        assert_runtime_schema(path)


class CompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
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

    def test_each_worker_gets_its_own_runtime_object(self) -> None:
        composition = RuntimeComposition(
            self.db, handlers=[transform_registration()], schemas=SCHEMAS,
            worker_count=3,
        )
        composition.start()
        try:
            names = [loop.name for loop in composition.loops]
            self.assertEqual(
                ["worker-1", "worker-2", "worker-3", "timer-1", "recovery"], names
            )
            self.assertEqual(3, len(composition._workers))
            self.assertEqual(3, len({id(w) for w in composition._workers}))
        finally:
            self.assertEqual([], composition.stop())


class HealthEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "runtime.db"
        self.app = create_app(
            self.db, handlers=[transform_registration()], schemas=SCHEMAS,
            worker_count=1, poll_seconds=0.05,
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
            self.assertEqual(list(range(1, 10)), checks["migrations"]["applied"])
            self.assertTrue(checks["handlers"]["sealed"])
            self.assertTrue(checks["components"]["ok"])

    def test_ready_is_503_when_a_component_is_down(self) -> None:
        with AsgiHarness(self.app) as client:
            composition = self.app.state.runtime
            composition.loops[0].stop()
            response = client.get("/health/ready")
            self.assertEqual(503, response.status_code)
            self.assertEqual("not_ready", response.json()["status"])


class EndToEndTests(unittest.TestCase):
    """Gate M2: a static workflow completes through the composed runtime."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "runtime.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _compose(self) -> RuntimeComposition:
        composition = RuntimeComposition(
            self.db, handlers=[transform_registration()], schemas=SCHEMAS,
            worker_count=2, poll_seconds=0.02, clock=lambda: datetime.now(timezone.utc),
        )
        return composition

    def _await_status(self, composition, run_id, statuses, timeout=30.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            run = composition.service.get_run(run_id)
            if run is not None:
                last = run.status
                if run.status in statuses:
                    return run
            time.sleep(0.05)
        self.fail(f"run did not reach {statuses}; last status was {last}")

    def test_static_workflow_runs_to_completion(self) -> None:
        composition = self._compose()
        _, digest = publish_linear_workflow(self.db)
        run_id = EntityId("run", "m2-e2e")

        composition.start()
        try:
            result = composition.service.submit(start_run_command(run_id, digest))
            self.assertEqual("applied", result.disposition.value)
            run = self._await_status(
                composition, run_id,
                {WorkflowRunStatus.SUCCEEDED, WorkflowRunStatus.FAILED},
            )
            self.assertIs(WorkflowRunStatus.SUCCEEDED, run.status)
        finally:
            self.assertEqual([], composition.stop())

    def test_unfinished_run_resumes_after_restart(self) -> None:
        _, digest = publish_linear_workflow(self.db)
        run_id = EntityId("run", "m2-restart")

        # First process: start the run, then shut everything down immediately so
        # the workflow is left mid-flight.
        first = self._compose()
        first.service.submit(start_run_command(run_id, digest))
        first.start()
        time.sleep(0.05)
        self.assertEqual([], first.stop())

        interrupted = first.service.get_run(run_id)
        self.assertIsNotNone(interrupted)

        # Second process over the same database: the surviving job/lease/timer
        # state must be enough to carry the run to completion.
        second = self._compose()
        second.start()
        try:
            run = self._await_status(
                second, run_id,
                {WorkflowRunStatus.SUCCEEDED, WorkflowRunStatus.FAILED},
            )
            self.assertIs(WorkflowRunStatus.SUCCEEDED, run.status)
        finally:
            self.assertEqual([], second.stop())

    def test_shutdown_stops_every_loop(self) -> None:
        composition = self._compose()
        composition.start()
        self.assertTrue(all(loop.alive for loop in composition.loops))
        self.assertEqual([], composition.stop())
        self.assertTrue(all(not loop.alive for loop in composition.loops))

    def test_lifespan_starts_and_stops_components(self) -> None:
        app = create_app(
            self.db, handlers=[transform_registration()], schemas=SCHEMAS,
            worker_count=1, poll_seconds=0.02,
        )
        composition = app.state.runtime
        with AsgiHarness(app):
            self.assertTrue(all(loop.alive for loop in composition.loops))
        self.assertTrue(all(not loop.alive for loop in composition.loops))
        self.assertFalse(hasattr(app.state, "shutdown_stragglers"))


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
