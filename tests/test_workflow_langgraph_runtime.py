from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid

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
    IRExtension,
    IRHandlerRef,
    IRNode,
    IRPolicy,
    IRPort,
    IRResult,
    WorkflowIR,
)
from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.data import PortDataPolicy, PortTransport
from orbit.workflow.domain.handlers import ResourceProfile, UnknownExternalResultError
from orbit.workflow.domain.serialization import definition_hash, to_primitive
from unittest.mock import patch

from orbit.workflow.langgraph_runtime import service as langgraph_service_module
from orbit.workflow.langgraph_runtime import (
    BoundHandler,
    HandlerBindingError,
    HandlerOutcome,
    LangGraphHandlerRegistry,
    LangGraphExecutionContext,
    LangGraphCompletionUnsatisfied,
    LangGraphJoinDeadlineExceeded,
    LangGraphRunConflict,
    LangGraphRetryableError,
    LangGraphWorkflowService,
    build_service,
    compile_generated_workflow,
    compile_workflow,
)
from orbit.workflow.handlers.agent import AgentHandler, AgentResponse, FakeAgentClient
from orbit.workflow.handlers.tools import (
    ToolHandler, ToolManifest, ToolRegistry, ToolResult,
)
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


def secret_port(name: str) -> IRPort:
    return IRPort(
        name, SCHEMA, True, False, None, "",
        PortDataPolicy(PortTransport.SECRET_REF),
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
    policy_ref=None,
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
        policy_ref,
    )


def workflow(nodes, edges, *, entry, terminals, result, policies=()) -> WorkflowIR:
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
        tuple(policies),
        (),
        {},
        IRResult(*result),
    )


def binding(name, invoke) -> BoundHandler:
    return BoundHandler(name, "1.0.0", FINGERPRINT, invoke)


class LangGraphWorkflowCompilerValidationTests(unittest.TestCase):
    def test_extensions_are_rejected_before_execution(self) -> None:
        action = node("action", inputs=("value",), outputs=("value",))
        base = workflow(
            (action,), (), entry=("action",), terminals=("action",),
            result=("action", "value"),
        )
        ir = WorkflowIR(
            base.ir_version, base.workflow_id, base.name, base.description,
            base.labels, base.inputs, base.outputs, base.nodes, base.edges,
            base.entry, base.terminals, base.policies,
            (IRExtension("vendor.example", "1.0.0", {}),),
            base.indexes, base.result,
        )

        with self.assertRaisesRegex(
            ValueError, "do not support extensions: vendor.example",
        ):
            compile_workflow(
                ir,
                LangGraphHandlerRegistry([
                    binding("action", lambda values, config, context: values)
                ]),
            )

    def test_legacy_controller_node_kinds_are_rejected(self) -> None:
        for kind in ("agentic", "foreach", "subflow", "extension"):
            with self.subTest(kind=kind):
                controller = node(
                    "controller", inputs=("value",), outputs=("value",),
                    kind=kind,
                )
                ir = workflow(
                    (controller,), (), entry=("controller",),
                    terminals=("controller",), result=("controller", "value"),
                )

                with self.assertRaisesRegex(
                    ValueError, f"controller:{kind}",
                ):
                    compile_workflow(
                        ir,
                        LangGraphHandlerRegistry([
                            binding(
                                "controller",
                                lambda values, config, context: values,
                            )
                        ]),
                    )

    def test_default_completion_policy_is_compatible(self) -> None:
        action = node("action", inputs=("value",), outputs=("value",))
        ir = workflow(
            (action,), (),
            entry=("action",),
            terminals=("action",),
            result=("action", "value"),
            policies=(IRPolicy("complete", "completion", {}),),
        )

        result = compile_workflow(
            ir,
            LangGraphHandlerRegistry([
                binding("action", lambda values, config, context: values)
            ]),
        ).invoke({"value": 7})

        self.assertEqual(7, result["result"])

    def test_completion_policy_fails_below_required_terminal_count(self) -> None:
        action = node("action", inputs=("value",), outputs=("value",))
        ir = workflow(
            (action,), (),
            entry=("action",),
            terminals=("action",),
            result=("action", "value"),
            policies=(IRPolicy(
                "complete", "completion", {"required_terminal_count": 2},
            ),),
        )

        with self.assertRaisesRegex(
            LangGraphCompletionUnsatisfied, "requires 2.*reached 1",
        ):
            compile_workflow(
                ir,
                LangGraphHandlerRegistry([
                    binding("action", lambda values, config, context: values)
                ]),
            ).invoke({"value": 7})

    def test_completion_policy_accepts_multiple_successful_terminals(self) -> None:
        fan = node(
            "fan", inputs=("value",), outputs=("value",), route_mode="parallel",
        )
        left = node(
            "left", inputs=("value",), kind="terminal", handler=False,
        )
        right = node(
            "right", inputs=("value",), kind="terminal", handler=False,
        )
        ir = workflow(
            (fan, left, right),
            (edge("to_left", "fan", "left"), edge("to_right", "fan", "right")),
            entry=("fan",), terminals=("left", "right"),
            result=("fan", "value"),
            policies=(IRPolicy(
                "complete", "completion", {"required_terminal_count": 2},
            ),),
        )

        result = compile_workflow(
            ir,
            LangGraphHandlerRegistry([
                binding("fan", lambda values, config, context: values)
            ]),
        ).invoke({"value": 7})

        self.assertEqual(7, result["result"])
        self.assertEqual({"left", "right"}, set(result["execution_order"][-2:]))

    def test_invalid_loop_exhaustion_is_rejected_fail_closed(self) -> None:
        action = node(
            "action", inputs=("value",), outputs=("value",),
            route_mode="exclusive",
        )
        ir = workflow(
            (action,),
            (edge(
                "again", "action", "action", back_edge=True,
                policy_ref="bounded",
            ),),
            entry=("action",),
            terminals=(),
            result=("action", "value"),
            policies=(IRPolicy(
                "bounded", "loop",
                {"max_iterations": 2, "exhaustion": "continue"},
            ),),
        )

        with self.assertRaisesRegex(ValueError, "invalid exhaustion"):
            compile_workflow(
                ir,
                LangGraphHandlerRegistry([
                    binding("action", lambda values, config, context: values)
                ]),
            )

    def test_retry_policy_requires_retry_safe_handler(self) -> None:
        action = node("action", inputs=("value",), outputs=("value",))
        action = IRNode(
            action.id, action.kind, action.inputs, action.outputs, action.handler,
            action.config, ("retry_network",), action.extension,
            action.route_mode,
        )
        base = workflow(
            (action,), (), entry=("action",), terminals=("action",),
            result=("action", "value"),
        )
        ir = WorkflowIR(
            base.ir_version, base.workflow_id, base.name, base.description,
            base.labels, base.inputs, base.outputs, base.nodes, base.edges,
            base.entry, base.terminals,
            (IRPolicy("retry_network", "retry", {"max_attempts": 3}),),
            base.extensions, base.indexes, base.result,
        )

        with self.assertRaisesRegex(
            ValueError, "retry-safe Handler",
        ):
            compile_workflow(
                ir,
                LangGraphHandlerRegistry([
                    binding("action", lambda values, config, context: values)
                ]),
            )

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
                    input_ports=(output,),
                ).read(artifact_id)

            store.commit(producer.produced_artifact_ids)
            with self.assertRaises(LangGraphArtifactAccessDenied):
                store.access(
                    run_id="langgraph_run:one", node_id="spoofed",
                    attempt_id="langgraph_attempt:spoofed", output_ports=(),
                    input_ports=(port("document"),),
                    inputs={"document": {"artifact_id": artifact_id}},
                ).read(artifact_id)
            content = store.access(
                run_id="langgraph_run:one", node_id="consume",
                attempt_id="langgraph_attempt:two", output_ports=(),
                inputs={"document": {"artifact_id": artifact_id}},
                input_ports=(output,),
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
                input_ports=(output,),
            )
            with self.assertRaises(LangGraphArtifactAccessDenied):
                foreign.read(artifact_id)

    def test_lineage_tracks_authorized_artifact_derivation(self) -> None:
        output = artifact_port("document")
        with tempfile.TemporaryDirectory() as directory:
            store = LangGraphArtifactStore(
                Path(directory) / "runs.sqlite3", Path(directory) / "blobs",
            )
            source = store.access(
                run_id="langgraph_run:one", node_id="source",
                attempt_id="langgraph_attempt:source", output_ports=(output,),
                inputs={}, actor="test:owner",
            )
            source_id = source.write(
                name="document", content=b"source",
                content_type="application/octet-stream",
            )
            source.commit()
            derived = store.access(
                run_id="langgraph_run:one", node_id="derived",
                attempt_id="langgraph_attempt:derived", output_ports=(output,),
                inputs={"document": {"artifact_id": source_id}},
                input_ports=(output,),
                actor="test:owner",
            )
            derived.read(source_id)
            derived_id = derived.write(
                name="document", content=b"derived",
                content_type="application/octet-stream",
            )
            derived.commit()

            graph = store.lineage(derived_id, actor="test:owner")
            reverse = store.lineage(source_id, actor="test:owner")
            with self.assertRaises(LookupError):
                store.lineage(derived_id, actor="test:other")

        self.assertEqual([source_id], graph["derived_from"])
        self.assertEqual([derived_id], reverse["derived_artifacts"])

    def test_gc_preserves_deduplicated_blob_still_in_use(self) -> None:
        output = artifact_port("document")
        with tempfile.TemporaryDirectory() as directory:
            store = LangGraphArtifactStore(
                Path(directory) / "runs.sqlite3", Path(directory) / "blobs",
            )
            abandoned = store.access(
                run_id="langgraph_run:one", node_id="failed",
                attempt_id="langgraph_attempt:failed", output_ports=(output,),
                inputs={},
            )
            abandoned_id = abandoned.write(
                name="document", content=b"shared",
                content_type="application/octet-stream",
            )
            retained = store.access(
                run_id="langgraph_run:one", node_id="success",
                attempt_id="langgraph_attempt:success", output_ports=(output,),
                inputs={},
            )
            retained_id = retained.write(
                name="document", content=b"shared",
                content_type="application/octet-stream",
            )
            retained.commit()
            store.abandon((abandoned_id,))

            collected = store.collect_abandoned()
            content = store.read(retained_id)

        self.assertEqual((abandoned_id,), collected)
        self.assertEqual(b"shared", content)


class LangGraphWorkflowCompilerTests(unittest.TestCase):
    def test_deadline_join_opens_from_partial_checkpoint(self) -> None:
        deadline = IRPolicy(
            "wait_for_enough", "join",
            {
                "mode": "deadline", "merge_mode": "array_by_edge",
                "deadline_seconds": 10, "min_successful": 1,
            },
        )
        fan = node(
            "fan", inputs=("value",), outputs=("value",), route_mode="parallel",
        )
        fast = node("fast", inputs=("value",), outputs=("value",))
        waiting = node(
            "waiting", inputs=("value",), outputs=("value",),
            kind="human", handler=False,
        )
        join = node(
            "join", inputs=("value",), outputs=("value",),
            kind="join", handler=False,
        )
        join = IRNode(
            join.id, join.kind, join.inputs, join.outputs, join.handler,
            join.config, (deadline.id,), join.extension, join.route_mode,
        )
        ir = workflow(
            (fan, fast, waiting, join),
            (
                edge("fan_fast", "fan", "fast"),
                edge("fan_waiting", "fan", "waiting"),
                edge("fast_join", "fast", "join"),
                edge("waiting_join", "waiting", "join"),
            ),
            entry=("fan",), terminals=("join",), result=("join", "value"),
            policies=(deadline,),
        )
        registry = LangGraphHandlerRegistry([
            binding("fan", lambda values, config, context: dict(values)),
            binding("fast", lambda values, config, context: {"value": "fast"}),
        ])
        compiled = compile_workflow(ir, registry, checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "deadline-partial"}}

        interrupted = compiled.invoke({"value": "start"}, config=config)
        completed = compiled.fire_join_deadline("join", config=config)

        self.assertIsNone(interrupted["result"])
        self.assertEqual(["fast"], completed["result"])
        self.assertEqual("join", completed["execution_order"][-1])

    def _deadline_race(self, *, second_consumer: bool):
        """fan → {fast, waiting} → join, with the deadline firing on `fast`.

        `fast` finishes inside the superstep that `waiting` interrupts, so its
        writes are pending on a checkpoint that was never committed — which is
        the state the deadline has to fire from.
        """

        deadline = IRPolicy(
            "wait_for_enough", "join",
            {
                "mode": "deadline", "merge_mode": "array_by_edge",
                "deadline_seconds": 10, "min_successful": 1,
            },
        )
        fan = node(
            "fan", inputs=("value",), outputs=("value",), route_mode="parallel",
        )
        fast = node("fast", inputs=("value",), outputs=("value",))
        waiting = node(
            "waiting", inputs=("value",), outputs=("value",),
            kind="human", handler=False,
        )
        join = node(
            "join", inputs=("value",), outputs=("value",),
            kind="join", handler=False,
        )
        join = IRNode(
            join.id, join.kind, join.inputs, join.outputs, join.handler,
            join.config, (deadline.id,), join.extension, join.route_mode,
        )
        after = node(
            "after",
            inputs=("value", "direct") if second_consumer else ("value",),
            outputs=("value",),
        )
        edges = [
            edge("fan_fast", "fan", "fast"),
            edge("fan_waiting", "fan", "waiting"),
            edge("fast_join", "fast", "join"),
            edge("waiting_join", "waiting", "join"),
            edge("join_after", "join", "after"),
        ]
        if second_consumer:
            edges.append(
                edge("fast_after", "fast", "after", target_port="direct")
            )
        ir = workflow(
            (fan, fast, waiting, join, after),
            tuple(edges),
            entry=("fan",), terminals=("after",), result=("after", "value"),
            policies=(deadline,),
        )
        registry = LangGraphHandlerRegistry([
            binding("fan", lambda values, config, context: dict(values)),
            binding("fast", lambda values, config, context: {"value": "fast"}),
            binding(
                "after",
                lambda values, config, context: {
                    "value": f"after:{values.get('direct', values['value'])}"
                },
            ),
        ])
        compiled = compile_workflow(ir, registry, checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "deadline-race"}}
        compiled.invoke({"value": "start"}, config=config)
        return compiled, config

    def test_a_deadline_keeps_the_branch_that_won_it(self) -> None:
        """The branch the join consumed has to survive in the run's state.

        Firing re-applies the join as a write onto the *stored* checkpoint,
        which is the one the interrupted superstep never committed — so `fast`
        was there to be merged into the join's inputs and gone from the state
        the run reports, having apparently never executed.
        """

        compiled, config = self._deadline_race(second_consumer=False)
        completed = compiled.fire_join_deadline("join", config=config)

        self.assertEqual(["fast"], completed["node_outputs"]["join"]["value"])
        self.assertIn("fast", completed["node_outputs"])
        self.assertEqual(
            ["fan", "fast", "join", "after"], list(completed["execution_order"]),
        )

    def test_a_second_consumer_of_that_branch_still_gets_its_input(self) -> None:
        """Not only the report: the next node reads `node_outputs` too."""

        compiled, config = self._deadline_race(second_consumer=True)
        completed = compiled.fire_join_deadline("join", config=config)

        self.assertEqual("after:fast", completed["result"])

    def test_completion_counts_distinct_terminals_not_arrivals(self) -> None:
        """One terminal reached twice is one terminal.

        `required_terminal_count` asks how many of a workflow's terminals were
        reached. Summing membership over `execution_order` counted arrivals
        instead, so a workflow that demanded both of its outcomes could be
        signed off having produced one of them twice — which a rework or a
        loop back edge upstream is what makes reachable.

        The rule is exercised directly on the state such a loop leaves behind,
        rather than through a graph built to produce it: the arithmetic is the
        subject, and a contrived loop would only be a slower way to write the
        same `execution_order` down.
        """

        completion = IRPolicy(
            "both", "completion", {"required_terminal_count": 2},
        )
        work = node("work", inputs=("value",), outputs=("value",))
        first = node("first", inputs=("value",), kind="terminal", handler=False)
        second = node("second", inputs=("value",), kind="terminal", handler=False)
        ir = workflow(
            (work, first, second),
            (
                edge("to_first", "work", "first"),
                edge("to_second", "work", "second", priority=1),
            ),
            entry=("work",), terminals=("first", "second"),
            result=("work", "value"), policies=(completion,),
        )
        compiled = compile_workflow(
            ir, LangGraphHandlerRegistry([
                binding("work", lambda values, config, context: dict(values)),
            ]),
            checkpointer=InMemorySaver(),
        )
        reached_once = {
            "execution_order": ("work", "first", "work", "first"),
            "node_outputs": {"work": {"value": 1}},
            "node_routes": {},
        }
        reached_both = {
            **reached_once,
            "execution_order": ("work", "first", "second"),
        }

        with self.assertRaisesRegex(
            LangGraphCompletionUnsatisfied, "requires 2 .*reached 1",
        ):
            compiled._result(reached_once)
        self.assertEqual(1, compiled._result(reached_both)["result"])

    def test_a_run_input_is_accepted_under_the_entry_ports(self) -> None:
        """`inputs` is optional; the entry node's ports are the interface.

        The kernel delivers one input object to every entry node, the catalog
        reads those ports to decide a workflow is startable from a Goal, and
        the goal binding names one of them. Only this engine's validation read
        `ir.inputs` alone — so a definition without that optional block was
        advertised `ready`, and then refused the very input the catalog said
        to send. Every workflow an Agent wrote was one of those.
        """

        entry = node("entry", inputs=("prompt",), outputs=("value",))
        done = node("done", inputs=("value",), kind="terminal", handler=False)
        ir = workflow(
            (entry, done), (edge("finish", "entry", "done"),),
            entry=("entry",), terminals=("done",), result=("entry", "value"),
        )
        # An IR with no `inputs` block at all, which is what an Agent writes:
        # the helper above always declares one, so it must be stripped here.
        undeclared = WorkflowIR(
            ir.ir_version, ir.workflow_id, ir.name, ir.description, ir.labels,
            (), ir.outputs, ir.nodes, ir.edges, ir.entry, ir.terminals,
            ir.policies, ir.extensions, ir.indexes, ir.result,
        )
        self.assertEqual((), tuple(undeclared.inputs))
        compiled = compile_workflow(
            undeclared,
            LangGraphHandlerRegistry([
                binding("entry", lambda values, config, context: {
                    "value": values["prompt"]["goal"],
                }),
            ]),
            checkpointer=InMemorySaver(),
        )

        completed = compiled.invoke(
            {"prompt": {"goal": "ship it"}},
            config={"configurable": {"thread_id": "entry-ports"}},
        )
        self.assertEqual("ship it", completed["result"])

        with self.assertRaisesRegex(ValueError, r"unknown workflow inputs: \['nope'\]"):
            compiled.invoke(
                {"nope": 1}, config={"configurable": {"thread_id": "unknown"}},
            )

    def test_a_declared_inputs_block_still_wins(self) -> None:
        """The fallback is for definitions without one, not instead of one."""

        entry = node("entry", inputs=("prompt",), outputs=("value",))
        done = node("done", inputs=("value",), kind="terminal", handler=False)
        base = workflow(
            (entry, done), (edge("finish", "entry", "done"),),
            entry=("entry",), terminals=("done",), result=("entry", "value"),
        )
        declared = WorkflowIR(
            base.ir_version, base.workflow_id, base.name, base.description,
            base.labels, (port("prompt"),), base.outputs, base.nodes,
            base.edges, base.entry, base.terminals, base.policies,
            base.extensions, base.indexes, base.result,
        )
        compiled = compile_workflow(
            declared,
            LangGraphHandlerRegistry([
                binding("entry", lambda values, config, context: {"value": 1}),
            ]),
            checkpointer=InMemorySaver(),
        )
        with self.assertRaisesRegex(ValueError, "unknown workflow inputs"):
            compiled.invoke(
                {"other": 1}, config={"configurable": {"thread_id": "declared"}},
            )

    def test_a_join_waits_for_a_branch_that_is_still_coming(self) -> None:
        """A join was an ordinary node: whoever reached it scheduled it.

        Branches that finish in different supersteps therefore ran it once
        each — merging partial data the first time and re-running everything
        downstream the second. Observed against real Agents: a workflow whose
        merge step was an Agent action performed that merge twice.
        """

        calls = []

        def counted(name):
            def handler(values, config, context):
                calls.append(name)
                return {"value": name}
            return handler

        # `left` reaches the join *and* the node that also feeds it, so the
        # two arrivals land one superstep apart.
        fan = node(
            "fan", inputs=("value",), outputs=("value",), route_mode="parallel",
        )
        left = node(
            "left", inputs=("value",), outputs=("value",), route_mode="parallel",
        )
        right = node("right", inputs=("value",), outputs=("value",))
        policy = IRPolicy(
            "all", "join", {"mode": "all", "merge_mode": "object_by_edge"},
        )
        gather = node(
            "gather", inputs=("value",), outputs=("value",),
            kind="join", handler=False,
        )
        gather = IRNode(
            gather.id, gather.kind, gather.inputs, gather.outputs,
            gather.handler, gather.config, (policy.id,), gather.extension,
            gather.route_mode,
        )
        after = node("after", inputs=("value",), outputs=("value",))
        done = node("done", inputs=("value",), kind="terminal", handler=False)
        ir = workflow(
            (fan, left, right, gather, after, done),
            (
                edge("f_l", "fan", "left"),
                edge("l_r", "left", "right"),
                edge("l_j", "left", "gather"),
                edge("r_j", "right", "gather"),
                edge("j_a", "gather", "after"),
                edge("a_d", "after", "done"),
            ),
            entry=("fan",), terminals=("done",), result=("after", "value"),
            policies=(policy,),
        )
        compiled = compile_workflow(
            ir,
            LangGraphHandlerRegistry([
                binding("fan", lambda values, config, context: dict(values)),
                binding("left", counted("left")),
                binding("right", counted("right")),
                binding("after", counted("after")),
            ]),
            checkpointer=InMemorySaver(),
        )

        completed = compiled.invoke(
            {"value": "in"}, config={"configurable": {"thread_id": "waiting-join"}},
        )

        self.assertEqual(["left", "right", "after"], calls)
        self.assertEqual(
            ["fan", "left", "right", "gather", "after", "done"],
            list(completed["execution_order"]),
        )

    def test_a_join_is_not_stranded_by_the_branch_that_ruled_it_out(self) -> None:
        """Deferring a join is only half a rule.

        The branch that completes last decides whether the join may run — and
        that branch may route somewhere else entirely, in which case it never
        points at the join and would never schedule it. The join then holds an
        arrival nobody will act on and the run simply stops. So a router also
        schedules a join that has *become* ready, whether or not it has an
        edge to it.
        """

        first = node(
            "first", inputs=("value",), outputs=("value",), route_mode="parallel",
        )
        second = node("second", inputs=("value",), outputs=("value",))
        elsewhere = node("elsewhere", inputs=("value",), outputs=("value",))
        policy = IRPolicy(
            "all", "join", {"mode": "all", "merge_mode": "object_by_edge"},
        )
        gate = node(
            "gate", inputs=("value",), outputs=("value",),
            kind="join", handler=False,
        )
        gate = IRNode(
            gate.id, gate.kind, gate.inputs, gate.outputs, gate.handler,
            gate.config, (policy.id,), gate.extension, gate.route_mode,
        )
        done = node("done", inputs=("value",), kind="terminal", handler=False)
        ir = workflow(
            (first, second, elsewhere, gate, done),
            (
                edge("f_s", "first", "second"),
                edge("f_g", "first", "gate"),
                # `second` rules the join out and goes elsewhere, so it is the
                # last word on the join and points away from it.
                edge(
                    "s_g", "second", "gate",
                    condition={"op": "literal", "value": False},
                ),
                edge("s_e", "second", "elsewhere", priority=100),
                edge("g_d", "gate", "done"),
            ),
            entry=("first",), terminals=("done",), result=("gate", "value"),
            policies=(policy,),
        )
        compiled = compile_workflow(
            ir,
            LangGraphHandlerRegistry([
                binding("first", lambda values, config, context: {"value": "a"}),
                binding("second", lambda values, config, context: {"value": "b"}),
                binding("elsewhere", lambda values, config, context: {"value": "c"}),
            ]),
            checkpointer=InMemorySaver(),
        )

        completed = compiled.invoke(
            {"value": "in"}, config={"configurable": {"thread_id": "stranded-join"}},
        )

        order = list(completed["execution_order"])
        self.assertIn("gate", order)
        self.assertIn("done", order)

    def test_a_join_stops_waiting_for_a_branch_that_cannot_arrive(self) -> None:
        """Waiting for every branch is only right while one may still come.

        A join fed by an automatic path and a human path — the shape an Agent
        writes for "merge, or ask somebody" — has two feeders of which exactly
        one can ever run. Waiting for both would hang the run forever, which
        is why the rule is what may still arrive rather than what exists.
        """

        check = node("check", inputs=("value",), outputs=("value",))
        decide = node(
            "decide", inputs=("value",), outputs=("value",),
            kind="decision", handler=False,
        )
        auto = node("auto", inputs=("value",), outputs=("value",))
        manual = node("manual", inputs=("value",), outputs=("value",))
        policy = IRPolicy(
            "gate", "join", {"mode": "all", "merge_mode": "object_by_edge"},
        )
        gate = node(
            "gate", inputs=("value",), outputs=("value",),
            kind="join", handler=False,
        )
        gate = IRNode(
            gate.id, gate.kind, gate.inputs, gate.outputs, gate.handler,
            gate.config, (policy.id,), gate.extension, gate.route_mode,
        )
        merge = node("merge", inputs=("value",), outputs=("value",))
        done = node("done", inputs=("value",), kind="terminal", handler=False)
        ir = workflow(
            (check, decide, auto, manual, gate, merge, done),
            (
                edge("c_d", "check", "decide"),
                edge(
                    "d_auto", "decide", "auto",
                    condition={
                        "op": "eq",
                        "left": {"op": "ref", "path": "source.value"},
                        "right": {"op": "literal", "value": "clean"},
                    },
                ),
                edge("d_manual", "decide", "manual", priority=100),
                edge("auto_g", "auto", "gate"),
                edge("manual_g", "manual", "gate"),
                edge("g_m", "gate", "merge"),
                edge("m_d", "merge", "done"),
            ),
            entry=("check",), terminals=("done",), result=("merge", "value"),
            policies=(policy,),
        )
        compiled = compile_workflow(
            ir,
            LangGraphHandlerRegistry([
                binding("check", lambda values, config, context: {"value": "clean"}),
                binding("auto", lambda values, config, context: {"value": "auto"}),
                binding("manual", lambda values, config, context: {"value": "manual"}),
                binding("merge", lambda values, config, context: {"value": "merged"}),
            ]),
            checkpointer=InMemorySaver(),
        )

        completed = compiled.invoke(
            {"value": "in"}, config={"configurable": {"thread_id": "exclusive-join"}},
        )

        order = list(completed["execution_order"])
        self.assertNotIn("manual", order)
        self.assertEqual(
            ["check", "decide", "auto", "gate", "merge", "done"], order,
        )
        self.assertEqual("merged", completed["result"])

    def test_an_any_join_does_not_demand_the_branch_that_lost(self) -> None:
        """The join policy decides how many inputs must be there, not the port.

        `any` is satisfied by one branch and every other port is *meant* to be
        absent — but the per-port `required` flag was checked anyway, so the
        shape an Agent writes for "ask a person, or publish straight away"
        died at the join it was routed through. For `all` the same flag is
        wrong for a different reason: a branch an upstream decision ruled out
        can never arrive at any port.
        """

        check = node("check", inputs=("value",), outputs=("value",))
        decide = node(
            "decide", inputs=("value",), outputs=("value",),
            kind="decision", handler=False,
        )
        ask = node("ask", inputs=("value",), outputs=("value",))
        policy = IRPolicy(
            "either", "join", {"mode": "any", "merge_mode": "object_by_edge"},
        )
        gate = IRNode(
            "gate", "join",
            (port("approved"), port("direct")),
            (port("result"),),
            None, {}, (policy.id,), None, None,
        )
        done = node("done", inputs=("result",), kind="terminal", handler=False)
        ir = workflow(
            (check, decide, ask, gate, done),
            (
                edge("to_decide", "check", "decide"),
                edge(
                    "needs_person", "decide", "ask",
                    condition={
                        "op": "eq",
                        "left": {"op": "ref", "path": "source.value"},
                        "right": {"op": "literal", "value": "unsure"},
                    },
                ),
                edge("direct", "decide", "gate", target_port="direct", priority=100),
                edge("approved", "ask", "gate", target_port="approved"),
                edge(
                    "finish", "gate", "done",
                    source_port="result", target_port="result",
                ),
            ),
            entry=("check",), terminals=("done",), result=("gate", "result"),
            policies=(policy,),
        )
        compiled = compile_workflow(
            ir,
            LangGraphHandlerRegistry([
                binding("check", lambda values, config, context: {"value": "clear"}),
                binding("ask", lambda values, config, context: {"value": "approved"}),
            ]),
            checkpointer=InMemorySaver(),
        )

        completed = compiled.invoke(
            {"value": "in"}, config={"configurable": {"thread_id": "any-join"}},
        )

        order = list(completed["execution_order"])
        self.assertNotIn("ask", order)
        self.assertIn("gate", order)
        # Only the branch that ran is in the merge; the other is absent by
        # design rather than missing.
        self.assertEqual({"direct"}, set(completed["result"]))

    def test_a_join_with_several_inputs_and_one_output_produces_it(self) -> None:
        """A join is not required to name its output after an input.

        The merge policy combines the edges feeding each *input port*; nothing
        said how those ports become the output. A join whose output happened
        to share a name with an input worked, and one that did not produced
        nothing and failed as a Handler that had omitted its own declared
        output — after the branches feeding it had run. Written by an Agent
        asked to merge two checks into one report.
        """

        fan = node(
            "fan", inputs=("value",), outputs=("value",), route_mode="parallel",
        )
        facts = node("facts", inputs=("value",), outputs=("value",))
        tone = node("tone", inputs=("value",), outputs=("value",))
        policy = IRPolicy(
            "both", "join", {"mode": "all", "merge_mode": "object_by_edge"},
        )
        gather = IRNode(
            "gather", "join",
            (port("from_facts"), port("from_tone")),
            (port("result"),),
            None, {}, (policy.id,), None, None,
        )
        done = node("done", inputs=("result",), kind="terminal", handler=False)
        ir = workflow(
            (fan, facts, tone, gather, done),
            (
                edge("f_facts", "fan", "facts"),
                edge("f_tone", "fan", "tone"),
                edge("facts_in", "facts", "gather", target_port="from_facts"),
                edge("tone_in", "tone", "gather", target_port="from_tone"),
                edge(
                    "finish", "gather", "done",
                    source_port="result", target_port="result",
                ),
            ),
            entry=("fan",), terminals=("done",), result=("gather", "result"),
            policies=(policy,),
        )
        compiled = compile_workflow(
            ir,
            LangGraphHandlerRegistry([
                binding("fan", lambda values, config, context: dict(values)),
                binding("facts", lambda values, config, context: {"value": "checked"}),
                binding("tone", lambda values, config, context: {"value": "polite"}),
            ]),
            checkpointer=InMemorySaver(),
        )

        completed = compiled.invoke(
            {"value": "in"}, config={"configurable": {"thread_id": "wide-join"}},
        )

        # Keyed by the input port names the author gave each branch.
        self.assertEqual(
            {"from_facts", "from_tone"}, set(completed["result"]),
        )
        self.assertIn("done", completed["execution_order"])

    def test_a_terminal_may_carry_an_artifact_the_authoring_rules_demand(self) -> None:
        """The two layers were asking for opposite things.

        `MARKDOWN_ARTIFACT_REQUIRED` tells an author to declare the Goal
        result an `artifact_ref` and to carry the same policy to the terminal
        input. This engine then asked what the terminal's Handler supports —
        a terminal has none, the default was `inline`, and the workflow was
        refused for doing what it had been told. A node without a Handler
        carries its value through; there is nothing to ask.
        """

        writer = node(
            "writer", inputs=("prompt",), outputs=("result",),
        )
        done = node("done", inputs=("result",), kind="terminal", handler=False)
        base = workflow(
            (writer, done), (edge("finish", "writer", "done", source_port="result",
                                  target_port="result"),),
            entry=("writer",), terminals=("done",), result=("writer", "result"),
        )
        as_artifact = PortDataPolicy(
            transport=PortTransport.ARTIFACT_REF, max_size_bytes=4096,
            content_types=("text/markdown",),
        )

        def carrying(item):
            ports = tuple(
                IRPort(
                    port.id, port.schema_id, port.required, port.has_default,
                    port.default, port.description, as_artifact,
                )
                for port in item.inputs
            )
            outputs = tuple(
                IRPort(
                    port.id, port.schema_id, port.required, port.has_default,
                    port.default, port.description, as_artifact,
                )
                for port in item.outputs
            )
            return IRNode(
                item.id, item.kind, ports, outputs, item.handler, item.config,
                item.policies, item.extension, item.route_mode,
            )

        ir = WorkflowIR(
            base.ir_version, base.workflow_id, base.name, base.description,
            base.labels, base.inputs, base.outputs,
            tuple(carrying(item) for item in base.nodes),
            base.edges, base.entry, base.terminals, base.policies,
            base.extensions, base.indexes, base.result,
        )
        registry = LangGraphHandlerRegistry([BoundHandler(
            "writer", "1.0.0", FINGERPRINT,
            lambda values, config, context: {"result": "artifact:1"},
            supported_transports=frozenset({"inline", "artifact_ref"}),
        )])

        compiled = compile_workflow(ir, registry, checkpointer=InMemorySaver())

        self.assertEqual(
            {"writer", "done"}, {item.id for item in compiled.ir.nodes},
        )

    def test_a_decision_routes_without_a_handler(self) -> None:
        """The DSL refuses a handler on a decision; this engine required one.

        Two rules that cannot both be satisfied, so no workflow containing a
        decision node could run at all — and LangGraph is the only engine. It
        held because no test compiled a decision through here; it surfaced
        when an Agent, asked for a real workflow, wrote one that branched.
        """

        classify = node("classify", inputs=("value",), outputs=("value",))
        route = node(
            "route", inputs=("value",), outputs=("value",),
            kind="decision", handler=False,
        )
        urgent = node("urgent", inputs=("value",), outputs=("value",))
        backlog = node("backlog", inputs=("value",), outputs=("value",))
        done_urgent = node(
            "done_urgent", inputs=("value",), kind="terminal", handler=False
        )
        done_backlog = node(
            "done_backlog", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (classify, route, urgent, backlog, done_urgent, done_backlog),
            (
                edge("classified", "classify", "route"),
                edge(
                    "is_urgent", "route", "urgent",
                    condition={
                        "op": "eq",
                        "left": {"op": "ref", "path": "source.value"},
                        "right": {"op": "literal", "value": "urgent"},
                    },
                ),
                edge("otherwise", "route", "backlog", priority=100),
                edge("urgent_done", "urgent", "done_urgent"),
                edge("backlog_done", "backlog", "done_backlog"),
            ),
            entry=("classify",), terminals=("done_urgent", "done_backlog"),
            result=("route", "value"),
        )
        registry = LangGraphHandlerRegistry([
            binding("classify", lambda values, config, context: {"value": "urgent"}),
            binding("urgent", lambda values, config, context: {"value": "paged"}),
            binding("backlog", lambda values, config, context: {"value": "queued"}),
        ])

        compiled = compile_workflow(
            ir, registry, checkpointer=InMemorySaver(),
        )
        completed = compiled.invoke(
            {"value": "in"}, config={"configurable": {"thread_id": "decision"}},
        )

        # The decision passed its input through and the edges chose.
        self.assertIn("urgent", completed["execution_order"])
        self.assertNotIn("backlog", completed["execution_order"])
        self.assertEqual("paged", completed["node_outputs"]["urgent"]["value"])

    def test_deadline_join_fails_below_minimum(self) -> None:
        deadline = IRPolicy(
            "wait_for_two", "join",
            {
                "mode": "deadline", "merge_mode": "array_by_edge",
                "deadline_seconds": 10, "min_successful": 2,
            },
        )
        waiting = node(
            "waiting", inputs=("value",), outputs=("value",),
            kind="human", handler=False,
        )
        join = node(
            "join", inputs=("value",), outputs=("value",),
            kind="join", handler=False,
        )
        join = IRNode(
            join.id, join.kind, join.inputs, join.outputs, join.handler,
            join.config, (deadline.id,), join.extension, join.route_mode,
        )
        ir = workflow(
            (waiting, join),
            (edge("waiting_join", "waiting", "join"),),
            entry=("waiting",), terminals=("join",), result=("join", "value"),
            policies=(deadline,),
        )
        compiled = compile_workflow(
            ir, LangGraphHandlerRegistry([]), checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": "deadline-insufficient"}}
        compiled.invoke({"value": "start"}, config=config)

        with self.assertRaisesRegex(
            LangGraphJoinDeadlineExceeded, "timed out with 0/2 successes",
        ):
            compiled.fire_join_deadline("join", config=config)

    def test_join_all_uses_legacy_deterministic_merge_modes(self) -> None:
        fan = node(
            "fan", inputs=("value",), outputs=("value",), route_mode="parallel",
        )
        left = node("left", inputs=("value",), outputs=("value",))
        right = node("right", inputs=("value",), outputs=("value",))
        join_policy = IRPolicy(
            "join_all", "join",
            {"mode": "all", "merge_mode": "object_by_edge"},
        )
        join = IRNode(
            "join", "join", (port("items"),), (port("merged"),), None,
            {}, (join_policy.id,), None,
        )
        ir = WorkflowIR(
            "1.3", "workflow:join", "Join workflow", "", {},
            (port("value"),), (), (fan, left, right, join),
            (
                edge("to_left", "fan", "left"),
                edge("to_right", "fan", "right"),
                edge(
                    "left_join", "left", "join",
                    target_port="items", priority=1,
                ),
                edge(
                    "right_join", "right", "join",
                    target_port="items", priority=2,
                ),
            ),
            ("fan",), ("join",), (join_policy,), (), {},
            IRResult("join", "merged"),
        )
        registry = LangGraphHandlerRegistry([
            binding("fan", lambda values, config, context: values),
            binding("left", lambda values, config, context: {
                "value": values["value"] + "-left"
            }),
            binding("right", lambda values, config, context: {
                "value": values["value"] + "-right"
            }),
        ])

        result = compile_workflow(ir, registry).invoke({"value": "x"})

        self.assertEqual({
            "left_join": "x-left", "right_join": "x-right",
        }, result["result"])

    def test_join_winner_modes_follow_priority_order(self) -> None:
        fan = node(
            "fan", inputs=("value",), outputs=("value",), route_mode="parallel",
        )
        left = node("left", inputs=("value",), outputs=("value",))
        right = node("right", inputs=("value",), outputs=("value",))
        registry = LangGraphHandlerRegistry([
            binding("fan", lambda values, config, context: values),
            binding("left", lambda values, config, context: {"value": "left"}),
            binding("right", lambda values, config, context: {"value": "right"}),
        ])
        for mode, extra, expected in (
            ("any", {}, {"left_join": "left"}),
            ("n_of_m", {"threshold": 1}, {"left_join": "left"}),
            (
                "all_successful", {},
                {"left_join": "left", "right_join": "right"},
            ),
        ):
            with self.subTest(mode=mode):
                policy = IRPolicy(
                    "join_policy", "join",
                    {"mode": mode, "merge_mode": "object_by_edge", **extra},
                )
                join = IRNode(
                    "join", "join", (port("items"),), (port("merged"),),
                    None, {}, (policy.id,), None,
                )
                ir = WorkflowIR(
                    "1.3", f"workflow:join-{mode}", "Join", "", {},
                    (port("value"),), (), (fan, left, right, join),
                    (
                        edge("to_left", "fan", "left"),
                        edge("to_right", "fan", "right"),
                        edge(
                            "left_join", "left", "join",
                            target_port="items", priority=1,
                        ),
                        edge(
                            "right_join", "right", "join",
                            target_port="items", priority=2,
                        ),
                    ),
                    ("fan",), ("join",), (policy,), (), {},
                    IRResult("join", "merged"),
                )

                result = compile_workflow(ir, registry).invoke({"value": "x"})

                self.assertEqual(expected, result["result"])

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
                    policy_ref="bounded",
                ),
                edge("done", "count", "terminal", priority=10),
            ),
            entry=("count",),
            terminals=("terminal",),
            result=("count", "value"),
            policies=(IRPolicy("bounded", "loop", {"max_iterations": 2}),),
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

    def test_back_edge_supersedes_ingress_regardless_of_edge_id_order(self) -> None:
        start = node(
            "start",
            inputs=("value",),
            outputs=("value",),
        )
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
        # The back edge's id sorts before the ingress edge's id. The loop
        # input must still come from the back edge once a value exists.
        ir = workflow(
            (start, count, terminal),
            (
                edge(
                    "zz_start_count",
                    "start",
                    "count",
                    priority=0,
                ),
                edge(
                    "aa_again",
                    "count",
                    "count",
                    condition=less_than_three,
                    back_edge=True,
                    policy_ref="bounded",
                ),
                edge("done", "count", "terminal", priority=10),
            ),
            entry=("start",),
            terminals=("terminal",),
            result=("count", "value"),
            policies=(IRPolicy("bounded", "loop", {"max_iterations": 2}),),
        )
        registry = LangGraphHandlerRegistry([
            binding("start", lambda values, config, context: {"value": values["value"]}),
            binding("count", lambda values, config, context: {
                "value": values["value"] + 1
            }),
        ])

        result = compile_workflow(ir, registry).invoke({"value": 0})

        self.assertEqual(3, result["result"])
        self.assertEqual(
            ["start", "count", "count", "count", "terminal"],
            result["execution_order"],
        )

    def test_back_edge_loop_fails_after_max_iterations(self) -> None:
        count = node(
            "count",
            inputs=("value",),
            outputs=("value",),
            route_mode="exclusive",
        )
        ir = workflow(
            (count,),
            (
                edge(
                    "again",
                    "count",
                    "count",
                    back_edge=True,
                    policy_ref="bounded",
                ),
            ),
            entry=("count",),
            terminals=(),
            result=("count", "value"),
            policies=(IRPolicy("bounded", "loop", {"max_iterations": 2}),),
        )
        calls = 0

        def increment(values, config, context):
            nonlocal calls
            calls += 1
            return {"value": values["value"] + 1}

        registry = LangGraphHandlerRegistry([binding("count", increment)])

        with self.assertRaisesRegex(ValueError, "exceeded max_iterations"):
            compile_workflow(ir, registry).invoke({"value": 0})

        self.assertEqual(3, calls)

    def test_back_edge_rework_fails_after_max_generations(self) -> None:
        revise = node(
            "revise",
            inputs=("value",),
            outputs=("value",),
            route_mode="exclusive",
        )
        ir = workflow(
            (revise,),
            (edge(
                "revise_again",
                "revise",
                "revise",
                back_edge=True,
                policy_ref="bounded_rework",
            ),),
            entry=("revise",),
            terminals=(),
            result=("revise", "value"),
            policies=(IRPolicy(
                "bounded_rework", "rework", {"max_generations": 2},
            ),),
        )
        calls = 0

        def increment(values, config, context):
            nonlocal calls
            calls += 1
            return {"value": values["value"] + 1}

        registry = LangGraphHandlerRegistry([binding("revise", increment)])

        with self.assertRaisesRegex(ValueError, "exceeded max_generations"):
            compile_workflow(ir, registry).invoke({"value": 0})

        self.assertEqual(3, calls)

    def test_loop_exhaustion_selects_error_route(self) -> None:
        count = node(
            "count",
            inputs=("value",),
            outputs=("value",),
            route_mode="exclusive",
        )
        failed = node(
            "failed", inputs=("value",), kind="terminal", handler=False,
        )
        ir = workflow(
            (count, failed),
            (
                edge(
                    "again", "count", "count", back_edge=True,
                    policy_ref="bounded",
                ),
                edge(
                    "exhausted", "count", "failed", route="error",
                    priority=10,
                ),
            ),
            entry=("count",),
            terminals=("failed",),
            result=("count", "value"),
            policies=(IRPolicy(
                "bounded", "loop",
                {"max_iterations": 2, "exhaustion": "error_route"},
            ),),
        )
        registry = LangGraphHandlerRegistry([
            binding("count", lambda values, config, context: {
                "value": values["value"] + 1,
            }),
        ])

        result = compile_workflow(ir, registry).invoke({"value": 0})

        self.assertEqual(3, result["result"])
        self.assertEqual(
            ["count", "count", "count", "failed"],
            result["execution_order"],
        )


class LangGraphWorkflowServiceTests(unittest.TestCase):
    def test_parallel_interrupts_require_and_accept_explicit_id(self) -> None:
        fan = node(
            "fan", inputs=("value",), outputs=("value",), route_mode="parallel",
        )
        left = node(
            "left", inputs=("value",), outputs=("value",),
            kind="human", handler=False,
        )
        right = node(
            "right", inputs=("value",), outputs=("value",),
            kind="human", handler=False,
        )
        ir = workflow(
            (fan, left, right),
            (edge("to_left", "fan", "left"), edge("to_right", "fan", "right")),
            entry=("fan",), terminals=("left", "right"),
            result=("left", "value"),
            policies=(IRPolicy(
                "complete", "completion", {"required_terminal_count": 2},
            ),),
        )
        registry = LangGraphHandlerRegistry([
            binding("fan", lambda values, config, context: dict(values)),
        ])
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(
                directory, self.publish(directory, ir), registry,
            )
            started = service.start(
                ir.workflow_id, {"value": "start"},
                idempotency_key="parallel-start",
            )
            with self.assertRaisesRegex(ValueError, "interrupt_id is required"):
                service.resume(
                    started.run_id, "ambiguous",
                    expected_revision=started.revision,
                    idempotency_key="parallel-ambiguous",
                )
            left_interrupt = next(
                item for item in started.interrupts
                if item["value"]["node_id"] == "left"
            )
            remaining = service.resume(
                started.run_id, "left-approved",
                expected_revision=started.revision,
                idempotency_key="parallel-left",
                interrupt_id=left_interrupt["id"],
            )
            completed = service.resume(
                remaining.run_id, "right-approved",
                expected_revision=remaining.revision,
                idempotency_key="parallel-right",
                interrupt_id=remaining.interrupts[0]["id"],
            )

        self.assertEqual("interrupted", remaining.status)
        self.assertEqual(
            ["right"],
            [item["value"]["node_id"] for item in remaining.interrupts],
        )
        self.assertEqual("completed", completed.status)
        self.assertEqual("left-approved", completed.result)

    def test_answers_survive_a_process_that_stops_mid_resume(self) -> None:
        """The window between committing the last answer and using it.

        `resume` records the answer, marks the run running, commits, and only
        then hands the collected answers to the graph. A process that stopped
        in that window used to leave `running` with nothing recorded, so
        recovery re-entered with `invoke(None)`, the graph interrupted at the
        same nodes, and every answer gathered across the earlier partial
        resumes was gone — with no trace that anyone had ever answered.
        """

        fan = node(
            "fan", inputs=("value",), outputs=("value",), route_mode="parallel",
        )
        left = node(
            "left", inputs=("value",), outputs=("value",),
            kind="human", handler=False,
        )
        right = node(
            "right", inputs=("value",), outputs=("value",),
            kind="human", handler=False,
        )
        ir = workflow(
            (fan, left, right),
            (edge("to_left", "fan", "left"), edge("to_right", "fan", "right")),
            entry=("fan",), terminals=("left", "right"),
            result=("left", "value"),
            policies=(IRPolicy(
                "complete", "completion", {"required_terminal_count": 2},
            ),),
        )
        registry = LangGraphHandlerRegistry([
            binding("fan", lambda values, config, context: dict(values)),
        ])
        with tempfile.TemporaryDirectory() as directory:
            store = self.publish(directory, ir)
            service = self.service(directory, store, registry)
            started = service.start(
                ir.workflow_id, {"value": "start"}, idempotency_key="crash-start",
            )
            first = next(item for item in started.interrupts)
            partial = service.resume(
                started.run_id, "first-answer",
                expected_revision=started.revision,
                idempotency_key="crash-first", interrupt_id=first["id"],
            )
            self.assertEqual("interrupted", partial.status)

            # The last answer, but the process stops before the graph is
            # given them: the row is committed and `_execute` never runs.
            crashed = self.service(directory, store, registry)
            original = crashed._execute

            def stop_after_commit(*args, **kwargs):
                raise KeyboardInterrupt("process lost")

            crashed._execute = stop_after_commit
            with self.assertRaises(KeyboardInterrupt):
                crashed.resume(
                    partial.run_id, "second-answer",
                    expected_revision=partial.revision,
                    idempotency_key="crash-second",
                    interrupt_id=partial.interrupts[0]["id"],
                )

            # A new process picks it up. Both answers have to still be there.
            rebuilt = self.service(directory, store, registry)
            recovered = rebuilt.recover(partial.run_id)

        self.assertEqual("completed", recovered.status, recovered.error)
        self.assertEqual("first-answer", recovered.result)

    def test_deadline_join_timer_resumes_partial_checkpoint(self) -> None:
        deadline = IRPolicy(
            "wait_for_enough", "join",
            {
                "mode": "deadline", "merge_mode": "array_by_edge",
                "deadline_seconds": 10, "min_successful": 1,
            },
        )
        fan = node(
            "fan", inputs=("value",), outputs=("value",), route_mode="parallel",
        )
        fast = node("fast", inputs=("value",), outputs=("value",))
        waiting = node(
            "waiting", inputs=("value",), outputs=("value",),
            kind="human", handler=False,
        )
        join = node(
            "join", inputs=("value",), outputs=("value",),
            kind="join", handler=False,
        )
        join = IRNode(
            join.id, join.kind, join.inputs, join.outputs, join.handler,
            join.config, (deadline.id,), join.extension, join.route_mode,
        )
        review = node(
            "review", inputs=("value",), outputs=("value",),
            kind="human", handler=False,
        )
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False,
        )
        ir = workflow(
            (fan, fast, waiting, join, review, terminal),
            (
                edge("fan_fast", "fan", "fast"),
                edge("fan_waiting", "fan", "waiting"),
                edge("fast_join", "fast", "join"),
                edge("waiting_join", "waiting", "join"),
                edge("join_review", "join", "review"),
                edge("review_terminal", "review", "terminal"),
            ),
            entry=("fan",), terminals=("terminal",), result=("review", "value"),
            policies=(deadline,),
        )
        registry = LangGraphHandlerRegistry([
            binding("fan", lambda values, config, context: dict(values)),
            binding("fast", lambda values, config, context: {"value": "fast"}),
        ])
        now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
        with tempfile.TemporaryDirectory() as directory:
            store = self.publish(directory, ir)
            service = LangGraphWorkflowService(
                store, registry,
                run_db_path=Path(directory) / "runs.sqlite3",
                checkpoint_db_path=Path(directory) / "checkpoints.sqlite3",
                clock=lambda: now[0],
            )
            interrupted = service.start(
                ir.workflow_id, {"value": "start"},
                idempotency_key="deadline-start",
            )
            self.assertEqual((), service.recover_due())
            now[0] += timedelta(seconds=10)
            with sqlite3.connect(Path(directory) / "runs.sqlite3") as connection:
                connection.execute(
                    "UPDATE langgraph_runs SET status='running',"
                    "revision=revision+1 WHERE run_id=?", (interrupted.run_id,),
                )
                connection.execute(
                    "UPDATE langgraph_timers SET status='firing'"
                    " WHERE run_id=? AND purpose='join_deadline'",
                    (interrupted.run_id,),
                )
            rebuilt = LangGraphWorkflowService(
                store, registry,
                run_db_path=Path(directory) / "runs.sqlite3",
                checkpoint_db_path=Path(directory) / "checkpoints.sqlite3",
                clock=lambda: now[0],
            )
            reviewing = rebuilt.recover(interrupted.run_id)
            completed = service.resume(
                reviewing.run_id,
                "approved",
                expected_revision=reviewing.revision,
                idempotency_key="deadline-review",
            )
            raced = service.start(
                ir.workflow_id, {"value": "race"},
                idempotency_key="deadline-race-start",
            )
            now[0] += timedelta(seconds=10)

            def race(operation):
                try:
                    return operation()
                except (LangGraphRunConflict, ValueError) as exc:
                    return exc

            waiting_interrupt = next(
                item for item in raced.interrupts
                if item["value"]["node_id"] == "waiting"
            )
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = tuple(pool.map(
                    race,
                    (
                        lambda: service.recover(raced.run_id),
                        lambda: service.resume(
                            raced.run_id, "late-approval",
                            expected_revision=raced.revision,
                            idempotency_key="deadline-race-human",
                            interrupt_id=waiting_interrupt["id"],
                        ),
                    ),
                ))
            raced_after = service.get(raced.run_id)

        self.assertEqual("interrupted", interrupted.status)
        self.assertEqual("interrupted", reviewing.status)
        self.assertEqual("review", reviewing.interrupts[0]["value"]["node_id"])
        self.assertEqual("completed", completed.status)
        self.assertEqual("approved", completed.result)
        self.assertTrue(any(
            isinstance(item, (LangGraphRunConflict, ValueError)) for item in outcomes
        ))
        self.assertEqual("interrupted", raced_after.status)
        self.assertEqual("review", raced_after.interrupts[0]["value"]["node_id"])

    def test_retry_timer_is_durable_and_does_not_fire_early(self) -> None:
        retry = IRPolicy(
            "retry_network", "retry",
            {"max_attempts": 2, "backoff_seconds": [10]},
        )
        action = IRNode(
            "action", "action", (port("value"),), (port("value"),),
            IRHandlerRef("action", "1.0.0", FINGERPRINT), {},
            (retry.id,), None,
        )
        ir = WorkflowIR(
            "1.3", "workflow:retry", "Retry workflow", "", {},
            (port("value"),), (), (action,), (), ("action",), ("action",),
            (retry,), (), {}, IRResult("action", "value"),
        )
        calls = []

        def flaky(values, config, context):
            calls.append(context.attempt_id)
            if len(calls) == 1:
                raise LangGraphRetryableError("temporary")
            return {"value": values["value"] + 1}

        registry = LangGraphHandlerRegistry([BoundHandler(
            "action", "1.0.0", FINGERPRINT, flaky, retry_safe=True,
        )])
        now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
        with tempfile.TemporaryDirectory() as directory:
            store = self.publish(directory, ir)
            service = LangGraphWorkflowService(
                store, registry,
                run_db_path=Path(directory) / "runs.sqlite3",
                checkpoint_db_path=Path(directory) / "checkpoints.sqlite3",
                clock=lambda: now[0],
            )
            waiting = service.start(
                ir.workflow_id, {"value": 4}, idempotency_key="retry-start",
            )
            early = service.recover(waiting.run_id)
            now[0] += timedelta(seconds=10)
            rebuilt = LangGraphWorkflowService(
                store, registry,
                run_db_path=Path(directory) / "runs.sqlite3",
                checkpoint_db_path=Path(directory) / "checkpoints.sqlite3",
                clock=lambda: now[0],
            )
            completed = rebuilt.recover(waiting.run_id)

        self.assertEqual("waiting", waiting.status)
        self.assertEqual(waiting, early)
        self.assertEqual("completed", completed.status)
        self.assertEqual(5, completed.result)
        self.assertEqual(2, len(calls))

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
                connection.execute(
                    "CREATE TABLE langgraph_timers("
                    "timer_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,"
                    "node_id TEXT NOT NULL,attempt_number INTEGER NOT NULL,"
                    "due_at TEXT NOT NULL,status TEXT NOT NULL,"
                    "UNIQUE(run_id,node_id,attempt_number))"
                )
                connection.execute(
                    "INSERT INTO langgraph_timers VALUES "
                    "('timer:1','run:1','action',1,'2026-01-01T00:00:00Z',"
                    "'scheduled')"
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
                timer_columns = {
                    row[1] for row in connection.execute(
                        "PRAGMA table_info(langgraph_timers)"
                    )
                }
                timer = connection.execute(
                    "SELECT purpose,target_id FROM langgraph_timers"
                    " WHERE timer_id='timer:1'"
                ).fetchone()

        self.assertIn("input_json", columns)
        self.assertIn("interrupts_json", columns)
        self.assertIn("interrupt_responses_json", columns)
        self.assertTrue({"purpose", "target_id"}.issubset(timer_columns))
        self.assertEqual(("retry", "action"), timer)

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

    def test_each_loop_generation_gets_its_own_retry_budget(self) -> None:
        """`max_attempts` is what one attempt at a node may spend, per visit.

        The count ran over the whole run, so a retry-safe node inside a
        bounded loop that failed once per generation exhausted the budget
        after N generations and failed the run — each generation having had
        exactly one attempt.
        """

        from orbit.workflow.langgraph_runtime.compiler import (
            LangGraphRetryRequested,
        )

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteWorkflowVersionStore(Path(directory) / "workflows.sqlite3")
            service = self.service(directory, store, LangGraphHandlerRegistry([]))
            run_id = "langgraph_run:" + uuid.uuid4().hex
            with sqlite3.connect(service.run_db_path) as connection:
                connection.execute(
                    "INSERT INTO langgraph_runs(run_id,workflow_id,workflow_version,"
                    "status,revision,input_json,interrupts_json,"
                    "interrupt_responses_json,result_json,error,created_at,"
                    "updated_at,owner_actor) VALUES"
                    " (?,'workflow:looping',1,'running',1,'{}','[]','{}',"
                    "NULL,NULL,?,?,'test:owner')",
                    (
                        run_id, "2026-01-01T00:00:00.000000Z",
                        "2026-01-01T00:00:00.000000Z",
                    ),
                )

            def failed(generation: int):
                return LangGraphRetryRequested(
                    "work", f"langgraph_attempt:{run_id}:work:{generation}",
                    {"max_attempts": 2, "backoff_seconds": [0]},
                    RuntimeError("transient"), generation,
                )

            # One failure in each of three generations. With one shared budget
            # the second would already be the last.
            for generation in (1, 2, 3):
                settled = service._schedule_retry(run_id, failed(generation))
                self.assertEqual("waiting", settled.status)

            with sqlite3.connect(service.run_db_path) as connection:
                targets = sorted(
                    row[0] for row in connection.execute(
                        "SELECT target_id FROM langgraph_timers"
                        " WHERE run_id=? AND purpose='retry'", (run_id,),
                    )
                )
                attempts = [
                    row[0] for row in connection.execute(
                        "SELECT attempt_number FROM langgraph_timers"
                        " WHERE run_id=? AND purpose='retry'", (run_id,),
                    )
                ]

        self.assertEqual(["work#1", "work#2", "work#3"], targets)
        # Each generation's first attempt, not the run's first, second, third.
        self.assertEqual([1, 1, 1], attempts)

    def test_a_deadline_join_visited_twice_is_bounded_twice(self) -> None:
        """`_finish_timer` marks a timer fired rather than deleting it.

        The id and `attempt_number` were fixed for the whole run, so
        `INSERT OR IGNORE` discarded every later generation's timer: a
        deadline join inside a loop was bounded on its first visit and
        unbounded on every one after.
        """

        deadline = IRPolicy(
            "wait", "join",
            {
                "mode": "deadline", "merge_mode": "array_by_edge",
                "deadline_seconds": 60, "min_successful": 1,
            },
        )
        left = node(
            "left", inputs=("value",), outputs=("value",),
            kind="human", handler=False,
        )
        join = node(
            "join", inputs=("value",), outputs=("value",),
            kind="join", handler=False,
        )
        join = IRNode(
            join.id, join.kind, join.inputs, join.outputs, join.handler,
            join.config, (deadline.id,), join.extension, join.route_mode,
        )
        ir = workflow(
            (left, join), (edge("left_join", "left", "join"),),
            entry=("left",), terminals=("join",), result=("join", "value"),
            policies=(deadline,),
        )
        with tempfile.TemporaryDirectory() as directory:
            store = self.publish(directory, ir)
            service = self.service(directory, store, LangGraphHandlerRegistry([]))
            run = service.start(
                ir.workflow_id, {"value": "in"},
                idempotency_key="looping-deadline", actor="test:owner",
            )
            # The join is reached a second time, as a loop upstream would.
            service._schedule_join_deadlines(
                run.run_id, ir,
                available_outputs={},
                pending_nodes=frozenset({"left"}),
                execution_order=("left", "join"),
            )
            with sqlite3.connect(service.run_db_path) as connection:
                timers = sorted(
                    row[0] for row in connection.execute(
                        "SELECT attempt_number FROM langgraph_timers"
                        " WHERE run_id=? AND purpose='join_deadline'",
                        (run.run_id,),
                    )
                )

        self.assertEqual("interrupted", run.status)
        # One for the first visit, one for the second.
        self.assertEqual([1, 2], timers)

    def test_a_join_fed_only_by_human_branches_still_gets_its_deadline(self) -> None:
        """The one wait the deadline could not bound was the one it was for.

        Scheduling required a branch to have produced output. A join fed by
        human branches has none at the moment it interrupts, and scheduling
        only runs when an execute ends at an interrupt — which will not happen
        again without the external input the deadline exists to stop waiting
        for. So no timer row was ever written, and the run waited forever.
        """

        deadline = IRPolicy(
            "wait", "join",
            {
                "mode": "deadline", "merge_mode": "array_by_edge",
                "deadline_seconds": 60, "min_successful": 1,
            },
        )
        fan = node(
            "fan", inputs=("value",), outputs=("value",), route_mode="parallel",
        )
        left = node(
            "left", inputs=("value",), outputs=("value",),
            kind="human", handler=False,
        )
        right = node(
            "right", inputs=("value",), outputs=("value",),
            kind="human", handler=False,
        )
        join = node(
            "join", inputs=("value",), outputs=("value",),
            kind="join", handler=False,
        )
        join = IRNode(
            join.id, join.kind, join.inputs, join.outputs, join.handler,
            join.config, (deadline.id,), join.extension, join.route_mode,
        )
        ir = workflow(
            (fan, left, right, join),
            (
                edge("fan_left", "fan", "left"),
                edge("fan_right", "fan", "right"),
                edge("left_join", "left", "join"),
                edge("right_join", "right", "join"),
            ),
            entry=("fan",), terminals=("join",), result=("join", "value"),
            policies=(deadline,),
        )
        registry = LangGraphHandlerRegistry([
            binding("fan", lambda values, config, context: dict(values)),
        ])
        with tempfile.TemporaryDirectory() as directory:
            store = self.publish(directory, ir)
            service = self.service(directory, store, registry)
            run = service.start(
                ir.workflow_id, {"value": "in"},
                idempotency_key="human-deadline", actor="test:owner",
            )
            with sqlite3.connect(service.run_db_path) as connection:
                timers = [
                    (row[0], row[1]) for row in connection.execute(
                        "SELECT purpose,target_id FROM langgraph_timers"
                        " WHERE run_id=?", (run.run_id,),
                    )
                ]

        self.assertEqual("interrupted", run.status)
        self.assertEqual(2, len(run.interrupts))
        self.assertEqual([("join_deadline", "join")], timers)

    def test_a_deadline_fire_that_raises_settles_instead_of_re_firing(self) -> None:
        """Only the deadline's own exception used to be caught.

        Anything else the fire raised on the way through — a Handler that
        failed, an output the compiler refused — escaped with the run still
        `running` and the timer still `firing`. `recover_due` selects exactly
        that pair and routes it back into the fire, and the timer loop
        isolates the exception, so the run never reached a terminal status and
        every poll tried again. Verified as a loop: four ticks, four times
        stuck.
        """

        deadline = IRPolicy(
            "wait", "join",
            {
                "mode": "deadline", "merge_mode": "array_by_edge",
                "deadline_seconds": 1, "min_successful": 1,
            },
        )
        fan = node(
            "fan", inputs=("value",), outputs=("value",), route_mode="parallel",
        )
        fast = node("fast", inputs=("value",), outputs=("value",))
        waiting = node(
            "waiting", inputs=("value",), outputs=("value",),
            kind="human", handler=False,
        )
        join = node(
            "join", inputs=("value",), outputs=("value",),
            kind="join", handler=False,
        )
        join = IRNode(
            join.id, join.kind, join.inputs, join.outputs, join.handler,
            join.config, (deadline.id,), join.extension, join.route_mode,
        )
        after = node("after", inputs=("value",), outputs=("value",))
        ir = workflow(
            (fan, fast, waiting, join, after),
            (
                edge("fan_fast", "fan", "fast"),
                edge("fan_waiting", "fan", "waiting"),
                edge("fast_join", "fast", "join"),
                edge("waiting_join", "waiting", "join"),
                edge("join_after", "join", "after"),
            ),
            entry=("fan",), terminals=("after",), result=("after", "value"),
            policies=(deadline,),
        )

        def explode(values, config, context):
            raise RuntimeError("handler exploded during the deadline fire")

        registry = LangGraphHandlerRegistry([
            binding("fan", lambda values, config, context: dict(values)),
            binding("fast", lambda values, config, context: {"value": "fast"}),
            binding("after", explode),
        ])
        now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
        with tempfile.TemporaryDirectory() as directory:
            store = self.publish(directory, ir)
            service = LangGraphWorkflowService(
                store, registry,
                run_db_path=Path(directory) / "runs.sqlite3",
                checkpoint_db_path=Path(directory) / "checkpoints.sqlite3",
                clock=lambda: now[0],
            )
            started = service.start(
                ir.workflow_id, {"value": "in"},
                idempotency_key="refire", actor="test:owner",
            )
            self.assertEqual("interrupted", started.status)
            now[0] += timedelta(seconds=10)

            observed = []
            for _ in range(4):
                service.recover_due(on_error=lambda run_id, exc: None)
                observed.append(service.get(started.run_id).status)
            with sqlite3.connect(service.run_db_path) as connection:
                timers = [
                    row[0] for row in connection.execute(
                        "SELECT status FROM langgraph_timers WHERE run_id=?",
                        (started.run_id,),
                    )
                ]

        # Terminal on the first tick, and still terminal after three more.
        self.assertEqual(["failed"] * 4, observed)
        self.assertEqual(["fired"], timers)

    def test_recover_does_not_re_enter_a_run_this_process_is_running(self) -> None:
        """`running` cannot tell a stranded run from one in flight here.

        `start` drives the graph synchronously while the row already reads
        `running`, so an operator's recover landing in that window compiled
        and invoked the same thread_id a second time — and the attempt journal,
        finding the sibling's `started` row, settled a healthy run `unknown`.
        """

        action = node("action", inputs=("value",), outputs=("value",))
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (action, terminal), (edge("done", "action", "terminal"),),
            entry=("action",), terminals=("terminal",), result=("action", "value"),
        )
        recovered: list[str] = []
        released = threading.Event()

        def slow(values, config, context):
            # Inside the handler the run is `running` and this process is
            # driving it — exactly the window an operator can land in.
            recovered.append(service.recover(run_id_holder["id"]).status)
            released.set()
            return {"value": 7}

        registry = LangGraphHandlerRegistry([binding("action", slow)])
        run_id_holder = {"id": ""}
        with tempfile.TemporaryDirectory() as directory:
            store = self.publish(directory, ir)
            service = self.service(directory, store, registry)
            run_id_holder["id"] = "langgraph_run:" + uuid.uuid4().hex
            with sqlite3.connect(service.run_db_path) as connection:
                connection.execute(
                    "INSERT INTO langgraph_runs(run_id,workflow_id,workflow_version,"
                    "status,revision,input_json,interrupts_json,"
                    "interrupt_responses_json,result_json,error,created_at,"
                    "updated_at,owner_actor) VALUES"
                    " (?,?,?,'running',1,?,'[]','{}',NULL,NULL,?,?,'test:owner')",
                    (
                        run_id_holder["id"], ir.workflow_id, 1,
                        json.dumps({"value": 1}),
                        "2026-01-01T00:00:00.000000Z",
                        "2026-01-01T00:00:00.000000Z",
                    ),
                )
            settled = service.recover(run_id_holder["id"])

        self.assertTrue(released.is_set(), "the handler never ran")
        # The reentrant recover found the run already in flight here and left
        # it alone, so the real execution finished normally.
        self.assertEqual(["running"], recovered)
        self.assertEqual("completed", settled.status)
        self.assertEqual(7, settled.result)

    def test_compatibility_compiles_a_definition_once(self) -> None:
        """The catalog asks per workflow per GET, and the UI polls it.

        A full `StateGraph` build per workflow per poll is work whose answer
        cannot have changed: the definition is content-addressed, the compiler
        is fixed, and this service's Handler registry is sealed at
        composition. What must *not* be cached is the resolution of "latest" —
        answering that from a key taken before a publish would report the
        previous definition's verdict.
        """

        action = node("action", inputs=("value",), outputs=("value",))
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (action, terminal), (edge("done", "action", "terminal"),),
            entry=("action",), terminals=("terminal",), result=("action", "value"),
        )
        registry = LangGraphHandlerRegistry([
            binding("action", lambda values, config, context: dict(values)),
            binding("extra", lambda values, config, context: dict(values)),
        ])
        compiles: list[str] = []
        real = langgraph_service_module.compile_workflow

        def counted(compiled_ir, handlers, **kwargs):
            compiles.append(compiled_ir.workflow_id)
            return real(compiled_ir, handlers, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            store = self.publish(directory, ir)
            service = self.service(directory, store, registry)
            with patch.object(langgraph_service_module, "compile_workflow", counted):
                first = service.compatibility(ir.workflow_id)
                second = service.compatibility(ir.workflow_id)
                by_version = service.compatibility(ir.workflow_id, 1)
                self.assertEqual(1, len(compiles))

                # A second version, and "latest" now means that one.
                revised = workflow(
                    (
                        node("action", inputs=("value",), outputs=("value",)),
                        node("extra", inputs=("value",), outputs=("value",)),
                        terminal,
                    ),
                    (
                        edge("onward", "action", "extra"),
                        edge("done", "extra", "terminal"),
                    ),
                    entry=("action",), terminals=("terminal",),
                    result=("extra", "value"),
                )
                store.publish(
                    CompiledWorkflow(
                        revised, definition_hash(revised), "test",
                        "sha256:" + "d" * 64,
                    ),
                    expected_latest_version=1, source_format="json",
                    source_text="{}", actor="test:author", dsl_version="1.3",
                )
                latest = service.compatibility(ir.workflow_id)

        self.assertTrue(first["compatible"])
        self.assertEqual(first, second)
        self.assertEqual(first, by_version)
        self.assertEqual(1, first["workflow_version"])
        # Not the cached answer: a different definition, so a fresh compile.
        self.assertEqual(2, len(compiles))
        self.assertEqual(2, latest["workflow_version"])

    def test_an_incompatible_definition_is_not_recompiled_either(self) -> None:
        """A refusal is as immutable as an acceptance, and costs the same."""

        orphan = node("orphan", inputs=("value",), outputs=("value",))
        ir = workflow(
            (orphan,), (), entry=("orphan",), terminals=("orphan",),
            result=("orphan", "value"),
        )
        compiles: list[str] = []
        real = langgraph_service_module.compile_workflow

        def counted(compiled_ir, handlers, **kwargs):
            compiles.append(compiled_ir.workflow_id)
            return real(compiled_ir, handlers, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            store = self.publish(directory, ir)
            # No Handler registered for `orphan`, so it cannot compile.
            service = self.service(directory, store, LangGraphHandlerRegistry([]))
            with patch.object(langgraph_service_module, "compile_workflow", counted):
                first = service.compatibility(ir.workflow_id)
                second = service.compatibility(ir.workflow_id)

        self.assertFalse(first["compatible"])
        self.assertEqual("unsupported_workflow", first["reason"])
        self.assertEqual(first, second)
        self.assertEqual(1, len(compiles))

    def test_replay_derives_the_run_from_what_was_recorded(self) -> None:
        """Replay has a definition here, and it is not "run it again".

        `workflow/README.md`: a replay may read old state and recorded events
        and nothing else — no clock, no random, no Planner, Handler, Tool,
        HTTP or Artifact writer, and no persistent write of any kind. So this
        asserts what it produces *and* that producing it touched no Handler.
        """

        calls: list[str] = []
        first = node("first", inputs=("value",), outputs=("value",))
        second = node("second", inputs=("value",), outputs=("value",))
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (first, second, terminal),
            (
                edge("onward", "first", "second"),
                edge("done", "second", "terminal"),
            ),
            entry=("first",), terminals=("terminal",),
            result=("second", "value"),
        )

        def record(name):
            def handler(values, config, context):
                calls.append(name)
                return {"value": f"{name}:{values['value']}"}
            return handler

        registry = LangGraphHandlerRegistry([
            binding("first", record("first")), binding("second", record("second")),
        ])
        with tempfile.TemporaryDirectory() as directory:
            store = self.publish(directory, ir)
            service = self.service(directory, store, registry)
            run = service.start(
                ir.workflow_id, {"value": "in"},
                idempotency_key="replay-once", actor="test:owner",
            )
            self.assertEqual("completed", run.status)
            self.assertEqual(["first", "second"], calls)

            steps = service.replay(run.run_id, actor="test:owner")

            with self.subTest("no handler ran"):
                self.assertEqual(["first", "second"], calls)

            with self.subTest("forwards, because a derivation runs forwards"):
                orders = [step["execution_order"] for step in steps]
                self.assertEqual(sorted(orders, key=len), orders)
                self.assertEqual(
                    ["first", "second", "terminal"], orders[-1],
                )

            with self.subTest("each step carries what was recorded at it"):
                final = steps[-1]
                self.assertEqual(
                    {"first", "second", "terminal"}, set(final["node_outputs"])
                )
                self.assertEqual(
                    "second:first:in", final["node_outputs"]["second"]["value"],
                )
                self.assertTrue(all(step["checkpoint_id"] for step in steps))

            with self.subTest("nothing was written"):
                # The same derivation twice, because a replay that recorded
                # anything would not produce the same answer the second time.
                self.assertEqual(steps, service.replay(run.run_id))

            with self.subTest("somebody else's run cannot be replayed"):
                with self.assertRaisesRegex(LookupError, "not found"):
                    service.replay(run.run_id, actor="test:other")

            with self.subTest("the limit is bounded"):
                with self.assertRaisesRegex(ValueError, "between 1 and 500"):
                    service.replay(run.run_id, limit=0)
                self.assertEqual(1, len(service.replay(run.run_id, limit=1)))

    def test_replay_shows_the_writes_an_interrupt_left_pending(self) -> None:
        """The state a resume would continue from, which is not the checkpoint.

        A branch that finished inside an interrupted superstep is a pending
        write and nothing else. Leaving it out would draw a run as though that
        branch had never executed — the same omission that made a deadline
        join lose the branch it had just merged.
        """

        fan = node(
            "fan", inputs=("value",), outputs=("value",), route_mode="parallel",
        )
        fast = node("fast", inputs=("value",), outputs=("value",))
        waiting = node(
            "waiting", inputs=("value",), outputs=("value",),
            kind="human", handler=False,
        )
        join = node(
            "join", inputs=("value",), outputs=("value",),
            kind="join", handler=False,
        )
        policy = IRPolicy(
            "wait", "join",
            {"mode": "all", "merge_mode": "array_by_edge"},
        )
        join = IRNode(
            join.id, join.kind, join.inputs, join.outputs, join.handler,
            join.config, (policy.id,), join.extension, join.route_mode,
        )
        ir = workflow(
            (fan, fast, waiting, join),
            (
                edge("fan_fast", "fan", "fast"),
                edge("fan_waiting", "fan", "waiting"),
                edge("fast_join", "fast", "join"),
                edge("waiting_join", "waiting", "join"),
            ),
            entry=("fan",), terminals=("join",), result=("join", "value"),
            policies=(policy,),
        )
        registry = LangGraphHandlerRegistry([
            binding("fan", lambda values, config, context: dict(values)),
            binding("fast", lambda values, config, context: {"value": "fast"}),
        ])
        with tempfile.TemporaryDirectory() as directory:
            store = self.publish(directory, ir)
            service = self.service(directory, store, registry)
            run = service.start(
                ir.workflow_id, {"value": "in"},
                idempotency_key="replay-pending", actor="test:owner",
            )
            self.assertEqual("interrupted", run.status)

            latest = service.replay(run.run_id, actor="test:owner")[-1]

        self.assertNotIn("fast", latest["execution_order"])
        self.assertIn("node_outputs", latest["pending_writes"])
        self.assertIn("execution_order", latest["pending_writes"])

    def test_a_run_belongs_to_the_actor_that_started_it(self) -> None:
        """`owner_actor` was recorded, threaded into the graph config, and
        enforced on every Artifact read — and on no run read at all. A run's
        `interrupts` carry the node inputs a human is being asked about, and
        `result` is the workflow's output; both were readable by any actor
        holding a read scope, and any write scope could cancel or resume.
        """

        waiting = node(
            "waiting", inputs=("value",), outputs=("value",),
            kind="human", handler=False,
        )
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (waiting, terminal), (edge("done", "waiting", "terminal"),),
            entry=("waiting",), terminals=("terminal",),
            result=("waiting", "value"),
        )
        with tempfile.TemporaryDirectory() as directory:
            store = self.publish(directory, ir)
            service = self.service(directory, store, LangGraphHandlerRegistry([]))
            mine = service.start(
                ir.workflow_id, {"value": 1},
                idempotency_key="owner-mine", actor="test:owner",
            )
            theirs = service.start(
                ir.workflow_id, {"value": 2},
                idempotency_key="owner-theirs", actor="test:other",
            )

            with self.subTest("get"):
                self.assertEqual(
                    mine.run_id, service.get(mine.run_id, actor="test:owner").run_id,
                )
                with self.assertRaisesRegex(LookupError, "not found"):
                    service.get(theirs.run_id, actor="test:owner")

            with self.subTest("list"):
                self.assertEqual(
                    [mine.run_id],
                    [run.run_id for run in service.list_runs(actor="test:owner")],
                )

            with self.subTest("resume"):
                with self.assertRaisesRegex(LookupError, "not found"):
                    service.resume(
                        theirs.run_id, True,
                        expected_revision=theirs.revision,
                        idempotency_key="owner-resume", actor="test:owner",
                    )

            with self.subTest("cancel"):
                with self.assertRaisesRegex(LookupError, "not found"):
                    service.cancel(
                        theirs.run_id, expected_revision=theirs.revision,
                        idempotency_key="owner-cancel", actor="test:owner",
                    )
                self.assertEqual(
                    "interrupted", service.get(theirs.run_id).status,
                )

            with self.subTest("recovery still sees every run"):
                # Nothing inside the service passes an actor: recovery, timers
                # and settlement exist to act on runs nobody is asking about.
                self.assertEqual(
                    {mine.run_id, theirs.run_id},
                    {run.run_id for run in service.list_runs()},
                )

    def _two_stranded_runs(self, directory: str):
        """Two runs the database believes are still in flight.

        Returned in the order `recover_running` will visit them, so a test can
        poison the *first* one. Poisoning the last would pass whether or not
        the batch is isolated, and prove nothing.
        """

        action = node("action", inputs=("value",), outputs=("value",))
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (action, terminal), (edge("done", "action", "terminal"),),
            entry=("action",), terminals=("terminal",), result=("action", "value"),
        )
        registry = LangGraphHandlerRegistry(
            [binding("action", lambda values, config, context: {"value": 1})]
        )
        store = SQLiteWorkflowVersionStore(Path(directory) / "workflows.sqlite3")
        service = self.service(directory, store, registry)
        runs = [
            service.start_snapshot(
                f"template:stranded-{index}", ir, {"value": index},
                template_id=f"stranded-{index}", idempotency_key=f"key-{index}",
            )
            for index in range(2)
        ]
        with sqlite3.connect(service.run_db_path) as connection:
            connection.execute("UPDATE langgraph_runs SET status='running'")
            connection.commit()
        visiting = [item.run_id for item in service.list_runs(status="running")]
        self.assertEqual(
            sorted(item.run_id for item in runs), sorted(visiting)
        )
        return service, visiting

    def test_one_unrecoverable_run_does_not_strand_the_rest_of_the_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, run_ids = self._two_stranded_runs(directory)
            poisoned, healthy = run_ids
            observed: list[tuple[str, str]] = []
            original = service.recover

            def recover(run_id: str):
                if run_id == poisoned:
                    raise RuntimeError("handler build is gone")
                return original(run_id)

            service.recover = recover

            recovered = service.recover_running(
                on_error=lambda run_id, exc: observed.append((run_id, str(exc)))
            )

            self.assertEqual([healthy], [item.run_id for item in recovered])
            self.assertEqual([(poisoned, "handler build is gone")], observed)

    def test_recovery_without_an_observer_still_isolates_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, run_ids = self._two_stranded_runs(directory)
            poisoned, healthy = run_ids
            original = service.recover
            service.recover = lambda run_id: (
                original(run_id) if run_id != poisoned
                else (_ for _ in ()).throw(RuntimeError("gone"))
            )

            recovered = service.recover_running()

            self.assertEqual([healthy], [item.run_id for item in recovered])

    def test_an_observer_that_raises_cannot_undo_the_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, run_ids = self._two_stranded_runs(directory)
            poisoned, healthy = run_ids
            original = service.recover
            service.recover = lambda run_id: (
                original(run_id) if run_id != poisoned
                else (_ for _ in ()).throw(RuntimeError("gone"))
            )

            def hostile(run_id: str, exc: Exception) -> None:
                raise ValueError("the observer is broken too")

            recovered = service.recover_running(on_error=hostile)

            self.assertEqual([healthy], [item.run_id for item in recovered])

    def test_a_process_that_died_mid_run_leaves_it_recoverable(self) -> None:
        """The guarantee the durable engine exists for.

        The suite tested that one unrecoverable run does not strand the batch,
        and never that recovery *works*: that a second process, over the same
        databases, carries a run its predecessor left in flight to completion
        and finishes the work that was outstanding. The legacy engine claimed
        the same thing in an end-to-end test that raced — a graceful shutdown
        cancelled the running Handler, the attempt was recorded failed and the
        run went terminal, so there was nothing left to recover — and that
        engine is not the one `orbit serve` runs.
        """

        started = []
        first = node("first", inputs=("value",), outputs=("value",))
        second = node("second", inputs=("value",), outputs=("value",))
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (first, second, terminal),
            (edge("onward", "first", "second"), edge("done", "second", "terminal")),
            entry=("first",), terminals=("terminal",),
            result=("second", "value"),
        )

        def record(name):
            def handler(values, config, context):
                started.append(name)
                return {"value": f"{values['value']}:{name}"}
            return handler

        registry = LangGraphHandlerRegistry([
            binding("first", record("first")),
            binding("second", record("second")),
        ])
        with tempfile.TemporaryDirectory() as directory:
            store = self.publish(directory, ir)
            paths = {
                "run_db_path": Path(directory) / "runs.sqlite3",
                "checkpoint_db_path": Path(directory) / "checkpoints.sqlite3",
            }
            dying = LangGraphWorkflowService(store, registry, **paths)
            run = dying.start(
                ir.workflow_id, {"value": "in"},
                idempotency_key="lost-process", actor="test:owner",
            )
            self.assertEqual("completed", run.status)

            # The process died after its work was durable but before the row
            # was settled: exactly what a kill mid-superstep leaves behind.
            with sqlite3.connect(paths["run_db_path"]) as connection:
                connection.execute(
                    "UPDATE langgraph_runs SET status='running',result_json=NULL"
                    " WHERE run_id=?", (run.run_id,),
                )
            started.clear()

            # A second process, sharing nothing but the databases.
            successor = LangGraphWorkflowService(store, registry, **paths)
            recovered = successor.recover_running()

            self.assertEqual([run.run_id], [item.run_id for item in recovered])
            self.assertEqual("completed", recovered[0].status)
            self.assertEqual("in:first:second", recovered[0].result)
            # Nothing was done twice: the checkpoint already held both nodes,
            # so recovery had only to settle what was already true.
            self.assertEqual([], started)

    def test_recovery_finishes_the_work_that_was_outstanding(self) -> None:
        """Recovery is not only bookkeeping; it runs what had not run."""

        started = []
        waiting = node(
            "waiting", inputs=("value",), outputs=("value",),
            kind="human", handler=False,
        )
        after = node("after", inputs=("value",), outputs=("value",))
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (waiting, after, terminal),
            (edge("onward", "waiting", "after"), edge("done", "after", "terminal")),
            entry=("waiting",), terminals=("terminal",), result=("after", "value"),
        )

        def record(values, config, context):
            started.append("after")
            return {"value": "done"}

        registry = LangGraphHandlerRegistry([binding("after", record)])
        with tempfile.TemporaryDirectory() as directory:
            store = self.publish(directory, ir)
            paths = {
                "run_db_path": Path(directory) / "runs.sqlite3",
                "checkpoint_db_path": Path(directory) / "checkpoints.sqlite3",
            }
            dying = LangGraphWorkflowService(store, registry, **paths)
            run = dying.start(
                ir.workflow_id, {"value": "in"},
                idempotency_key="lost-mid-answer", actor="test:owner",
            )
            self.assertEqual("interrupted", run.status)
            answered = dying.resume(
                run.run_id, {"value": "approved"},
                expected_revision=run.revision,
                idempotency_key="answered", actor="test:owner",
            )
            self.assertEqual("completed", answered.status)

            # The answer was recorded and the graph consumed it, and then the
            # process is taken away before the row said so.
            with sqlite3.connect(paths["run_db_path"]) as connection:
                connection.execute(
                    "UPDATE langgraph_runs SET status='running',result_json=NULL"
                    " WHERE run_id=?", (run.run_id,),
                )
            started.clear()

            successor = LangGraphWorkflowService(store, registry, **paths)
            recovered = successor.recover_running()

        self.assertEqual(["completed"], [item.status for item in recovered])
        self.assertEqual("done", recovered[0].result)

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

    def test_template_run_persists_its_graph_without_a_workflow_version(self) -> None:
        action = node("action", inputs=("value",), outputs=("value",))
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (action, terminal), (edge("done", "action", "terminal"),),
            entry=("action",), terminals=("terminal",), result=("action", "value"),
        )
        registry = LangGraphHandlerRegistry([
            binding("action", lambda values, config, context: {
                "value": values["value"] + 1
            })
        ])
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteWorkflowVersionStore(Path(directory) / "workflows.sqlite3")
            service = self.service(directory, store, registry)
            run = service.start_snapshot(
                "template:direct", ir, {"value": 4}, template_id="direct",
                idempotency_key="template-once", actor="test:author",
            )
            with sqlite3.connect(service.run_db_path) as connection:
                row = connection.execute(
                    "SELECT workflow_version,template_id,graph_snapshot_json"
                    " FROM langgraph_runs WHERE run_id=?", (run.run_id,),
                ).fetchone()

        self.assertEqual("completed", run.status)
        self.assertEqual(5, run.result)
        self.assertEqual(0, row[0])
        self.assertEqual("direct", row[1])
        self.assertEqual(ir.workflow_id, json.loads(row[2])["workflow_id"])

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

    def test_template_run_resumes_from_its_snapshot_after_service_rebuild(self) -> None:
        action = node("action", inputs=("value",), outputs=("value",))
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (action, terminal), (edge("done", "action", "terminal"),),
            entry=("action",), terminals=("terminal",), result=("action", "value"),
        )

        def approval(values, config, context):
            accepted = interrupt("approve template")
            return {"value": values["value"] if accepted else -1}

        registry = LangGraphHandlerRegistry([binding("action", approval)])
        with tempfile.TemporaryDirectory() as directory:
            empty_store = SQLiteWorkflowVersionStore(
                Path(directory) / "empty-workflows.sqlite3"
            )
            first = self.service(directory, empty_store, registry)
            paused = first.start_snapshot(
                "template:review", ir, {"value": 7}, template_id="review",
                idempotency_key="template-interrupt", actor="test:author",
            )
            rebuilt = self.service(directory, empty_store, registry)
            completed = rebuilt.resume(
                paused.run_id, True, expected_revision=paused.revision,
                idempotency_key="template-resume",
            )

        self.assertEqual("interrupted", paused.status)
        self.assertEqual("completed", completed.status)
        self.assertEqual(7, completed.result)

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
    def registration(self, client, *, required_secrets=()) -> HandlerRegistration:
        manifest = HandlerManifest(
            "trusted_agent", "1.0.0", ("action",),
            {"value": SCHEMA}, {"value": SCHEMA},
            {"type": "object"}, ExecutionSafety.UNKNOWN_ON_LEASE_LOSS,
            ResourceProfile(1000, 1000, 0, 30, 0, "agent"),
            "schema://object/1.0", ("agent.invoke",),
            tuple(required_secrets), True, True,
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
                input_ports=(output,),
            ).read(artifact_id)

        self.assertEqual(b"agent document", content)
        self.assertEqual(first, replayed)
        self.assertEqual(1, len(client.requests))

    def test_artifact_flows_between_agent_nodes_in_one_graph(self) -> None:
        class PipelineClient(FakeAgentClient):
            def execute(self, request, context):
                self.requests.append(request)
                if ":produce:" in request.idempotency_key:
                    artifact_id = context.artifacts.write(
                        name="document", content=b"pipeline document",
                        content_type="application/octet-stream",
                    )
                    return AgentResponse(
                        {"document": {"artifact_id": artifact_id}}, None, None,
                        artifact_refs=(artifact_id,),
                    )
                reference = request.input["document"]["artifact_id"]
                content = context.artifacts.read(reference)
                return AgentResponse({"value": content.decode("utf-8")}, None, None)

        client = PipelineClient()
        registration = self.registration(client)
        reference = IRHandlerRef(
            registration.manifest.name, registration.manifest.version,
            registration.manifest.fingerprint,
        )
        document = artifact_port("document")
        produce = IRNode(
            "produce", "action", (port("value"),), (document,),
            reference, {}, (), None,
        )
        consume = IRNode(
            "consume", "action", (document,), (port("value"),),
            reference, {}, (), None,
        )
        ir = WorkflowIR(
            "1.3", "workflow:artifact-pipeline", "Artifact pipeline", "", {},
            (port("value"),), (), (produce, consume),
            (edge(
                "document", "produce", "consume",
                source_port="document", target_port="document",
            ),),
            ("produce",), ("consume",), (), (), {},
            IRResult("consume", "value"),
        )
        with tempfile.TemporaryDirectory() as directory:
            registry = trusted_handlers(
                [registration],
                attempt_db_path=Path(directory) / "runs.sqlite3",
            )
            result = compile_workflow(ir, registry).invoke({"value": "start"})

        self.assertEqual("pipeline document", result["result"])
        self.assertEqual(2, len(client.requests))

    def test_agent_secret_ref_is_scoped_and_never_enters_graph_result(self) -> None:
        class SecretClient(FakeAgentClient):
            def execute(self, request, context):
                self.requests.append(request)
                revealed = context.secrets.resolve("api_key").reveal()
                return AgentResponse(
                    {"value": "authorized" if revealed == "top-secret" else "bad"},
                    None, None,
                )

        client = SecretClient()
        registration = self.registration(client, required_secrets=("api_key",))
        credential = secret_port("credential")
        action = IRNode(
            "agent", "action", (credential,), (port("value"),),
            IRHandlerRef(
                registration.manifest.name, registration.manifest.version,
                registration.manifest.fingerprint,
            ),
            {}, (), None,
        )
        ir = WorkflowIR(
            "1.3", "workflow:secret", "Secret workflow", "", {},
            (credential,), (), (action,), (), ("agent",), ("agent",), (), (), {},
            IRResult("agent", "value"),
        )
        with tempfile.TemporaryDirectory() as directory:
            registry = trusted_handlers(
                [registration],
                attempt_db_path=Path(directory) / "runs.sqlite3",
                secret_values={"api_key": "top-secret"},
            )
            result = compile_workflow(ir, registry).invoke({
                "credential": {"logical_name": "api_key"},
            })

        self.assertEqual("authorized", result["result"])
        self.assertNotIn("top-secret", json.dumps(result))

    def test_agent_secret_value_cannot_enter_result_or_artifact(self) -> None:
        class LeakingClient(FakeAgentClient):
            def execute(self, request, context):
                secret = context.secrets.resolve("api_key").reveal()
                if request.config.get("artifact"):
                    context.artifacts.write(
                        name="value", content=secret.encode(),
                        content_type="application/octet-stream",
                    )
                return AgentResponse({"value": secret}, None, None)

        client = LeakingClient()
        registration = self.registration(client, required_secrets=("api_key",))
        with tempfile.TemporaryDirectory() as directory:
            registry = trusted_handlers(
                [registration],
                attempt_db_path=Path(directory) / "runs.sqlite3",
                secret_values={"api_key": "top-secret"},
            )
            bound = registry.resolve(self.bound_node(registration.manifest))
            context = LangGraphExecutionContext(
                "workflow:test", "agent", "langgraph_run:secret",
                "langgraph_attempt:secret:result",
            )
            with self.assertRaisesRegex(ValueError, "resolved Secret value"):
                bound.invoke({"value": "prompt"}, {}, context)
            artifact_context = LangGraphExecutionContext(
                "workflow:test", "agent", "langgraph_run:secret",
                "langgraph_attempt:secret:artifact", (),
                (to_primitive(artifact_port("value")),),
            )
            with self.assertRaisesRegex(ValueError, "resolved Secret value"):
                bound.invoke(
                    {"value": "prompt"}, {"artifact": True}, artifact_context,
                )

    def tool_registration(self, adapter, *, safety=ExecutionSafety.REPLAY_SAFE):
        tools = ToolRegistry()
        tools.register(ToolManifest(
            "example.read", "1.0.0", safety, {"value": SCHEMA},
            "schema://object/1.0", 30, True, True, True,
            ("workspace.read",), (),
        ), adapter)
        tools.seal()
        manifest = HandlerManifest(
            "tool", "1.0.0", ("action",), {"value": SCHEMA},
            {"value": SCHEMA}, {"type": "object"}, safety,
            ResourceProfile(0, 0, 0, 30, 0, "tool"),
            "schema://object/1.0", ("workspace.read",), (), True, True,
        )
        return HandlerRegistration(manifest, ToolHandler(tools), "tool@test")

    def test_tool_handler_is_validated_and_replayed_from_journal(self) -> None:
        class Adapter:
            def __init__(self): self.calls = 0
            def execute(self, request, context):
                self.calls += 1
                return ToolResult({"value": request.input["value"] + 1})
            def cancel(self, execution_ref, context): return None
            def recover(self, recovery_ref, context): return None

        adapter = Adapter()
        registration = self.tool_registration(adapter)
        with tempfile.TemporaryDirectory() as directory:
            registry = trusted_handlers(
                [registration],
                attempt_db_path=Path(directory) / "runs.sqlite3",
            )
            bound = registry.resolve(IRNode(
                "tool", "action", (port("value"),), (port("value"),),
                IRHandlerRef(
                    registration.manifest.name, registration.manifest.version,
                    registration.manifest.fingerprint,
                ),
                {
                    "tool_name": "example.read",
                    "tool_version": "1.0.0",
                }, (), None,
            ))
            context = LangGraphExecutionContext(
                "workflow:test", "tool", "langgraph_run:tool",
                "langgraph_attempt:tool:1",
            )
            config = {"tool_name": "example.read", "tool_version": "1.0.0"}
            first = bound.invoke({"value": 4}, config, context)
            replayed = bound.invoke({"value": 4}, config, context)

        self.assertEqual({"value": 5}, first)
        self.assertEqual(first, replayed)
        self.assertEqual(1, adapter.calls)

    def test_unknown_safety_tool_failure_is_not_reexecuted(self) -> None:
        class Adapter:
            def __init__(self): self.calls = 0
            def execute(self, request, context):
                self.calls += 1
                raise RuntimeError("lost after write")
            def cancel(self, execution_ref, context): return None
            def recover(self, recovery_ref, context): return None

        adapter = Adapter()
        registration = self.tool_registration(
            adapter, safety=ExecutionSafety.UNKNOWN_ON_LEASE_LOSS,
        )
        with tempfile.TemporaryDirectory() as directory:
            registry = trusted_handlers(
                [registration],
                attempt_db_path=Path(directory) / "runs.sqlite3",
            )
            node = IRNode(
                "tool", "action", (port("value"),), (port("value"),),
                IRHandlerRef(
                    registration.manifest.name, registration.manifest.version,
                    registration.manifest.fingerprint,
                ),
                {}, (), None,
            )
            bound = registry.resolve(node)
            context = LangGraphExecutionContext(
                "workflow:test", "tool", "langgraph_run:tool",
                "langgraph_attempt:tool:unknown",
            )
            config = {"tool_name": "example.read", "tool_version": "1.0.0"}
            with self.assertRaisesRegex(RuntimeError, "outcome is unknown"):
                bound.invoke({"value": 4}, config, context)
            with self.assertRaisesRegex(RuntimeError, "unknown outcome"):
                bound.invoke({"value": 4}, config, context)

        self.assertEqual(1, adapter.calls)

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

    def test_artifact_http_and_mcp_projections_expose_committed_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "langgraph-runs.sqlite3"
            artifacts = LangGraphArtifactStore(
                database, Path(directory) / "artifacts",
            )
            access = artifacts.access(
                run_id="langgraph_run:projection", node_id="agent",
                attempt_id="langgraph_attempt:projection",
                output_ports=(artifact_port("document"),), inputs={},
                actor="test:reader",
            )
            artifact_id = access.write(
                name="document", content=b"projected content",
                content_type="application/octet-stream",
            )
            artifacts.commit(access.produced_artifact_ids)
            service = LangGraphWorkflowService(
                object(), LangGraphHandlerRegistry([]),
                run_db_path=database,
                checkpoint_db_path=Path(directory) / "checkpoints.sqlite3",
                artifact_store=artifacts,
            )
            app = create_app(
                Path(directory) / "orbit.sqlite3",
                langgraph_service=service,
                authenticator=lambda request: request.headers.get("x-orbit-actor"),
                authorizer=Authorizer(lambda actor: (READ_SCOPE,)),
                worker_count=1,
            )
            with AsgiHarness(app) as client:
                listed = client.get(
                    "/api/v1/langgraph-artifacts?run_id=langgraph_run%3Aprojection",
                    actor="test:reader",
                )
                detail = client.get(
                    f"/api/v1/langgraph-artifacts/{artifact_id}",
                    actor="test:reader",
                )
                content = client.get(
                    f"/api/v1/langgraph-artifacts/{artifact_id}/content",
                    actor="test:reader",
                )
                lineage = client.get(
                    f"/api/v1/langgraph-artifacts/{artifact_id}/lineage",
                    actor="test:reader",
                )
                denied = client.get(
                    f"/api/v1/langgraph-artifacts/{artifact_id}",
                    actor="test:other",
                )
                hidden = client.get(
                    "/api/v1/langgraph-artifacts",
                    actor="test:other",
                )
                mcp = client.request(
                    "POST", "/mcp", actor="test:reader",
                    body={
                        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {
                            "name": "read_langgraph_artifact",
                            "arguments": {"artifact_id": artifact_id},
                        },
                    },
                ).json()

        self.assertEqual(200, listed.status_code, listed.text)
        self.assertEqual(
            artifact_id,
            listed.json()["data"]["artifacts"][0]["artifact_id"],
        )
        self.assertEqual(artifact_id, detail.json()["data"]["artifact_id"])
        self.assertEqual(b"projected content", content.content)
        self.assertEqual([], lineage.json()["data"]["derived_from"])
        self.assertEqual(404, denied.status_code)
        self.assertEqual([], hidden.json()["data"]["artifacts"])
        self.assertEqual(
            artifact_id,
            json.loads(mcp["result"]["content"][0]["text"])["artifact_id"],
        )

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

    def test_langgraph_only_composition_removes_legacy_execution_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = create_app(
                root / "orbit.sqlite3",
                langgraph_state_directory=root,
                legacy_execution=False,
                authenticator=lambda request: request.headers.get("x-orbit-actor"),
                authorizer=Authorizer(lambda actor: (
                    READ_SCOPE, WRITE_SCOPE, OPS_WRITE_SCOPE,
                )),
                worker_count=1,
            )
            with AsgiHarness(app) as client:
                legacy_runs = client.get("/api/v1/runs", actor="test:reader")
                legacy_inbox = client.get("/api/v1/inbox", actor="test:reader")
                langgraph_runs = client.get(
                    "/api/v1/langgraph-runs", actor="test:reader"
                )
                tools = client.request(
                    "POST", "/mcp", actor="test:reader",
                    body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                ).json()["result"]["tools"]
                loop_names = {loop.name for loop in app.state.runtime.loops}

        self.assertEqual(404, legacy_runs.status_code)
        self.assertEqual(404, legacy_inbox.status_code)
        self.assertEqual(200, langgraph_runs.status_code)
        tool_names = {item["name"] for item in tools}
        self.assertIn("start_run", tool_names)
        self.assertNotIn("start_langgraph_run", tool_names)
        self.assertNotIn("submit_human_task", tool_names)
        self.assertEqual({"langgraph-timer"}, loop_names)

    def test_capability_and_startup_recovery_follow_optional_wiring(self) -> None:
        class RecoveringService:
            def __init__(self) -> None:
                self.recoveries = 0

            def recover_running(self, *, on_error=None):
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

    def test_the_runtime_starts_even_when_startup_recovery_cannot_run(self) -> None:
        """A left-over run must not be able to stop the whole Runtime.

        `recover_running` isolates per-run failures itself; this covers the
        outer guard, for when recovery cannot get off the ground at all — a
        corrupt run database, a missing checkpoint file. Refusing to serve
        anything, including the half of the Runtime that has no LangGraph in
        it, is a worse outage than the one being reported.
        """

        class BrokenService:
            def recover_running(self, *, on_error=None):
                raise sqlite3.DatabaseError("run database is malformed")

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                Path(directory) / "orbit.sqlite3",
                langgraph_service=BrokenService(),
                authenticator=lambda request: request.headers.get("x-orbit-actor"),
                authorizer=Authorizer(lambda actor: (READ_SCOPE,)),
                worker_count=1,
            )
            with AsgiHarness(app) as client:
                self.assertEqual(
                    200,
                    client.get("/health/live", actor="test:reader").status_code,
                )
            failures = app.state.startup_recovery_failures
        self.assertEqual(1, len(failures))
        self.assertEqual("*", failures[0]["run_id"])
        self.assertIn("run database is malformed", failures[0]["error"])

    def test_a_clean_startup_records_no_recovery_failures(self) -> None:
        class RecoveringService:
            def recover_running(self, *, on_error=None):
                return ()

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                Path(directory) / "orbit.sqlite3",
                langgraph_service=RecoveringService(),
                authenticator=lambda request: request.headers.get("x-orbit-actor"),
                authorizer=Authorizer(lambda actor: (READ_SCOPE,)),
                worker_count=1,
            )
            with AsgiHarness(app):
                pass
            self.assertFalse(hasattr(app.state, "startup_recovery_failures"))

    def test_a_run_that_cannot_be_recovered_is_reported_not_fatal(self) -> None:
        """The isolated per-run path, seen from startup."""

        class PartialService:
            def recover_running(self, *, on_error=None):
                on_error("run:stuck", RuntimeError("handler build is gone"))
                return ()

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                Path(directory) / "orbit.sqlite3",
                langgraph_service=PartialService(),
                authenticator=lambda request: request.headers.get("x-orbit-actor"),
                authorizer=Authorizer(lambda actor: (READ_SCOPE,)),
                worker_count=1,
            )
            with AsgiHarness(app) as client:
                self.assertEqual(
                    200,
                    client.get("/health/live", actor="test:reader").status_code,
                )
            failures = app.state.startup_recovery_failures
        self.assertEqual(
            [{"run_id": "run:stuck", "error": "RuntimeError: handler build is gone"}],
            failures,
        )

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
                self.assertNotIn(
                    "run.start",
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

    def test_replay_is_a_read_on_both_doors_and_owner_scoped(self) -> None:
        """Replay reads recorded state, so it is offered on the read scope.

        It is forbidden to call a Handler or write anything — that is what
        replay means in this codebase — which is why it carries no command and
        needs no idempotency key. What it does need is the same ownership rule
        every other run read has: the recorded state is the run's inputs and
        outputs, node by node.
        """

        action = node("action", inputs=("value",), outputs=("value",))
        terminal = node(
            "terminal", inputs=("value",), kind="terminal", handler=False
        )
        ir = workflow(
            (action, terminal), (edge("done", "action", "terminal"),),
            entry=("action",), terminals=("terminal",), result=("action", "value"),
        )
        registry = LangGraphHandlerRegistry([
            binding("action", lambda values, config, context: {"value": 21}),
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
                    (READ_SCOPE,) if actor == "test:reader"
                    else (READ_SCOPE, WRITE_SCOPE, OPS_WRITE_SCOPE)
                )),
                worker_count=1,
            )
            with AsgiHarness(app) as client:
                run = client.post(
                    "/api/v1/langgraph-runs",
                    actor="test:operator", key="replay-start",
                    body={"workflow_id": ir.workflow_id, "input": {"value": 8}},
                ).json()["data"]["run"]
                replayed = client.get(
                    f"/api/v1/langgraph-runs/{run['run_id']}/replay",
                    actor="test:operator",
                )
                foreign = client.get(
                    f"/api/v1/langgraph-runs/{run['run_id']}/replay",
                    actor="test:reader",
                )
                bad_query = client.get(
                    f"/api/v1/langgraph-runs/{run['run_id']}/replay?depth=2",
                    actor="test:operator",
                )
                over_mcp = client.request(
                    "POST", "/mcp", actor="test:operator",
                    body={
                        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {
                            "name": "replay_langgraph_run",
                            "arguments": {"run_id": run["run_id"]},
                        },
                    },
                ).json()
                # A read-only actor: refused for whose run it is, never for
                # lacking a write scope. That is what puts replay on the read
                # side — it derives state and writes nothing.
                by_reader = client.request(
                    "POST", "/mcp", actor="test:reader",
                    body={
                        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {
                            "name": "replay_langgraph_run",
                            "arguments": {"run_id": run["run_id"]},
                        },
                    },
                ).json()

        self.assertEqual("completed", run["status"])
        self.assertEqual(200, replayed.status_code, replayed.text)
        steps = replayed.json()["data"]["steps"]
        self.assertTrue(steps)
        self.assertEqual(
            ["action", "terminal"], steps[-1]["execution_order"],
        )
        self.assertEqual(21, steps[-1]["node_outputs"]["action"]["value"])
        # Somebody else's run is not found, as it is on every other read.
        self.assertEqual(404, foreign.status_code)
        self.assertEqual(400, bad_query.status_code)
        self.assertEqual(
            steps,
            json.loads(over_mcp["result"]["content"][0]["text"])["steps"],
        )
        self.assertNotIn("lacks scope", json.dumps(by_reader))
        self.assertIn("not found", json.dumps(by_reader))

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
                # Owned by the reader, so it can see it — and holding only
                # READ_SCOPE, so it is offered nothing to do with it.
                own = service.start(
                    ir.workflow_id, {"value": 7},
                    idempotency_key="reader-own", actor="test:reader",
                )
                own_view = client.get(
                    f"/api/v1/langgraph-runs/{own.run_id}", actor="test:reader",
                ).json()["data"]
                # Somebody else's run, whose interrupts carry its node inputs.
                foreign_view = client.get(
                    f"/api/v1/langgraph-runs/{started['run_id']}",
                    actor="test:reader",
                )
                foreign_list = client.get(
                    "/api/v1/langgraph-runs", actor="test:reader",
                ).json()["data"]["runs"]
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
        self.assertEqual([], own_view["allowed_commands"])
        self.assertEqual(404, foreign_view.status_code)
        self.assertEqual(
            [own.run_id], [item["run_id"] for item in foreign_list],
        )
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
