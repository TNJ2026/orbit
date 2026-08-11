from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import interrupt

from orbit.workflow.catalogs import (
    HandlerManifest,
    InMemoryHandlerCatalog,
    InMemorySchemaCatalog,
)
from orbit.workflow.domain.definitions import (
    CompiledWorkflow,
    IREdge,
    IRHandlerRef,
    IRNode,
    IRPort,
    IRResult,
    WorkflowIR,
)
from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.data import PortDataPolicy, PortTransport
from orbit.workflow.domain.handlers import ResourceProfile, UnknownExternalResultError
from orbit.workflow.domain.serialization import definition_hash, to_primitive
from orbit.workflow.langgraph_runtime import (
    BoundHandler,
    HandlerBindingError,
    HandlerOutcome,
    LangGraphHandlerRegistry,
    LangGraphExecutionContext,
    LangGraphRunConflict,
    LangGraphWorkflowService,
    build_service,
    compile_generated_workflow,
    compile_workflow,
)
from orbit.workflow.handlers.agent import AgentHandler, AgentResponse, FakeAgentClient
from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
from orbit.web.api_v1 import OPS_WRITE_SCOPE, READ_SCOPE, WRITE_SCOPE, Authorizer
from orbit.web.app import HandlerRegistration, create_app
from orbit.web.builtin_handlers import BUILTIN_SCHEMAS, builtin_handlers
from orbit.workflow.langgraph_runtime.wiring import trusted_handlers
from orbit.workflow.langgraph_runtime.artifacts import (
    LangGraphArtifactAccessDenied,
    LangGraphArtifactStore,
)
from tests.test_web_composition import AsgiHarness


FINGERPRINT = "sha256:" + "a" * 64
SCHEMA = "example://value/1.0"
TRUE = {"op": "literal", "value": True}
IDENTITY = {"op": "identity"}


def port(name: str) -> IRPort:
    return IRPort(name, SCHEMA, True, False, None, "")


def artifact_port(name: str) -> IRPort:
    return IRPort(
        name,
        SCHEMA,
        True,
        False,
        None,
        "",
        PortDataPolicy(PortTransport.ARTIFACT_REF),
    )


def node(
    node_id: str,
    *,
    inputs=(),
    outputs=(),
    kind="action",
    route_mode=None,
    handler=True,
) -> IRNode:
    reference = (
        IRHandlerRef(node_id, "1.0.0", FINGERPRINT) if handler else None
    )
    return IRNode(
        node_id,
        kind,
        tuple(port(item) for item in inputs),
        tuple(port(item) for item in outputs),
        reference,
        {},
        (),
        None,
        route_mode,
    )


def edge(
    edge_id: str,
    source: str,
    target: str,
    *,
    source_port="value",
    target_port="value",
    condition=TRUE,
    mapping=IDENTITY,
    priority=0,
    back_edge=False,
    route="success",
) -> IREdge:
    return IREdge(
        edge_id,
        source,
        source_port,
        target,
        target_port,
        route,
        condition,
        mapping,
        priority,
        back_edge,
    )


def workflow(nodes, edges, *, entry, terminals, result) -> WorkflowIR:
    return WorkflowIR(
        "1.3",
        "workflow:generated",
        "Agent generated",
        "",
        {},
        (port("value"),),
        (),
        tuple(nodes),
        tuple(edges),
        tuple(entry),
        tuple(terminals),
        (),
        (),
        {},
        IRResult(*result),
    )


def binding(name, invoke) -> BoundHandler:
    return BoundHandler(name, "1.0.0", FINGERPRINT, invoke)


class LangGraphWorkflowCompilerValidationTests(unittest.TestCase):
    def test_non_inline_port_transport_is_rejected_fail_closed(self) -> None:
        artifact = artifact_port("payload")
        action = IRNode(
            "action",
            "action",
            (artifact,),
            (port("value"),),
            IRHandlerRef("action", "1.0.0", FINGERPRINT),
            {},
            (),
            None,
        )
        ir = WorkflowIR(
            "1.3",
            "workflow:artifact",
            "Artifact workflow",
            "",
            {},
            (artifact,),
            (),
            (action,),
            (),
            ("action",),
            ("action",),
            (),
            (),
            {},
            IRResult("action", "value"),
        )

        with self.assertRaisesRegex(
            ValueError,
            "node 'action' input port 'payload' uses 'artifact_ref'",
        ):
            compile_workflow(
                ir,
                LangGraphHandlerRegistry([
                    binding("action", lambda values, config, context: values)
                ]),
            )

    def test_explicit_error_outcome_selects_error_edge(self) -> None:
        action = node(
            "action", inputs=("value",), outputs=("value",), route_mode="exclusive"
        )
        succeeded = node(
            "succeeded", inputs=("value",), kind="terminal", handler=False
        )
        failed = node(
            "failed", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (action, succeeded, failed),
            (
                edge("ok", "action", "succeeded"),
                edge("failed", "action", "failed", route="error"),
            ),
            entry=("action",),
            terminals=("succeeded", "failed"),
            result=("action", "value"),
        )
        registry = LangGraphHandlerRegistry([
            binding("action", lambda values, config, context: HandlerOutcome(
                {"value": "rejected:" + values["value"]}, route="error"
            ))
        ])

        result = compile_workflow(ir, registry).invoke({"value": "x"})

        self.assertEqual("error", result["node_routes"]["action"])
        self.assertEqual(["action", "failed"], result["execution_order"])
        self.assertEqual("rejected:x", result["result"])


class LangGraphArtifactStoreTests(unittest.TestCase):
    def test_stage_commit_and_scoped_read(self) -> None:
        output = artifact_port("document")
        with tempfile.TemporaryDirectory() as directory:
            store = LangGraphArtifactStore(
                Path(directory) / "runs.sqlite3", Path(directory) / "blobs",
            )
            producer = store.access(
                run_id="langgraph_run:one", node_id="produce",
                attempt_id="langgraph_attempt:one", output_ports=(output,), inputs={},
            )
            artifact_id = producer.write(
                name="document", content=b"hello",
                content_type="application/octet-stream",
            )
            with self.assertRaises(LangGraphArtifactAccessDenied):
                store.access(
                    run_id="langgraph_run:one", node_id="consume",
                    attempt_id="langgraph_attempt:two", output_ports=(),
                    inputs={"document": {"artifact_id": artifact_id}},
                ).read(artifact_id)

            store.commit(producer.produced_artifact_ids)
            content = store.access(
                run_id="langgraph_run:one", node_id="consume",
                attempt_id="langgraph_attempt:two", output_ports=(),
                inputs={"document": {"artifact_id": artifact_id}},
            ).read(artifact_id)

        self.assertEqual(b"hello", content)

    def test_size_content_type_and_run_scope_are_enforced(self) -> None:
        output = IRPort(
            "document", SCHEMA, True, False, None, "",
            PortDataPolicy(
                PortTransport.ARTIFACT_REF,
                max_size_bytes=4,
                content_types=("text/plain",),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            store = LangGraphArtifactStore(
                Path(directory) / "runs.sqlite3", Path(directory) / "blobs",
            )
            access = store.access(
                run_id="langgraph_run:one", node_id="produce",
                attempt_id="langgraph_attempt:one", output_ports=(output,), inputs={},
            )
            with self.assertRaises(LangGraphArtifactAccessDenied):
                access.write(
                    name="document", content=b"ok",
                    content_type="application/octet-stream",
                )
            with self.assertRaisesRegex(ValueError, "size limit"):
                access.write(
                    name="document", content=b"large", content_type="text/plain",
                )
            artifact_id = access.write(
                name="document", content=b"okay", content_type="text/plain",
            )
            store.commit(access.produced_artifact_ids)
            foreign = store.access(
                run_id="langgraph_run:other", node_id="consume",
                attempt_id="langgraph_attempt:other", output_ports=(),
                inputs={"document": {"artifact_id": artifact_id}},
            )
            with self.assertRaises(LangGraphArtifactAccessDenied):
                foreign.read(artifact_id)


class LangGraphWorkflowCompilerTests(unittest.TestCase):
    def test_checkpointed_interrupt_resumes_with_same_thread(self) -> None:
        action = node("action", inputs=("value",), outputs=("value",))
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (action, terminal),
            (edge("done", "action", "terminal"),),
            entry=("action",),
            terminals=("terminal",),
            result=("action", "value"),
        )

        def wait_for_approval(values, config, context):
            approved = interrupt({"value": values["value"]})
            return {"value": values["value"] if approved else "rejected"}

        compiled = compile_workflow(
            ir,
            LangGraphHandlerRegistry([binding("action", wait_for_approval)]),
            checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": "test-interrupt"}}

        paused = compiled.invoke({"value": "payload"}, config=config)
        resumed = compiled.resume(True, config=config)

        self.assertIsNone(paused["result"])
        self.assertEqual("payload", resumed["result"])

    def test_sqlite_checkpoint_resumes_after_runtime_is_rebuilt(self) -> None:
        action = node("action", inputs=("value",), outputs=("value",))
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (action, terminal),
            (edge("done", "action", "terminal"),),
            entry=("action",),
            terminals=("terminal",),
            result=("action", "value"),
        )

        def wait_for_value(values, config, context):
            suffix = interrupt("suffix")
            return {"value": values["value"] + suffix}

        registry = LangGraphHandlerRegistry([binding("action", wait_for_value)])
        config = {"configurable": {"thread_id": "durable-interrupt"}}
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "checkpoints.sqlite3")
            with SqliteSaver.from_conn_string(database) as saver:
                first = compile_workflow(ir, registry, checkpointer=saver)
                self.assertIsNone(first.invoke({"value": "saved"}, config=config)["result"])
            with SqliteSaver.from_conn_string(database) as saver:
                rebuilt = compile_workflow(ir, registry, checkpointer=saver)
                result = rebuilt.resume("-resumed", config=config)

        self.assertEqual("saved-resumed", result["result"])

    def test_stream_exposes_native_node_updates(self) -> None:
        action = node("action", inputs=("value",), outputs=("value",))
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (action, terminal),
            (edge("done", "action", "terminal"),),
            entry=("action",),
            terminals=("terminal",),
            result=("action", "value"),
        )
        compiled = compile_workflow(
            ir,
            LangGraphHandlerRegistry([
                binding("action", lambda values, config, context: {
                    "value": values["value"] + 1
                })
            ]),
        )

        chunks = list(compiled.stream({"value": 1}))

        self.assertIn("action", chunks[0])
        self.assertIn("terminal", chunks[-1])

    def test_agent_dsl_is_validated_before_langgraph_compilation(self) -> None:
        schema_catalog = InMemorySchemaCatalog(
            {SCHEMA: {"type": "integer"}}
        )
        manifest = HandlerManifest(
            name="increment",
            version="1.0.0",
            node_kinds=("action",),
            inputs={"value": SCHEMA},
            outputs={"value": SCHEMA},
            config_schema={"type": "object", "additionalProperties": False},
            execution_safety=ExecutionSafety.REPLAY_SAFE,
            resource_profile=ResourceProfile(0, 0, 0, 30, 0, "free"),
            result_schema_id=SCHEMA,
        )
        source = json.dumps({
            "dsl_version": "1.3",
            "metadata": {"id": "generated", "name": "Generated"},
            "inputs": [{"id": "value", "schema_id": SCHEMA}],
            "nodes": [
                {
                    "id": "increment",
                    "kind": "action",
                    "inputs": [{"id": "value", "schema_id": SCHEMA}],
                    "outputs": [{"id": "value", "schema_id": SCHEMA}],
                    "handler": {"name": "increment", "version": "1.0.0"},
                },
                {
                    "id": "done",
                    "kind": "terminal",
                    "inputs": [{"id": "value", "schema_id": SCHEMA}],
                },
            ],
            "edges": [{
                "id": "increment_done",
                "from": {"node": "increment", "port": "value"},
                "to": {"node": "done", "port": "value"},
            }],
            "entry": ["increment"],
            "terminals": ["done"],
            "result": {"node": "increment", "port": "value"},
        })
        runtime_registry = LangGraphHandlerRegistry([
            BoundHandler(
                "increment",
                "1.0.0",
                manifest.fingerprint,
                lambda values, config, context: {"value": values["value"] + 1},
            )
        ])

        compiled = compile_generated_workflow(
            source,
            InMemoryHandlerCatalog([manifest]),
            schema_catalog,
            runtime_registry,
        )

        self.assertEqual(42, compiled.invoke({"value": 41})["result"])

    def test_compiles_exact_handlers_and_routes_conditionally(self) -> None:
        decide = node(
            "decide",
            inputs=("value",),
            outputs=("approved",),
            route_mode="exclusive",
        )
        accepted = node(
            "accepted", inputs=("approved",), kind="terminal", handler=False
        )
        rejected = node(
            "rejected", inputs=("approved",), kind="terminal", handler=False
        )
        ir = workflow(
            (decide, accepted, rejected),
            (
                edge(
                    "accept",
                    "decide",
                    "accepted",
                    source_port="approved",
                    target_port="approved",
                    condition={
                        "op": "eq",
                        "left": {"op": "ref", "path": "source.approved"},
                        "right": {"op": "literal", "value": True},
                    },
                ),
                edge(
                    "reject",
                    "decide",
                    "rejected",
                    source_port="approved",
                    target_port="approved",
                    condition=TRUE,
                    priority=10,
                ),
            ),
            entry=("decide",),
            terminals=("accepted", "rejected"),
            result=("decide", "approved"),
        )
        registry = LangGraphHandlerRegistry(
            [binding("decide", lambda values, config, context: {
                "approved": values["value"] == "allow"
            })]
        )

        compiled = compile_workflow(ir, registry)
        allowed = compiled.invoke({"value": "allow"})
        denied = compiled.invoke({"value": "deny"})

        self.assertIs(True, allowed["result"])
        self.assertEqual(["decide", "accepted"], allowed["execution_order"])
        self.assertIs(False, denied["result"])
        self.assertEqual(["decide", "rejected"], denied["execution_order"])

    def test_parallel_fanout_merges_before_join(self) -> None:
        fan = node(
            "fan", inputs=("value",), outputs=("value",), route_mode="parallel"
        )
        left = node("left", inputs=("value",), outputs=("value",))
        right = node("right", inputs=("value",), outputs=("value",))
        join = node(
            "join", inputs=("left", "right"), kind="terminal", handler=False
        )
        ir = workflow(
            (fan, left, right, join),
            (
                edge("fan_left", "fan", "left"),
                edge("fan_right", "fan", "right"),
                edge(
                    "left_join",
                    "left",
                    "join",
                    target_port="left",
                    mapping={
                        "op": "object",
                        "schema_id": SCHEMA,
                        "fields": {"left": {"op": "ref", "path": "source.value"}},
                    },
                ),
                edge(
                    "right_join",
                    "right",
                    "join",
                    target_port="right",
                    mapping={
                        "op": "object",
                        "schema_id": SCHEMA,
                        "fields": {"right": {"op": "ref", "path": "source.value"}},
                    },
                ),
            ),
            entry=("fan",),
            terminals=("join",),
            result=("left", "value"),
        )
        registry = LangGraphHandlerRegistry(
            [
                binding("fan", lambda values, config, context: dict(values)),
                binding("left", lambda values, config, context: {
                    "value": "L:" + values["value"]
                }),
                binding("right", lambda values, config, context: {
                    "value": "R:" + values["value"]
                }),
            ]
        )

        result = compile_workflow(ir, registry).invoke({"value": "x"})

        self.assertEqual("L:x", result["result"])
        self.assertEqual("L:x", result["node_outputs"]["left"]["value"])
        self.assertEqual("R:x", result["node_outputs"]["right"]["value"])
        self.assertEqual("join", result["execution_order"][-1])

    def test_rejects_manifest_fingerprint_mismatch_before_execution(self) -> None:
        action = node("action", inputs=("value",), outputs=("value",))
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (action, terminal),
            (edge("done", "action", "terminal"),),
            entry=("action",),
            terminals=("terminal",),
            result=("action", "value"),
        )
        registry = LangGraphHandlerRegistry(
            [
                BoundHandler(
                    "action",
                    "1.0.0",
                    "sha256:" + "b" * 64,
                    lambda values, config, context: values,
                )
            ]
        )

        with self.assertRaisesRegex(HandlerBindingError, "manifest mismatch"):
            compile_workflow(ir, registry)

    def test_back_edge_loop_replaces_previous_node_output(self) -> None:
        count = node(
            "count",
            inputs=("value",),
            outputs=("value",),
            route_mode="exclusive",
        )
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        less_than_three = {
            "op": "lt",
            "left": {"op": "ref", "path": "source.value"},
            "right": {"op": "literal", "value": 3},
        }
        ir = workflow(
            (count, terminal),
            (
                edge(
                    "again",
                    "count",
                    "count",
                    condition=less_than_three,
                    back_edge=True,
                ),
                edge("done", "count", "terminal", priority=10),
            ),
            entry=("count",),
            terminals=("terminal",),
            result=("count", "value"),
        )
        registry = LangGraphHandlerRegistry(
            [binding("count", lambda values, config, context: {
                "value": values["value"] + 1
            })]
        )

        result = compile_workflow(ir, registry).invoke({"value": 0})

        self.assertEqual(3, result["result"])
        self.assertEqual(
            ["count", "count", "count", "terminal"],
            result["execution_order"],
        )


class LangGraphWorkflowServiceTests(unittest.TestCase):
    def test_service_migrates_early_run_metadata_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_database = Path(directory) / "langgraph-runs.sqlite3"
            with sqlite3.connect(run_database) as connection:
                connection.execute(
                    "CREATE TABLE langgraph_runs("
                    "run_id TEXT PRIMARY KEY,workflow_id TEXT,workflow_version INTEGER,"
                    "status TEXT,revision INTEGER,result_json TEXT,error TEXT,"
                    "created_at TEXT,updated_at TEXT)"
                )
            LangGraphWorkflowService(
                object(),
                LangGraphHandlerRegistry([]),
                run_db_path=run_database,
                checkpoint_db_path=Path(directory) / "checkpoints.sqlite3",
            )
            with sqlite3.connect(run_database) as connection:
                columns = {
                    row[1] for row in connection.execute(
                        "PRAGMA table_info(langgraph_runs)"
                    )
                }

        self.assertIn("input_json", columns)
        self.assertIn("interrupts_json", columns)

    def publish(self, directory: str, ir: WorkflowIR) -> SQLiteWorkflowVersionStore:
        store = SQLiteWorkflowVersionStore(Path(directory) / "workflows.sqlite3")
        store.publish(
            CompiledWorkflow(ir, definition_hash(ir), "test", "sha256:" + "c" * 64),
            expected_latest_version=0,
            source_format="json",
            source_text="{}",
            actor="test:author",
            dsl_version="1.3",
        )
        return store

    def service(self, directory: str, store, registry) -> LangGraphWorkflowService:
        return LangGraphWorkflowService(
            store,
            registry,
            run_db_path=Path(directory) / "langgraph-runs.sqlite3",
            checkpoint_db_path=Path(directory) / "langgraph-checkpoints.sqlite3",
        )

    def test_start_is_durable_and_idempotent(self) -> None:
        action = node("action", inputs=("value",), outputs=("value",))
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (action, terminal),
            (edge("done", "action", "terminal"),),
            entry=("action",), terminals=("terminal",), result=("action", "value"),
        )
        registry = LangGraphHandlerRegistry([
            binding("action", lambda values, config, context: {
                "value": values["value"] + 1
            })
        ])
        with tempfile.TemporaryDirectory() as directory:
            store = self.publish(directory, ir)
            service = self.service(directory, store, registry)

            created = service.start(
                ir.workflow_id, {"value": 4}, idempotency_key="start-once"
            )
            replayed = service.start(
                ir.workflow_id, {"value": 4}, idempotency_key="start-once"
            )

            self.assertEqual("completed", created.status)
            self.assertEqual(5, created.result)
            self.assertEqual(created.run_id, replayed.run_id)
            self.assertEqual(created, service.get(created.run_id))
            with self.assertRaises(LangGraphRunConflict):
                service.start(
                    ir.workflow_id, {"value": 9}, idempotency_key="start-once"
                )

    def test_compatibility_explains_missing_handler_before_start(self) -> None:
        action = node("action", inputs=("value",), outputs=("value",))
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (action, terminal),
            (edge("done", "action", "terminal"),),
            entry=("action",), terminals=("terminal",), result=("action", "value"),
        )
        with tempfile.TemporaryDirectory() as directory:
            store = self.publish(directory, ir)
            unsupported = self.service(
                directory, store, LangGraphHandlerRegistry([])
            ).compatibility(ir.workflow_id)
            supported = self.service(
                directory,
                store,
                LangGraphHandlerRegistry([
                    binding("action", lambda values, config, context: values)
                ]),
            ).compatibility(ir.workflow_id)

        self.assertFalse(unsupported["compatible"])
        self.assertEqual("unsupported_workflow", unsupported["reason"])
        self.assertIn("handler not registered", unsupported["detail"])
        self.assertEqual(
            {"compatible": True, "workflow_version": 1, "engine": "langgraph"},
            supported,
        )

    def test_compatibility_rejects_artifact_transport_before_start(self) -> None:
        artifact = artifact_port("payload")
        action = IRNode(
            "action", "action", (artifact,), (port("value"),),
            IRHandlerRef("action", "1.0.0", FINGERPRINT), {}, (), None,
        )
        ir = WorkflowIR(
            "1.3", "workflow:artifact", "Artifact workflow", "", {},
            (artifact,), (), (action,), (), ("action",), ("action",), (), (), {},
            IRResult("action", "value"),
        )
        registry = LangGraphHandlerRegistry([
            binding("action", lambda values, config, context: values)
        ])
        with tempfile.TemporaryDirectory() as directory:
            result = self.service(
                directory, self.publish(directory, ir), registry
            ).compatibility(ir.workflow_id)

        self.assertFalse(result["compatible"])
        self.assertEqual("unsupported_workflow", result["reason"])
        self.assertIn("artifact_ref", result["detail"])

    def test_interrupted_run_resumes_after_service_rebuild(self) -> None:
        action = node("action", inputs=("value",), outputs=("value",))
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (action, terminal),
            (edge("done", "action", "terminal"),),
            entry=("action",), terminals=("terminal",), result=("action", "value"),
        )

        def approval(values, config, context):
            accepted = interrupt("approve")
            return {"value": values["value"] if accepted else -1}

        registry = LangGraphHandlerRegistry([binding("action", approval)])
        with tempfile.TemporaryDirectory() as directory:
            store = self.publish(directory, ir)
            first = self.service(directory, store, registry)
            paused = first.start(
                ir.workflow_id, {"value": 7}, idempotency_key="interrupt"
            )
            rebuilt = self.service(directory, store, registry)
            completed = rebuilt.resume(
                paused.run_id,
                True,
                expected_revision=paused.revision,
                idempotency_key="resume-once",
            )
            replayed = rebuilt.resume(
                paused.run_id,
                True,
                expected_revision=paused.revision,
                idempotency_key="resume-once",
            )

            self.assertEqual("interrupted", paused.status)
            self.assertEqual("approve", paused.interrupts[0]["value"])
            self.assertEqual("completed", completed.status)
            self.assertEqual(7, completed.result)
            self.assertEqual(paused.revision + 1, completed.revision)
            self.assertEqual(completed, replayed)
            self.assertEqual((completed,), rebuilt.list_runs(status="completed"))
            self.assertEqual((), rebuilt.recover_running())
            with self.assertRaises(LangGraphRunConflict):
                rebuilt.resume(
                    paused.run_id,
                    False,
                    expected_revision=paused.revision,
                    idempotency_key="resume-once",
                )

    def test_cancel_is_versioned_idempotent_and_wins_over_late_settlement(self):
        action = node("action", inputs=("value",), outputs=("value",))
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (action, terminal),
            (edge("done", "action", "terminal"),),
            entry=("action",), terminals=("terminal",), result=("action", "value"),
        )

        def approval(values, config, context):
            interrupt("approve")
            return {"value": values["value"]}

        cancelled_runs = []
        registry = LangGraphHandlerRegistry([BoundHandler(
            "action", "1.0.0", FINGERPRINT, approval,
            lambda run_id: not cancelled_runs.append(run_id),
        )])
        with tempfile.TemporaryDirectory() as directory:
            store = self.publish(directory, ir)
            service = self.service(directory, store, registry)
            paused = service.start(
                ir.workflow_id, {"value": 7}, idempotency_key="cancel-start"
            )
            cancelled = service.cancel(
                paused.run_id,
                expected_revision=paused.revision,
                idempotency_key="cancel-once",
            )
            replayed = service.cancel(
                paused.run_id,
                expected_revision=paused.revision,
                idempotency_key="cancel-once",
            )
            late = service._settle(
                paused.run_id, "completed", result={"too": "late"}
            )

        self.assertEqual("cancelled", cancelled.status)
        self.assertEqual(paused.revision + 1, cancelled.revision)
        self.assertEqual(cancelled, replayed)
        self.assertEqual(cancelled, late)
        self.assertIsNone(late.result)
        self.assertEqual([paused.run_id], cancelled_runs)


class LangGraphProductionWiringTests(unittest.TestCase):
    def registration(self, client) -> HandlerRegistration:
        manifest = HandlerManifest(
            "trusted_agent", "1.0.0", ("action",),
            {"value": SCHEMA}, {"value": SCHEMA},
            {"type": "object"}, ExecutionSafety.UNKNOWN_ON_LEASE_LOSS,
            ResourceProfile(1000, 1000, 0, 30, 0, "agent"),
            "schema://object/1.0", ("agent.invoke",), (), True, True,
        )
        return HandlerRegistration(
            manifest, AgentHandler(client), "trusted-agent@test"
        )

    def bound_node(self, manifest) -> IRNode:
        return IRNode(
            "agent", "action", (port("value"),), (port("value"),),
            IRHandlerRef(manifest.name, manifest.version, manifest.fingerprint),
            {}, (), None, None,
        )

    def test_successful_agent_attempt_is_replayed_from_journal(self) -> None:
        client = FakeAgentClient(AgentResponse({"value": 8}, None, "provider:1"))
        registration = self.registration(client)
        with tempfile.TemporaryDirectory() as directory:
            registry = trusted_handlers(
                [registration],
                attempt_db_path=Path(directory) / "runs.sqlite3",
            )
            bound = registry.resolve(self.bound_node(registration.manifest))
            context = LangGraphExecutionContext(
                "workflow:test", "agent", "langgraph_run:test",
                "langgraph_attempt:test:agent:1",
            )
            first = bound.invoke({"value": 7}, {}, context)
            replayed = bound.invoke({"value": 7}, {}, context)

        self.assertEqual({"value": 8}, first)
        self.assertEqual(first, replayed)
        self.assertEqual(1, len(client.requests))

    def test_agent_artifact_is_committed_and_replayed(self) -> None:
        class ArtifactClient(FakeAgentClient):
            def execute(self, request, context):
                self.requests.append(request)
                artifact_id = context.artifacts.write(
                    name="value", content=b"agent document",
                    content_type="application/octet-stream",
                )
                return AgentResponse(
                    {"value": {"artifact_id": str(artifact_id)}},
                    None, "provider:artifact", artifact_refs=(artifact_id,),
                )

        client = ArtifactClient()
        registration = self.registration(client)
        output = artifact_port("value")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runs.sqlite3"
            registry = trusted_handlers(
                [registration], attempt_db_path=database,
            )
            bound = registry.resolve(self.bound_node(registration.manifest))
            context = LangGraphExecutionContext(
                "workflow:test", "agent", "langgraph_run:test",
                "langgraph_attempt:test:agent:1", (),
                (to_primitive(output),),
            )
            first = bound.invoke({"value": "prompt"}, {}, context)
            replayed = bound.invoke({"value": "prompt"}, {}, context)
            artifact_id = first["value"]["artifact_id"]
            content = LangGraphArtifactStore(
                database, Path(directory) / "artifacts",
            ).access(
                run_id="langgraph_run:test", node_id="consumer",
                attempt_id="langgraph_attempt:consumer", output_ports=(),
                inputs={"value": {"artifact_id": artifact_id}},
            ).read(artifact_id)

        self.assertEqual(b"agent document", content)
        self.assertEqual(first, replayed)
        self.assertEqual(1, len(client.requests))

    def test_unknown_agent_attempt_is_never_executed_twice(self) -> None:
        client = FakeAgentClient(
            error=UnknownExternalResultError("connection lost after submission")
        )
        registration = self.registration(client)
        with tempfile.TemporaryDirectory() as directory:
            registry = trusted_handlers(
                [registration],
                attempt_db_path=Path(directory) / "runs.sqlite3",
            )
            bound = registry.resolve(self.bound_node(registration.manifest))
            context = LangGraphExecutionContext(
                "workflow:test", "agent", "langgraph_run:test",
                "langgraph_attempt:test:agent:1",
            )
            with self.assertRaisesRegex(RuntimeError, "connection lost"):
                bound.invoke({"value": 7}, {}, context)
            with self.assertRaisesRegex(RuntimeError, "unknown outcome"):
                bound.invoke({"value": 7}, {}, context)

        self.assertEqual(1, len(client.requests))

    def test_unknown_agent_result_parks_the_run_without_reexecution(self) -> None:
        client = FakeAgentClient(
            error=UnknownExternalResultError("connection lost after submission")
        )
        registration = self.registration(client)
        action = self.bound_node(registration.manifest)
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (action, terminal),
            (edge("done", "agent", "terminal"),),
            entry=("agent",), terminals=("terminal",), result=("agent", "value"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteWorkflowVersionStore(root / "workflows.sqlite3")
            store.publish(
                CompiledWorkflow(
                    ir, definition_hash(ir), "test", "sha256:" + "c" * 64
                ),
                expected_latest_version=0,
                source_format="json",
                source_text="{}",
                actor="test:author",
                dsl_version="1.3",
            )
            service = build_service(
                store.path, [registration], state_directory=root,
            )
            parked = service.start(
                ir.workflow_id, {"value": 7}, idempotency_key="unknown-agent"
            )
            recovered = service.recover(parked.run_id)

        self.assertEqual("unknown", parked.status)
        self.assertIn("connection lost", parked.error)
        self.assertEqual(parked, recovered)
        self.assertEqual(1, len(client.requests))


class LangGraphHttpApiTests(unittest.TestCase):
    def publish(self, directory: str, ir: WorkflowIR) -> SQLiteWorkflowVersionStore:
        store = SQLiteWorkflowVersionStore(Path(directory) / "workflows.sqlite3")
        store.publish(
            CompiledWorkflow(ir, definition_hash(ir), "test", "sha256:" + "c" * 64),
            expected_latest_version=0,
            source_format="json",
            source_text="{}",
            actor="test:author",
            dsl_version="1.3",
        )
        return store

    def service(self, directory: str, store, registry) -> LangGraphWorkflowService:
        return LangGraphWorkflowService(
            store,
            registry,
            run_db_path=Path(directory) / "langgraph-runs.sqlite3",
            checkpoint_db_path=Path(directory) / "langgraph-checkpoints.sqlite3",
        )

    def test_optional_routes_start_list_and_read_a_run(self) -> None:
        action = node("action", inputs=("value",), outputs=("value",))
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (action, terminal),
            (edge("done", "action", "terminal"),),
            entry=("action",), terminals=("terminal",), result=("action", "value"),
        )
        registry = LangGraphHandlerRegistry([
            binding("action", lambda values, config, context: {
                "value": values["value"] + 1
            })
        ])
        with tempfile.TemporaryDirectory() as directory:
            store = self.publish(directory, ir)
            service = self.service(directory, store, registry)
            app = create_app(
                Path(directory) / "orbit.sqlite3",
                workflow_db_path=store.path,
                langgraph_service=service,
                authenticator=lambda request: request.headers.get("x-orbit-actor"),
                authorizer=Authorizer(lambda actor: (
                    READ_SCOPE, WRITE_SCOPE, OPS_WRITE_SCOPE,
                )),
                worker_count=1,
            )
            with AsgiHarness(app) as client:
                started = client.post(
                    "/api/v1/langgraph-runs",
                    actor="test:operator",
                    key="http-start",
                    body={"workflow_id": ir.workflow_id, "input": {"value": 8}},
                )
                self.assertEqual(200, started.status_code, started.text)
                run = started.json()["data"]["run"]
                listed = client.get(
                    "/api/v1/langgraph-runs?status=completed",
                    actor="test:operator",
                )
                detail = client.get(
                    f"/api/v1/langgraph-runs/{run['run_id']}",
                    actor="test:operator",
                )
                tools = client.request(
                    "POST", "/mcp", actor="test:operator",
                    body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                ).json()["result"]["tools"]
                mcp_started = client.request(
                    "POST", "/mcp", actor="test:operator",
                    body={
                        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {
                            "name": "start_langgraph_run",
                            "arguments": {
                                "workflow_id": ir.workflow_id,
                                "input": {"value": 20},
                                "idempotency_key": "mcp-start",
                            },
                        },
                    },
                ).json()

        self.assertEqual(9, run["result"])
        self.assertEqual(run["run_id"], listed.json()["data"]["runs"][0]["run_id"])
        self.assertEqual(run["revision"], detail.json()["projection_version"])
        self.assertIn("start_langgraph_run", {item["name"] for item in tools})
        self.assertIn("cancel_langgraph_run", {item["name"] for item in tools})
        mcp_payload = json.loads(
            mcp_started["result"]["content"][0]["text"]
        )
        self.assertEqual(21, mcp_payload["result"])

    def test_routes_are_absent_without_explicit_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                Path(directory) / "orbit.sqlite3",
                authenticator=lambda request: request.headers.get("x-orbit-actor"),
                authorizer=Authorizer(lambda actor: (READ_SCOPE,)),
                worker_count=1,
            )
            with AsgiHarness(app) as client:
                response = client.get("/api/v1/langgraph-runs")
                tools = client.request(
                    "POST", "/mcp", actor="test:reader",
                    body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                ).json()["result"]["tools"]
                capability = client.get(
                    "/api/v1/capabilities", actor="test:reader"
                ).json()["data"][
                    "capabilities"
                ]["langgraph_workflows"]
        self.assertEqual(404, response.status_code)
        self.assertNotIn(
            "start_langgraph_run", {item["name"] for item in tools}
        )
        self.assertEqual(
            {"available": False, "reason": "service_not_configured"}, capability
        )

    def test_capability_and_startup_recovery_follow_optional_wiring(self) -> None:
        class RecoveringService:
            def __init__(self) -> None:
                self.recoveries = 0

            def recover_running(self):
                self.recoveries += 1
                return ()

        service = RecoveringService()
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                Path(directory) / "orbit.sqlite3",
                langgraph_service=service,
                authenticator=lambda request: request.headers.get("x-orbit-actor"),
                authorizer=Authorizer(lambda actor: (READ_SCOPE,)),
                worker_count=1,
            )
            self.assertIs(service, app.state.langgraph_service)
            with AsgiHarness(app) as client:
                capability = client.get(
                    "/api/v1/capabilities", actor="test:reader"
                ).json()["data"][
                    "capabilities"
                ]["langgraph_workflows"]
        self.assertEqual(
            {"available": True, "api": "/api/v1/langgraph-runs"}, capability
        )
        self.assertEqual(1, service.recoveries)

    def test_agent_generation_publishes_and_runs_through_langgraph_mcp(self) -> None:
        document = {
            "dsl_version": "1.3",
            "metadata": {"id": "generated", "name": "Agent generated"},
            "inputs": [{
                "id": "value", "schema_id": "example://integer/1.0",
            }],
            "nodes": [
                {
                    "id": "transform", "kind": "action", "label": "Transform",
                    "inputs": [{
                        "id": "value", "schema_id": "example://integer/1.0",
                    }],
                    "outputs": [{
                        "id": "value", "schema_id": "example://integer/1.0",
                    }],
                    "handler": {"name": "transform", "version": "1.0.0"},
                },
                {
                    "id": "done", "kind": "terminal",
                    "inputs": [{
                        "id": "result", "schema_id": "schema://object/1.0",
                    }],
                },
                {
                    "id": "approve", "kind": "human", "label": "Approve",
                    "inputs": [{
                        "id": "value", "schema_id": "example://integer/1.0",
                    }],
                    "outputs": [{
                        "id": "result", "schema_id": "schema://object/1.0",
                    }],
                    "config": {
                        "task_kind": "approval", "participants": ["local"],
                        "quorum": "any",
                    },
                },
            ],
            "edges": [
                {
                    "id": "review",
                    "from": {"node": "transform", "port": "value"},
                    "to": {"node": "approve", "port": "value"},
                },
                {
                    "id": "finish",
                    "from": {"node": "approve", "port": "result"},
                    "to": {"node": "done", "port": "result"},
                },
            ],
            "entry": ["transform"],
            "terminals": ["done"],
            "result": {"node": "approve", "port": "result"},
        }

        def mcp(client, name, arguments, request_id):
            response = client.request(
                "POST", "/mcp", actor="test:agent",
                body={
                    "jsonrpc": "2.0", "id": request_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            ).json()
            self.assertFalse(response["result"]["isError"], response)
            return json.loads(response["result"]["content"][0]["text"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow_path = root / "workflows.sqlite3"
            registrations = list(builtin_handlers())
            service = build_service(
                workflow_path, registrations, state_directory=root,
            )
            app = create_app(
                root / "orbit.sqlite3",
                workflow_db_path=workflow_path,
                handlers=registrations,
                schemas=BUILTIN_SCHEMAS,
                workflow_generator=lambda _prompt: json.dumps(document),
                langgraph_service=service,
                authenticator=lambda request: request.headers.get("x-orbit-actor"),
                authorizer=Authorizer(lambda _actor: (
                    READ_SCOPE, WRITE_SCOPE, OPS_WRITE_SCOPE,
                )),
                worker_count=1,
                poll_seconds=0.01,
            )
            with AsgiHarness(app) as client:
                job = mcp(client, "generate_workflow", {
                    "prompt": "Build an identity workflow",
                    "idempotency_key": "generate-langgraph",
                }, 1)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    authored = mcp(
                        client, "get_authoring_job", {"job_id": job["job_id"]}, 2,
                    )
                    if authored["status"] not in {"queued", "running"}:
                        break
                    time.sleep(0.01)
                self.assertEqual("done", authored["status"], authored)
                catalog = client.get(
                    "/api/v1/workflows", actor="test:agent"
                ).json()["data"]["workflows"]
                generated = next(
                    item for item in catalog
                    if item["workflow_id"] == authored["result"]["workflow_id"]
                )
                self.assertTrue(
                    generated["langgraph_compatibility"]["compatible"]
                )
                self.assertIn(
                    "langgraph_run.start",
                    {command["command"] for command in generated["allowed_commands"]},
                )
                interrupted = mcp(client, "start_langgraph_run", {
                    "workflow_id": authored["result"]["workflow_id"],
                    "input": {"value": 42},
                    "idempotency_key": "run-generated-langgraph",
                }, 3)
                self.assertEqual("interrupted", interrupted["status"])
                self.assertEqual("approve", interrupted["interrupts"][0]["value"]["node_id"])
                run = mcp(client, "resume_langgraph_run", {
                    "run_id": interrupted["run_id"],
                    "value": {"result": {"decision": "approve", "value": 43}},
                    "expected_version": interrupted["revision"],
                    "idempotency_key": "resume-generated-langgraph",
                }, 4)

        self.assertEqual("completed", run["status"])
        self.assertEqual({"decision": "approve", "value": 43}, run["result"])

    def test_interrupt_resume_is_authorized_versioned_and_idempotent(self) -> None:
        action = node("action", inputs=("value",), outputs=("value",))
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (action, terminal),
            (edge("done", "action", "terminal"),),
            entry=("action",), terminals=("terminal",), result=("action", "value"),
        )

        def approval(values, config, context):
            accepted = interrupt({"question": "approve?"})
            return {"value": values["value"] if accepted else -1}

        registry = LangGraphHandlerRegistry([binding("action", approval)])
        with tempfile.TemporaryDirectory() as directory:
            store = self.publish(directory, ir)
            service = self.service(directory, store, registry)
            app = create_app(
                Path(directory) / "orbit.sqlite3",
                workflow_db_path=store.path,
                langgraph_service=service,
                authenticator=lambda request: request.headers.get("x-orbit-actor"),
                authorizer=Authorizer(lambda actor: (
                    (READ_SCOPE,) if actor == "test:reader"
                    else (READ_SCOPE, WRITE_SCOPE, OPS_WRITE_SCOPE)
                )),
                worker_count=1,
            )
            with AsgiHarness(app) as client:
                started = client.post(
                    "/api/v1/langgraph-runs",
                    actor="test:operator", key="api-interrupt",
                    body={"workflow_id": ir.workflow_id, "input": {"value": 12}},
                ).json()["data"]["run"]
                reader_view = client.get(
                    f"/api/v1/langgraph-runs/{started['run_id']}",
                    actor="test:reader",
                ).json()["data"]
                forbidden = client.post(
                    f"/api/v1/langgraph-runs/{started['run_id']}/resume",
                    actor="test:reader", key="reader-resume",
                    body={"expected_version": started["revision"], "value": True},
                )
                path = f"/api/v1/langgraph-runs/{started['run_id']}/resume"
                body = {"expected_version": started["revision"], "value": True}
                completed = client.post(
                    path, actor="test:operator", key="api-resume", body=body,
                )
                replayed = client.post(
                    path, actor="test:operator", key="api-resume", body=body,
                )
                conflict = client.post(
                    path,
                    actor="test:operator",
                    key="api-resume",
                    body={"expected_version": started["revision"], "value": False},
                )
                to_cancel = client.post(
                    "/api/v1/langgraph-runs",
                    actor="test:operator", key="api-cancel-start",
                    body={"workflow_id": ir.workflow_id, "input": {"value": 13}},
                ).json()["data"]["run"]
                cancel_path = (
                    f"/api/v1/langgraph-runs/{to_cancel['run_id']}/cancel"
                )
                cancel_body = {"expected_version": to_cancel["revision"]}
                cancel_forbidden = client.post(
                    cancel_path, actor="test:reader", key="reader-cancel",
                    body=cancel_body,
                )
                cancelled = client.post(
                    cancel_path, actor="test:operator", key="api-cancel",
                    body=cancel_body,
                )
                cancel_replayed = client.post(
                    cancel_path, actor="test:operator", key="api-cancel",
                    body=cancel_body,
                )

        self.assertEqual("interrupted", started["status"])
        self.assertEqual({"question": "approve?"}, started["interrupts"][0]["value"])
        self.assertTrue(started["allowed_commands"])
        self.assertEqual([], reader_view["allowed_commands"])
        self.assertEqual(403, forbidden.status_code)
        self.assertEqual(200, completed.status_code)
        self.assertEqual(completed.json(), replayed.json())
        self.assertEqual(12, completed.json()["data"]["run"]["result"])
        self.assertEqual(409, conflict.status_code)
        self.assertEqual("idempotency_conflict", conflict.json()["error"]["code"])
        self.assertEqual(403, cancel_forbidden.status_code)
        self.assertEqual("cancelled", cancelled.json()["data"]["run"]["status"])
        self.assertEqual(cancelled.json(), cancel_replayed.json())


if __name__ == "__main__":
    unittest.main()
