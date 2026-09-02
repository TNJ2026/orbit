"""Where a run got to, derived from its definition and its checkpoint.

There is no step table. The definition says what the run could do, the
checkpoint says what it did, and the difference is the progress — so most of
what is worth testing here is whether the derivation tells the truth at the
moments it would be easiest to get wrong.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from types import SimpleNamespace
import unittest

from orbit.workflow.domain.definitions import (
    CompiledWorkflow, IRHandlerRef, IRNode, IRPolicy,
)
from orbit.workflow.domain.serialization import definition_hash
from orbit.workflow.langgraph_runtime import build_service
from orbit.workflow.langgraph_runtime import service as service_module
from orbit.workflow.langgraph_runtime.compiler import LangGraphHandlerRegistry
from orbit.workflow.langgraph_runtime.service import (
    BRANCH_VERDICTS, EDGE_STATUSES, LangGraphWorkflowService,
)
from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore

import tests.test_workflow_langgraph_runtime as engine_tests
from tests.test_web_composition import (
    publish_human_workflow, publish_linear_workflow, transform_registration,
)


def by_id(steps):
    return {step["node_id"]: step for step in steps}


class PublishedWorkflowStepTests(unittest.TestCase):
    """Against the shared human fixture: action → human → terminal."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "runtime.db"
        publish_human_workflow(self.path)
        self.engine = build_service(
            self.path, [transform_registration()],
            state_directory=Path(self.temp.name) / "langgraph",
        )

    def start(self):
        return self.engine.start(
            "workflow:human", {"value": 1}, idempotency_key="one", actor="local",
        )

    def answer(self, run):
        interrupt = run.interrupts[0]
        ports = interrupt["value"]["output_ports"]
        return self.engine.resume(
            run.run_id,
            value={
                port["id"]: {"decision": "approve", "value": None}
                for port in ports
            },
            expected_revision=run.revision, idempotency_key="answered",
            interrupt_id=interrupt["id"], actor="local",
        )

    def test_a_run_waiting_on_a_person_says_which_step_is_waiting(self) -> None:
        run = self.start()
        steps = by_id(self.engine.steps(run.run_id, actor="local"))
        self.assertEqual("succeeded", steps["transform"]["status"])
        self.assertEqual("waiting", steps["approve"]["status"])
        self.assertEqual("not_reached", steps["done"]["status"])

    def test_answering_moves_the_run_through_the_rest(self) -> None:
        run = self.start()
        self.answer(run)
        steps = by_id(self.engine.steps(run.run_id, actor="local"))
        self.assertEqual(
            {"succeeded"}, {step["status"] for step in steps.values()},
        )
        self.assertEqual(1, steps["approve"]["runs"])

    def test_a_step_carries_what_it_is_rather_than_only_that_it_ran(self) -> None:
        run = self.start()
        steps = by_id(self.engine.steps(run.run_id, actor="local"))
        self.assertEqual("human", steps["approve"]["kind"])
        self.assertIsNone(steps["approve"]["handler"])
        self.assertEqual("transform", steps["transform"]["handler"]["name"])
        # Timing comes from the attempt journal, which only a Handler with an
        # external effect keeps. A transform and a human node have none, and
        # say so rather than inventing a moment.
        self.assertIsNone(steps["transform"]["first_at"])
        self.assertIsNone(steps["approve"]["first_at"])

    def test_a_run_is_only_readable_by_whoever_started_it(self) -> None:
        run = self.start()
        with self.assertRaises(LookupError):
            self.engine.steps(run.run_id, actor="somebody-else")

    def test_the_order_is_the_one_the_catalog_and_the_canvas_use(self) -> None:
        """A node must not move between the picture and the progress."""

        run = self.start()
        self.assertEqual(
            ["transform", "approve", "done"],
            [step["node_id"] for step in self.engine.steps(run.run_id, actor="local")],
        )


class DerivedStatusTests(unittest.TestCase):
    """The shapes the derivation is easiest to get wrong on."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def service(self, ir, bindings) -> LangGraphWorkflowService:
        store = SQLiteWorkflowVersionStore(self.root / "workflows.sqlite3")
        store.publish(
            CompiledWorkflow(ir, definition_hash(ir), "test", "sha256:" + "c" * 64),
            expected_latest_version=0, source_format="json",
            source_text="{}", actor="test", dsl_version="1.3",
        )
        return LangGraphWorkflowService(
            store, LangGraphHandlerRegistry(bindings),
            run_db_path=self.root / "runs.sqlite3",
            checkpoint_db_path=self.root / "checkpoints.sqlite3",
        )

    def test_a_branch_that_finished_while_another_waits_is_not_called_pending(self) -> None:
        """The reason the projection reads the pending writes.

        A branch that completed inside the superstep another branch
        interrupted is only in the pending writes; the committed checkpoint
        still says it never ran. Reading the checkpoint alone shows a finished
        parallel branch as `not_reached` for as long as its sibling waits for
        a person — on exactly the runs somebody is watching.
        """

        fan = engine_tests.node(
            "fan", inputs=("value",), outputs=("value",), route_mode="parallel",
        )
        beside = engine_tests.node("beside", inputs=("value",), outputs=("value",))
        ask = engine_tests.node(
            "ask", inputs=("value",), outputs=("value",), kind="human", handler=False,
        )
        ir = engine_tests.workflow(
            (fan, beside, ask),
            (engine_tests.edge("f_b", "fan", "beside"),
             engine_tests.edge("f_a", "fan", "ask")),
            entry=("fan",), terminals=("beside", "ask"),
            result=("beside", "value"),
            policies=(IRPolicy(
                "complete", "completion", {"required_terminal_count": 2},
            ),),
        )
        service = self.service(ir, [
            engine_tests.binding("fan", lambda values, config, context: dict(values)),
            engine_tests.binding("beside", lambda values, config, context: dict(values)),
        ])
        run = service.start(
            ir.workflow_id, {"value": "x"}, idempotency_key="p", actor="local",
        )
        self.assertEqual("interrupted", run.status)
        steps = by_id(service.steps(run.run_id, actor="local"))
        self.assertEqual("succeeded", steps["beside"]["status"])
        self.assertEqual("waiting", steps["ask"]["status"])

    def test_harness_unknown_step_exposes_structured_reconciliation(self) -> None:
        delegated = engine_tests.node(
            "delegate", inputs=("value",), outputs=("value",),
        )
        ir = engine_tests.workflow(
            (delegated,), (), entry=("delegate",), terminals=("delegate",),
            result=("delegate", "value"),
        )
        service = self.service(ir, [
            engine_tests.binding(
                "delegate", lambda values, config, context: dict(values),
            ),
        ])
        run = service.start(
            ir.workflow_id, {"value": "x"},
            idempotency_key="harness-unknown", actor="local",
        )
        with service._connect() as connection:
            connection.execute(
                "INSERT INTO langgraph_handler_attempts"
                "(attempt_id,run_id,node_id,status,output_json,error,updated_at,"
                "handler_name,execution_ref,execution_owner)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "attempt:harness", run.run_id, "delegate", "unknown", None,
                    "UnknownExternalResultError", run.updated_at,
                    "harness.subagent", "harness_delegation:abc", "worker:lost",
                ),
            )
            connection.commit()

        step = by_id(service.steps(run.run_id, actor="local"))["delegate"]
        self.assertEqual("unknown", step["status"])
        self.assertEqual({
            "kind": "reconciliation_required",
            "delegation_id": "harness_delegation:abc",
        }, step["resolution"])

    def test_an_answered_parallel_interrupt_is_not_called_not_reached(self) -> None:
        fan = engine_tests.node(
            "fan", inputs=("value",), outputs=("value",), route_mode="parallel",
        )
        left = engine_tests.node(
            "left", inputs=("value",), outputs=("value",),
            kind="human", handler=False,
        )
        right = engine_tests.node(
            "right", inputs=("value",), outputs=("value",),
            kind="human", handler=False,
        )
        ir = engine_tests.workflow(
            (fan, left, right),
            (engine_tests.edge("f_l", "fan", "left"),
             engine_tests.edge("f_r", "fan", "right")),
            entry=("fan",), terminals=("left", "right"),
            result=("left", "value"),
            policies=(IRPolicy(
                "complete", "completion", {"required_terminal_count": 2},
            ),),
        )
        service = self.service(ir, [
            engine_tests.binding("fan", lambda values, config, context: dict(values)),
        ])
        run = service.start(
            ir.workflow_id, {"value": "x"}, idempotency_key="parallel",
            actor="local",
        )
        left_interrupt = next(
            item for item in run.interrupts
            if item["value"]["node_id"] == "left"
        )

        partial = service.resume(
            run.run_id, {"value": "approved"},
            expected_revision=run.revision, idempotency_key="answer-left",
            interrupt_id=left_interrupt["id"], actor="local",
        )
        steps = by_id(service.steps(partial.run_id, actor="local"))

        self.assertEqual("answered", steps["left"]["status"])
        self.assertEqual("waiting", steps["right"]["status"])

        rebuilt = LangGraphWorkflowService(
            service.workflow_versions, service.handlers,
            run_db_path=self.root / "runs.sqlite3",
            checkpoint_db_path=self.root / "checkpoints.sqlite3",
        )
        rebuilt_steps = by_id(rebuilt.steps(partial.run_id, actor="local"))
        self.assertEqual("answered", rebuilt_steps["left"]["status"])
        self.assertEqual("waiting", rebuilt_steps["right"]["status"])

    def test_a_node_that_ran_more_than_once_says_how_many_times(self) -> None:
        """A loop is one row with a count, not a row that hides its repeats."""

        visits = {"count": 0}

        def counting(values, config, context):
            visits["count"] += 1
            return {"value": visits["count"]}

        work = engine_tests.node("work", inputs=("value",), outputs=("value",))
        done = engine_tests.node(
            "done", inputs=("value",), kind="terminal", handler=False,
        )
        again = {
            "op": "lt",
            "left": {"op": "ref", "path": "source.value"},
            "right": {"op": "literal", "value": 3},
        }
        ir = engine_tests.workflow(
            (work, done),
            (engine_tests.edge(
                 "w_w", "work", "work", condition=again,
                 back_edge=True, policy_ref="loop",
             ),
             engine_tests.edge("w_d", "work", "done", priority=10)),
            entry=("work",), terminals=("done",), result=("work", "value"),
            policies=(IRPolicy("loop", "loop", {"max_iterations": 5}),),
        )
        service = self.service(ir, [engine_tests.binding("work", counting)])
        run = service.start(
            ir.workflow_id, {"value": {}}, idempotency_key="l", actor="local",
        )
        self.assertEqual("completed", run.status)
        steps = by_id(service.steps(run.run_id, actor="local"))
        self.assertEqual(3, visits["count"])
        self.assertEqual(3, steps["work"]["runs"])
        self.assertEqual("succeeded", steps["work"]["status"])

    def test_a_run_that_never_started_has_every_step_unreached(self) -> None:
        """No checkpoint at all is a shape the derivation must survive."""

        work = engine_tests.node("work", inputs=("value",), outputs=("value",))
        done = engine_tests.node(
            "done", inputs=("value",), kind="terminal", handler=False,
        )
        ir = engine_tests.workflow(
            (work, done), (engine_tests.edge("w_d", "work", "done"),),
            entry=("work",), terminals=("done",), result=("work", "value"),
        )
        service = self.service(ir, [
            engine_tests.binding("work", lambda values, config, context: dict(values)),
        ])
        run = service.start(
            ir.workflow_id, {"value": 1}, idempotency_key="n", actor="local",
        )
        # A run whose checkpoints are gone: recovery reads the same emptiness.
        (self.root / "checkpoints.sqlite3").unlink()
        steps = service.steps(run.run_id, actor="local")
        self.assertEqual(
            {"not_reached"}, {step["status"] for step in steps},
        )
        self.assertEqual([0, 0], [step["runs"] for step in steps])


class FailedStepTests(unittest.TestCase):
    """Failure is only knowable where an attempt was journalled."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_a_journalled_handler_that_failed_is_named(self) -> None:
        from orbit.workflow.langgraph_runtime.artifacts import LangGraphArtifactStore
        from orbit.workflow.langgraph_runtime.wiring import trusted_handlers

        class Adapter:
            def execute(self, request, context):
                raise RuntimeError("the tool refused")

            def cancel(self, execution_ref, context):
                return None

            def recover(self, recovery_ref, context):
                return None

        registration = engine_tests.LangGraphProductionWiringTests(
            "run"
        ).tool_registration(Adapter())
        manifest = registration.manifest
        node = IRNode(
            "tool", "action",
            (engine_tests.port("value"),), (engine_tests.port("value"),),
            IRHandlerRef(manifest.name, manifest.version, manifest.fingerprint),
            {"tool_name": "example.read", "tool_version": "1.0.0"}, (), None,
        )
        ir = engine_tests.workflow(
            (node, engine_tests.node(
                "done", inputs=("value",), kind="terminal", handler=False,
            )),
            (engine_tests.edge("t_d", "tool", "done"),),
            entry=("tool",), terminals=("done",), result=("tool", "value"),
        )
        store = SQLiteWorkflowVersionStore(self.root / "workflows.sqlite3")
        store.publish(
            CompiledWorkflow(ir, definition_hash(ir), "test", "sha256:" + "c" * 64),
            expected_latest_version=0, source_format="json",
            source_text="{}", actor="test", dsl_version="1.3",
        )
        runs = self.root / "runs.sqlite3"
        service = LangGraphWorkflowService(
            store, trusted_handlers([registration], attempt_db_path=runs),
            run_db_path=runs, checkpoint_db_path=self.root / "checkpoints.sqlite3",
            artifact_store=LangGraphArtifactStore(runs, self.root / "artifacts"),
        )
        # `_execute` settles the run before it re-raises, so the caller sees
        # the exception and the run is already recorded as failed.
        with self.assertRaises(Exception):
            service.start(
                ir.workflow_id, {"value": 1}, idempotency_key="f", actor="local",
            )
        run = service.list_runs()[0]
        self.assertEqual("failed", run.status)
        steps = by_id(service.steps(run.run_id, actor="local"))
        self.assertEqual("failed", steps["tool"]["status"])
        self.assertEqual("not_reached", steps["done"]["status"])

        # A failure that predates cancellation remains a failure. If the
        # attempt settles after the run was cancelled, it is the cancellation
        # outcome and must not be presented as a Handler failure.
        with sqlite3.connect(runs) as connection:
            connection.execute(
                "UPDATE langgraph_runs SET status='cancelled',updated_at=?"
                " WHERE run_id=?", ("9999-01-01T00:00:00Z", run.run_id),
            )
            connection.commit()
        self.assertEqual(
            "failed", by_id(service.steps(run.run_id, actor="local"))["tool"]["status"],
        )
        with sqlite3.connect(runs) as connection:
            connection.execute(
                "UPDATE langgraph_runs SET updated_at=? WHERE run_id=?",
                ("0001-01-01T00:00:00Z", run.run_id),
            )
            connection.commit()
        self.assertEqual(
            "cancelled",
            by_id(service.steps(run.run_id, actor="local"))["tool"]["status"],
        )
        # A Handler can race cancellation and return success after the run's
        # terminal cancellation write. That late result is discarded by the
        # run CAS, so the page must not resurrect the step as successful.
        with sqlite3.connect(runs) as connection:
            connection.execute(
                "UPDATE langgraph_handler_attempts SET status='succeeded',"
                "updated_at=? WHERE run_id=?",
                ("0002-01-01T00:00:00Z", run.run_id),
            )
            connection.commit()
        self.assertEqual(
            "cancelled",
            by_id(service.steps(run.run_id, actor="local"))["tool"]["status"],
        )


class ProgressIsObservableTests(unittest.TestCase):
    """Steps are only worth deriving if a page can see them arrive.

    Two things had to be true for that and neither was. The run executed on
    the event loop, so nothing was served while it worked; and the change
    marker was made of the run row alone, which is written once at the start
    and once at the end — so a page watching a long run polled a frozen
    cursor until it was over.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def slow_steps(self, count: int, seconds: float):
        from orbit.workflow.handlers.tools import ToolResult

        class Adapter:
            def execute(self, request, context):
                time.sleep(seconds)
                return ToolResult({"value": 1})

            def cancel(self, execution_ref, context):
                return None

            def recover(self, recovery_ref, context):
                return None

        registration = engine_tests.LangGraphProductionWiringTests(
            "run"
        ).tool_registration(Adapter())
        manifest = registration.manifest
        nodes = tuple(
            IRNode(
                f"step{index}", "action",
                (engine_tests.port("value"),), (engine_tests.port("value"),),
                IRHandlerRef(manifest.name, manifest.version, manifest.fingerprint),
                {"tool_name": "example.read", "tool_version": "1.0.0"}, (), None,
            )
            for index in range(count)
        )
        terminal = engine_tests.node(
            "done", inputs=("value",), kind="terminal", handler=False,
        )
        edges = tuple(
            engine_tests.edge(f"e{index}", f"step{index}", f"step{index + 1}")
            for index in range(count - 1)
        ) + (engine_tests.edge("last", f"step{count - 1}", "done"),)
        ir = engine_tests.workflow(
            (*nodes, terminal), edges,
            entry=("step0",), terminals=("done",),
            result=(f"step{count - 1}", "value"),
        )
        return registration, ir

    def test_the_page_that_started_it_is_given_the_run_not_the_outcome(self) -> None:
        """The case the whole feature exists for.

        A watcher could always poll a run somebody else started. The person
        who clicks start could not: the request that created the run only
        answered when the run was over, so by the time the page had an id
        there was nothing left to watch. Asking for the run rather than its
        outcome is what makes the common path the one that works.
        """

        from orbit.web.api_v1 import (
            Authorizer, OPS_READ_SCOPE, READ_SCOPE, WRITE_SCOPE,
        )
        from orbit.web.app import create_app
        from tests.test_web_composition import AsgiHarness

        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        registration, ir = self.slow_steps(2, 0.4)
        SQLiteWorkflowVersionStore(root / "runtime.db").publish(
            CompiledWorkflow(ir, definition_hash(ir), "test", "sha256:" + "c" * 64),
            expected_latest_version=0, source_format="json",
            source_text="{}", actor="test", dsl_version="1.3",
        )
        app = create_app(
            root / "runtime.db",
            handlers=[registration],
            schemas={"schema://object/1.0": {"type": "object"},
                     engine_tests.SCHEMA: {"type": "object"}},
            poll_seconds=0.01,
            authenticator=lambda request: "author",
            authorizer=Authorizer(
                lambda _actor: [READ_SCOPE, WRITE_SCOPE, OPS_READ_SCOPE]
            ),
            langgraph_state_directory=root / "langgraph",
        )
        with AsgiHarness(app) as client:
            began = time.monotonic()
            started = client.post(
                "/api/v1/langgraph-runs", actor="author", key="deferred-1",
                body={
                    "workflow_id": ir.workflow_id, "input": {"value": 1},
                    "wait": False,
                },
            )
            answered_in = time.monotonic() - began
            self.assertEqual(200, started.status_code, started.text)
            run = started.json()["data"]["run"]
            self.assertEqual("running", run["status"])
            # Two steps of 0.4s. Being handed the id before they are done is
            # the point; the bound is far from both numbers.
            self.assertLess(answered_in, 0.4, f"waited {answered_in:.2f}s for the id")

            steps = client.get(
                f"/api/v1/langgraph-runs/{run['run_id']}/steps", actor="author",
            ).json()["data"]["steps"]
            self.assertIn("not_reached", [step["status"] for step in steps])

            app.state.langgraph_service.wait_for_background(timeout=10)
            settled = client.get(
                f"/api/v1/langgraph-runs/{run['run_id']}", actor="author",
            ).json()["data"]
            self.assertEqual("completed", settled["status"])

    def test_waiting_is_still_what_a_caller_gets_unless_it_says_otherwise(self) -> None:
        """MCP and every embedder call the same command and want the answer."""

        from orbit.web.api_v1 import Authorizer, READ_SCOPE, WRITE_SCOPE
        from orbit.web.app import create_app
        from tests.test_web_composition import (
            AsgiHarness, SCHEMAS, publish_linear_workflow, transform_registration,
        )

        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        app = create_app(
            root / "runtime.db",
            handlers=[transform_registration()], schemas=SCHEMAS,
            poll_seconds=0.02,
            authenticator=lambda request: "author",
            authorizer=Authorizer(lambda _actor: [READ_SCOPE, WRITE_SCOPE]),
            langgraph_state_directory=root / "langgraph",
        )
        publish_linear_workflow(root / "runtime.db")
        with AsgiHarness(app) as client:
            for body, expected in (
                ({"workflow_id": "workflow:linear", "input": {"value": 1}},
                 "completed"),
                ({"workflow_id": "workflow:linear", "input": {"value": 1},
                  "wait": True}, "completed"),
            ):
                with self.subTest(body=sorted(body)):
                    response = client.post(
                        "/api/v1/langgraph-runs", actor="author",
                        key=f"wait-{len(body)}", body=body,
                    )
                    self.assertEqual(
                        expected, response.json()["data"]["run"]["status"],
                    )
            refused = client.post(
                "/api/v1/langgraph-runs", actor="author", key="wait-bad",
                body={"workflow_id": "workflow:linear", "input": {"value": 1},
                      "wait": "later"},
            )
            self.assertEqual(409, refused.status_code, refused.text)

    def deferring_service(self, seconds: float, **extra):
        store = SQLiteWorkflowVersionStore(self.root / "workflows.sqlite3")
        work = engine_tests.node("work", inputs=("value",), outputs=("value",))
        done = engine_tests.node(
            "done", inputs=("value",), kind="terminal", handler=False,
        )
        ir = engine_tests.workflow(
            (work, done), (engine_tests.edge("w_d", "work", "done"),),
            entry=("work",), terminals=("done",), result=("work", "value"),
        )
        store.publish(
            CompiledWorkflow(ir, definition_hash(ir), "test", "sha256:" + "c" * 64),
            expected_latest_version=0, source_format="json",
            source_text="{}", actor="test", dsl_version="1.3",
        )
        visits = {"count": 0}

        def slow(values, config, context):
            visits["count"] += 1
            time.sleep(seconds)
            return dict(values)

        service = LangGraphWorkflowService(
            store, LangGraphHandlerRegistry([engine_tests.binding("work", slow)]),
            run_db_path=self.root / "runs.sqlite3",
            checkpoint_db_path=self.root / "checkpoints.sqlite3",
            **extra,
        )
        self.addCleanup(service.wait_for_background, 10.0)
        return service, ir, visits

    def test_the_step_being_worked_on_says_so(self) -> None:
        """Otherwise the node in flight looks like one not yet started.

        A live timeline whose current step reads `not_reached` is worse than
        no timeline: it says the run has done nothing while an Agent is in the
        middle of the thing you are waiting for.
        """

        from orbit.web.api_v1 import Authorizer, READ_SCOPE, WRITE_SCOPE
        from orbit.web.app import create_app
        from tests.test_web_composition import AsgiHarness

        registration, ir = self.slow_steps(2, 0.5)
        SQLiteWorkflowVersionStore(self.root / "runtime.db").publish(
            CompiledWorkflow(ir, definition_hash(ir), "test", "sha256:" + "c" * 64),
            expected_latest_version=0, source_format="json",
            source_text="{}", actor="test", dsl_version="1.3",
        )
        app = create_app(
            self.root / "runtime.db",
            handlers=[registration],
            schemas={"schema://object/1.0": {"type": "object"},
                     engine_tests.SCHEMA: {"type": "object"}},
            poll_seconds=0.01,
            authenticator=lambda request: "author",
            authorizer=Authorizer(lambda _actor: [READ_SCOPE, WRITE_SCOPE]),
            langgraph_state_directory=self.root / "langgraph",
        )
        with AsgiHarness(app) as client:
            started = client.post(
                "/api/v1/langgraph-runs", actor="author", key="running-1",
                body={"workflow_id": ir.workflow_id, "input": {"value": 1},
                      "wait": False},
            )
            run_id = started.json()["data"]["run"]["run_id"]
            time.sleep(0.2)
            steps = client.get(
                f"/api/v1/langgraph-runs/{run_id}/steps", actor="author",
            ).json()["data"]["steps"]
            app.state.langgraph_service.wait_for_background(timeout=10)

        statuses = {step["node_id"]: step["status"] for step in steps}
        self.assertEqual("running", statuses["step0"])
        self.assertEqual("not_reached", statuses["step1"])

    def test_replaying_a_deferred_start_does_not_execute_it_twice(self) -> None:
        """Two threads on one run would both write its checkpoints.

        The receipt is committed before the run is scheduled, so a repeat
        returns from there and never reaches the scheduling — but nothing
        about that is obvious from either half on its own.
        """

        service, ir, visits = self.deferring_service(0.5)
        first = service.start(
            ir.workflow_id, {"value": 1}, idempotency_key="same",
            actor="local", wait=False,
        )
        again = service.start(
            ir.workflow_id, {"value": 1}, idempotency_key="same",
            actor="local", wait=False,
        )
        self.assertEqual(first.run_id, again.run_id)
        service.wait_for_background(timeout=10)
        self.assertEqual(1, visits["count"])
        self.assertEqual("completed", service.get(first.run_id).status)

    def test_the_single_goal_slot_is_held_by_a_run_nobody_is_waiting_for(self) -> None:
        from orbit.workflow.langgraph_runtime.service import ActiveGoalExists

        service, ir, _visits = self.deferring_service(0.5, single_goal=True)
        service.start(
            ir.workflow_id, {"value": 1}, idempotency_key="one",
            actor="local", wait=False, goal="the first",
        )
        with self.assertRaises(ActiveGoalExists) as caught:
            service.start(
                ir.workflow_id, {"value": 2}, idempotency_key="two",
                actor="local", wait=False,
            )
        self.assertEqual("the first", caught.exception.active_goal["goal"])

    def test_the_single_goal_slot_belongs_to_the_workspace_not_the_caller(self) -> None:
        """One goal at a time here means here, not per caller.

        The slot was per-actor because a Run belonging to somebody else could
        not be opened to see why it was blocking. Reads stopped being scoped
        that way, and now writes have too: the Run holding the slot is visible
        to whoever is refused, and cancellable by them.
        """

        from orbit.workflow.langgraph_runtime.service import ActiveGoalExists

        service, ir, _visits = self.deferring_service(0.5, single_goal=True)
        service.start(
            ir.workflow_id, {"value": 1}, idempotency_key="one",
            actor="harness:session:first", wait=False, goal="the first",
        )
        with self.assertRaises(ActiveGoalExists) as caught:
            service.start(
                ir.workflow_id, {"value": 2}, idempotency_key="two",
                actor="harness:session:second", wait=False,
            )
        self.assertEqual("the first", caught.exception.active_goal["goal"])

    def test_a_run_started_elsewhere_can_still_be_cancelled(self) -> None:
        """The Session that started a Run is not the only one that may end it.

        A Run parked on a human step outlives the Session that started it, and
        the panel that draws it is a view of the Workspace. While `cancel`
        filtered by owner, the Runtime answered "not found" for a Run the
        person was looking at, and one whose Session had ended could not be
        answered by anybody — recoverable only by editing SQLite.
        """

        service, ir, _visits = self.deferring_service(0.5)
        run = service.start(
            ir.workflow_id, {"value": 1}, idempotency_key="started",
            actor="harness:session:gone", wait=False, goal="left behind",
        )
        cancelled = service.cancel(
            run.run_id, expected_revision=run.revision,
            idempotency_key="ended-by-another", actor=None,
        )
        self.assertEqual("cancelled", cancelled.status)

    def test_a_shutdown_waits_for_runs_nobody_else_is_waiting_for(self) -> None:
        service, ir, _visits = self.deferring_service(0.3)
        run = service.start(
            ir.workflow_id, {"value": 1}, idempotency_key="drain",
            actor="local", wait=False,
        )
        self.assertEqual("running", run.status)
        self.assertEqual((), service.wait_for_background(timeout=10))
        self.assertEqual("completed", service.get(run.run_id).status)

    def test_a_watcher_sees_the_run_partly_done_rather_than_only_finished(self) -> None:
        """The property the marker exists for, asserted on what a page reads.

        Counting cursor moves would pass without the change: the run row is
        written at the start and at the end, so a marker made of it alone
        already moves twice. What it cannot do is move *between* them, and
        that is what a watcher polling the steps of a working run sees.
        """

        from orbit.web.api_v1 import (
            Authorizer, OPS_READ_SCOPE, READ_SCOPE, WRITE_SCOPE,
        )
        from orbit.web.app import create_app
        from tests.test_web_composition import AsgiHarness

        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        registration, ir = self.slow_steps(3, 0.4)
        SQLiteWorkflowVersionStore(root / "runtime.db").publish(
            CompiledWorkflow(ir, definition_hash(ir), "test", "sha256:" + "c" * 64),
            expected_latest_version=0, source_format="json",
            source_text="{}", actor="test", dsl_version="1.3",
        )
        app = create_app(
            root / "runtime.db",
            handlers=[registration],
            schemas={"schema://object/1.0": {"type": "object"},
                     engine_tests.SCHEMA: {"type": "object"}},
            poll_seconds=0.01,
            authenticator=lambda request: "author",
            authorizer=Authorizer(
                lambda _actor: [READ_SCOPE, WRITE_SCOPE, OPS_READ_SCOPE]
            ),
            langgraph_state_directory=root / "langgraph",
        )
        running = {"value": True}

        with AsgiHarness(app) as client:
            async def watch():
                """A page that did not start the run, reading where it is.

                It finds the run by listing, because the row is written before
                execution begins — the caller that started it is still waiting
                for its own POST and has no id yet to watch by.
                """

                seen = []
                while running["value"]:
                    await asyncio.sleep(0.1)
                    listed = (await client.arequest(
                        "GET", "/api/v1/langgraph-runs", actor="author",
                    )).json()["data"]["runs"]
                    if not listed:
                        continue
                    steps = (await client.arequest(
                        "GET",
                        f"/api/v1/langgraph-runs/{listed[0]['run_id']}/steps",
                        actor="author",
                    )).json()["data"]["steps"]
                    seen.append(tuple(step["status"] for step in steps))
                return seen

            async def scenario():
                watcher = asyncio.ensure_future(watch())
                await asyncio.sleep(0.1)
                try:
                    return await client.arequest(
                        "POST", "/api/v1/langgraph-runs", actor="author",
                        headers={"idempotency-key": "progress-1"},
                        body={"workflow_id": ir.workflow_id, "input": {"value": 1}},
                    ), watcher
                finally:
                    running["value"] = False

            started, watcher = client.gather(scenario())[0]
            seen = client.gather(watcher)[0]

        self.assertEqual(200, started.status_code, started.text)
        self.assertEqual("completed", started.json()["data"]["run"]["status"])
        partly = [
            statuses for statuses in seen
            if "succeeded" in statuses and "not_reached" in statuses
        ]
        self.assertTrue(
            partly,
            f"never saw the run partly done; readings were {sorted(set(seen))}",
        )


class RetentionTests(unittest.TestCase):
    """Forgetting a run, and the things that must survive it.

    The three stores grow with every run and nothing ever removed anything.
    A console is the bulk of it — measured at roughly 316KB a run against
    37KB of checkpoints for a chatty six-step workflow — so the growth is
    real, and so is the risk of a policy that takes the wrong thing.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "runtime.db"
        publish_linear_workflow(self.path)
        publish_human_workflow(self.path)
        self.state = Path(self.temp.name) / "langgraph"
        self.engine = build_service(
            self.path, [transform_registration()], state_directory=self.state,
        )

    def finished(self, key: str):
        return self.engine.start(
            "workflow:linear", {"value": 1}, idempotency_key=key, actor="local",
        )

    def waiting(self, key: str):
        return self.engine.start(
            "workflow:human", {"value": 1}, idempotency_key=key, actor="local",
        )

    def rows(self, table: str) -> int:
        connection = sqlite3.connect(self.state / "langgraph-runs.sqlite3")
        try:
            return int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
        finally:
            connection.close()

    def test_a_finished_run_is_forgotten_whole(self) -> None:
        """Whole, because half of one lies about itself.

        A run without its console says "nothing printed" about a Handler that
        printed plenty; one without its checkpoints has a steps panel calling
        it "not started" beside a hero calling it completed. A run that is
        gone says neither.
        """

        run = self.finished("old")
        self.assertEqual({"runs", "run_ids", "artifacts", "pruned"},
                         set(self.engine.prune(before="2999-01-01")))
        self.assertEqual([], list(self.engine.list_runs()))
        for table in (
            "langgraph_run_events", "langgraph_handler_attempts",
            "langgraph_attempt_output", "langgraph_run_receipts",
        ):
            with self.subTest(table=table):
                self.assertEqual(0, self.rows(table))
        with self.assertRaises(LookupError):
            self.engine.get(run.run_id)

    def test_the_checkpoints_go_with_it(self) -> None:
        """They are the heaviest half of a run that no longer exists."""

        run = self.finished("old")
        self.engine.prune(before="2999-01-01")
        connection = sqlite3.connect(self.state / "langgraph-checkpoints.sqlite3")
        try:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE thread_id=?",
                (run.run_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(0, remaining)

    def test_a_run_that_can_still_do_something_is_never_taken(self) -> None:
        waiting = self.waiting("live")
        self.finished("old")
        self.engine.prune(before="2999-01-01")
        self.assertEqual(
            [waiting.run_id], [run.run_id for run in self.engine.list_runs()],
        )

    def test_an_unknown_outcome_is_kept_on_purpose(self) -> None:
        """Its console is the only account of what happened.

        Nobody can say whether a Handler that ended `unknown` acted, so the
        run is not resumable and not prunable either — throwing it away
        destroys the one record of the case most worth reading.
        """

        run = self.finished("mystery")
        with self.engine._connect() as connection:
            connection.execute(
                "UPDATE langgraph_runs SET status='unknown' WHERE run_id=?",
                (run.run_id,),
            )
            connection.commit()
        self.engine.prune(before="2999-01-01")
        self.assertEqual("unknown", self.engine.get(run.run_id).status)

    def test_a_finished_run_carries_no_live_workspace_refs(self) -> None:
        """Prunable the moment it settles: nothing can ever acquire its
        workspace grant again, so the cleanup loop may reclaim it without
        waiting for `run_retention_days` to also delete the row."""

        self.finished("old")
        self.assertEqual(frozenset(), self.engine.live_workspace_refs())

    def test_a_node_mid_execution_is_live_before_it_ever_settles(self) -> None:
        """The race this method exists to close.

        `execution_order` is only appended once a node's whole step function
        returns — *after* its Handler has already run. A long-running Agent
        node has claimed its attempt, and could already have acquired a
        workspace grant, well before that point; a sweep reading only
        `execution_order` would see it as never having started, and could
        reclaim the workspace out from under the process still using it.
        `langgraph_handler_attempts` is claimed *before* the Handler runs and
        stays non-terminal (`status='started'`) until it settles, so it spans
        exactly the window a grant can be held.
        """

        run = self.waiting("live")
        with self.engine._connect() as connection:
            connection.execute(
                "INSERT INTO langgraph_handler_attempts"
                "(attempt_id,run_id,node_id,status,updated_at,handler_name)"
                " VALUES (?,?,?,'started',?,?)",
                ("attempt-mid-flight", run.run_id, "in_flight_node",
                 "2026-01-01T00:00:00Z", "agent.claude"),
            )
            connection.commit()

        self.assertIn(
            f"{run.run_id}:in_flight_node", self.engine.live_workspace_refs(),
        )

    def test_a_run_that_ended_recently_is_kept(self) -> None:
        self.finished("recent")
        self.assertEqual(0, self.engine.prune(before="2000-01-01")["runs"])
        self.assertEqual(1, len(self.engine.list_runs()))

    def test_a_dry_run_reports_the_same_and_changes_nothing(self) -> None:
        self.finished("old")
        preview = self.engine.prune(before="2999-01-01", dry_run=True)
        self.assertEqual(1, preview["runs"])
        self.assertFalse(preview["pruned"])
        self.assertEqual(1, len(self.engine.list_runs()))

    def test_pruning_is_bounded_per_call(self) -> None:
        """It must not hold the write lock for the length of a year."""

        for index in range(4):
            self.finished(f"old-{index}")
        self.assertEqual(2, self.engine.prune(before="2999-01-01", limit=2)["runs"])
        self.assertEqual(2, len(self.engine.list_runs()))
        with self.assertRaises(ValueError):
            self.engine.prune(before="2999-01-01", limit=0)

    def test_a_blob_two_runs_share_outlives_the_first_of_them(self) -> None:
        """The store is content addressed, so the same bytes serve both."""

        keep = self.waiting("keeper")
        drop = self.finished("old")
        store = self.engine.artifacts
        receipt = store.backend.write(b"the same bytes", max_size_bytes=4096)
        with store._connect() as connection:
            for index, run in enumerate((keep, drop)):
                connection.execute(
                    "INSERT INTO langgraph_artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"artifact:{index}", run.run_id, "attempt:1", "work",
                     "value", "schema:text", "text/plain", receipt.size_bytes,
                     receipt.blob_key, "committed", None, "local"),
                )
            connection.commit()
        self.engine.prune(before="2999-01-01")
        self.assertEqual(
            b"the same bytes",
            store.backend.read(receipt.blob_key, max_size_bytes=4096),
        )

    def test_the_size_of_what_is_kept_can_be_read(self) -> None:
        self.finished("one")
        sizes = self.engine.store_sizes()
        self.assertGreater(sizes["runs"], 0)
        self.assertGreater(sizes["checkpoints"], 0)



class RetryOnTheRealHandlerPathTests(unittest.TestCase):
    """A retry policy on a Handler that actually runs a process.

    The machinery was complete and unreachable. `LangGraphRetryableError` was
    raised in exactly one place in the codebase — a test double — so a real
    Tool or Agent with `max_attempts: 3` was called once and the run failed,
    while the DSL, the validator and the compiler all carried the feature.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def service(self, adapter, *, policy=True, safety=None):
        from orbit.workflow.langgraph_runtime.artifacts import LangGraphArtifactStore
        from orbit.workflow.langgraph_runtime.wiring import trusted_handlers

        fixture = engine_tests.LangGraphProductionWiringTests("run")
        registration = (
            fixture.tool_registration(adapter) if safety is None
            else fixture.tool_registration(adapter, safety=safety)
        )
        manifest = registration.manifest
        retry = IRPolicy(
            "retry_it", "retry", {"max_attempts": 3, "backoff_seconds": [0]},
        )
        node = IRNode(
            "tool", "action",
            (engine_tests.port("value"),), (engine_tests.port("value"),),
            IRHandlerRef(manifest.name, manifest.version, manifest.fingerprint),
            {"tool_name": "example.read", "tool_version": "1.0.0"},
            (retry.id,) if policy else (), None,
        )
        ir = engine_tests.workflow(
            (node, engine_tests.node(
                "done", inputs=("value",), kind="terminal", handler=False,
            )),
            (engine_tests.edge("t_d", "tool", "done"),),
            entry=("tool",), terminals=("done",), result=("tool", "value"),
            policies=(retry,) if policy else (),
        )
        store = SQLiteWorkflowVersionStore(self.root / "workflows.sqlite3")
        store.publish(
            CompiledWorkflow(ir, definition_hash(ir), "test", "sha256:" + "c" * 64),
            expected_latest_version=0, source_format="json",
            source_text="{}", actor="test", dsl_version="1.3",
        )
        runs = self.root / "runs.sqlite3"
        service = LangGraphWorkflowService(
            store, trusted_handlers([registration], attempt_db_path=runs),
            run_db_path=runs,
            checkpoint_db_path=self.root / "checkpoints.sqlite3",
            artifact_store=LangGraphArtifactStore(runs, self.root / "artifacts"),
        )
        return service, ir

    def flaky(self, failures: int):
        from orbit.workflow.handlers.tools import ToolResult

        calls = {"count": 0}

        class Adapter:
            def execute(self, request, context):
                calls["count"] += 1
                if calls["count"] <= failures:
                    raise RuntimeError("the network blinked")
                return ToolResult({"value": calls["count"]})

            def cancel(self, execution_ref, context):
                return None

            def recover(self, recovery_ref, context):
                return None

        return Adapter(), calls

    def test_a_failure_that_may_be_repeated_is(self) -> None:
        adapter, calls = self.flaky(1)
        service, ir = self.service(adapter)
        run = service.start(
            ir.workflow_id, {"value": 1}, idempotency_key="retry", actor="local",
        )
        # The run waits on a durable timer rather than looping in place, so a
        # process that dies between attempts resumes at the next start.
        self.assertEqual("waiting", run.status)
        self.assertEqual(1, calls["count"])

        service.recover_due(limit=10)
        self.assertEqual("completed", service.get(run.run_id).status)
        self.assertEqual(2, calls["count"])
        steps = {
            step["node_id"]: step
            for step in service.steps(run.run_id, actor="local")
        }
        self.assertEqual("succeeded", steps["tool"]["status"])

    def test_the_budget_is_what_stops_it(self) -> None:
        adapter, calls = self.flaky(99)
        service, ir = self.service(adapter)
        run = service.start(
            ir.workflow_id, {"value": 1}, idempotency_key="doomed", actor="local",
        )
        for _ in range(5):
            if service.get(run.run_id).status in {"failed", "completed"}:
                break
            service.recover_due(limit=10)
        self.assertEqual("failed", service.get(run.run_id).status)
        # `max_attempts: 3` is three goes, not three retries after the first.
        self.assertEqual(3, calls["count"])

    def test_without_a_policy_the_original_failure_is_what_surfaces(self) -> None:
        """Being repeatable is not the same as being repeated.

        The adapter says a failure may be retried; the compiler decides
        whether this node asked for that. A node that did not gets its own
        exception back, not the engine's word for one.
        """

        adapter, calls = self.flaky(99)
        service, ir = self.service(adapter, policy=False)
        with self.assertRaises(RuntimeError) as caught:
            service.start(
                ir.workflow_id, {"value": 1}, idempotency_key="bare",
                actor="local",
            )
        self.assertIn("the network blinked", str(caught.exception))
        self.assertEqual(1, calls["count"])

    def test_a_handler_whose_effect_may_have_happened_is_not_repeatable(self) -> None:
        """Compilation refuses the policy; the adapter refuses the label.

        Two guards for one rule, and the first is the one that stops the
        repeat: a workflow that attaches a retry policy to an
        `unknown_on_lease_loss` Handler does not compile, so its Handler is
        never even reached.
        """

        from orbit.workflow.domain.durable_execution import ExecutionSafety
        from orbit.workflow.langgraph_runtime.compiler import LangGraphCompileError

        adapter, calls = self.flaky(99)
        service, ir = self.service(
            adapter, safety=ExecutionSafety.UNKNOWN_ON_LEASE_LOSS,
        )
        with self.assertRaises(LangGraphCompileError):
            service.start(
                ir.workflow_id, {"value": 1}, idempotency_key="unsafe",
                actor="local",
            )
        self.assertEqual(0, calls["count"])

    def test_each_attempt_leaves_its_own_console(self) -> None:
        """Reading why the first go failed is the point of keeping them."""

        from orbit.workflow.handlers.tools import ToolResult
        from orbit.workflow.langgraph_runtime.console import AttemptConsole

        calls = {"count": 0}

        class Adapter:
            def execute(self, request, context):
                calls["count"] += 1
                sink = getattr(context, "output", None)
                if sink:
                    sink.emit("stderr", f"attempt {calls['count']}\n")
                if calls["count"] == 1:
                    raise RuntimeError("the network blinked")
                return ToolResult({"value": 1})

            def cancel(self, execution_ref, context):
                return None

            def recover(self, recovery_ref, context):
                return None

        service, ir = self.service(Adapter())
        service.console = AttemptConsole(self.root / "runs.sqlite3")
        run = service.start(
            ir.workflow_id, {"value": 1}, idempotency_key="console", actor="local",
        )
        service.recover_due(limit=10)
        self.assertEqual("completed", service.get(run.run_id).status)
        chunks, _after, _more = AttemptConsole(
            self.root / "runs.sqlite3"
        ).read(run.run_id)
        self.assertEqual(
            ["attempt 1\n", "attempt 2\n"], [item["text"] for item in chunks],
        )



def exists(path: str):
    return {"op": "call", "name": "exists", "args": [{"op": "ref", "path": path}]}


class EdgeReportTests(unittest.TestCase):
    """Which branches a run took, and which it silently did not.

    The condition `exists(source.value.severity)` is checked no further than
    its `source.value` prefix at compile time, and `exists` answers False
    rather than raising when the rest of the path is missing. An author whose
    Agent never produces `severity` therefore gets a branch that is never
    taken, on every run, with nothing raised anywhere — which is the hole
    this report exists to show.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_with(self, output, *, edges, route_mode=None):
        from orbit.workflow.handlers.tools import ToolResult
        from orbit.workflow.langgraph_runtime.artifacts import LangGraphArtifactStore
        from orbit.workflow.langgraph_runtime.wiring import trusted_handlers

        class Adapter:
            def execute(self, request, context):
                return ToolResult({"value": output})

            def cancel(self, execution_ref, context):
                return None

            def recover(self, recovery_ref, context):
                return None

        fixture = engine_tests.LangGraphProductionWiringTests("run")
        registration = fixture.tool_registration(Adapter())
        manifest = registration.manifest
        classify = IRNode(
            "classify", "action",
            (engine_tests.port("value"),), (engine_tests.port("value"),),
            IRHandlerRef(manifest.name, manifest.version, manifest.fingerprint),
            {"tool_name": "example.read", "tool_version": "1.0.0"},
            (), None, route_mode,
        )
        ir = engine_tests.workflow(
            (
                classify,
                engine_tests.node(
                    "urgent", inputs=("value",), outputs=("value",),
                    kind="terminal", handler=False,
                ),
                engine_tests.node(
                    "normal", inputs=("value",), outputs=("value",),
                    kind="terminal", handler=False,
                ),
            ),
            edges,
            entry=("classify",), terminals=("urgent", "normal"),
            result=("classify", "value"),
        )
        store = SQLiteWorkflowVersionStore(self.root / "workflows.sqlite3")
        store.publish(
            CompiledWorkflow(ir, definition_hash(ir), "test", "sha256:" + "c" * 64),
            expected_latest_version=0, source_format="json",
            source_text="{}", actor="test", dsl_version="1.3",
        )
        runs = self.root / "runs.sqlite3"
        service = LangGraphWorkflowService(
            store, trusted_handlers([registration], attempt_db_path=runs),
            run_db_path=runs,
            checkpoint_db_path=self.root / "checkpoints.sqlite3",
            artifact_store=LangGraphArtifactStore(runs, self.root / "artifacts"),
        )
        run = service.start(
            ir.workflow_id, {"value": {}}, idempotency_key="one", actor="local",
        )
        reported = service.edges(run.run_id, actor="local")
        # The word the UI shows is looked up by this string, so a status the
        # vocabulary does not declare would render as a raw key.
        for item in reported:
            assert item["status"] in EDGE_STATUSES, item["status"]
        return service, run, {item["edge_id"]: item for item in reported}

    def branching(self):
        return (
            engine_tests.edge(
                "to_urgent", "classify", "urgent",
                condition=exists("source.value.severity"),
            ),
            engine_tests.edge("to_normal", "classify", "normal"),
        )

    def test_a_branch_guarded_on_a_field_nobody_produces_is_named(self) -> None:
        _service, _run, report = self.run_with({}, edges=self.branching())
        self.assertEqual("not_taken", report["to_urgent"]["status"])
        self.assertEqual("taken", report["to_normal"]["status"])
        # It was decided, not merely unvisited: the node ran.
        self.assertEqual(1, report["to_urgent"]["visits"])

    def test_the_same_branch_when_the_field_is_there(self) -> None:
        """The report must be capable of saying the branch is alive."""

        _service, _run, report = self.run_with(
            {"severity": "high"}, edges=self.branching(),
        )
        self.assertEqual("taken", report["to_urgent"]["status"])
        # The default's condition did hold; being passed over is its job, and
        # `default` is how a reader tells that apart from a dead branch.
        self.assertEqual("shadowed", report["to_normal"]["status"])
        self.assertTrue(report["to_normal"]["default"])
        self.assertFalse(report["to_urgent"]["default"])

    def test_an_unconditional_edge_is_tried_last_however_it_is_numbered(self) -> None:
        """A default with a winning priority would kill every condition.

        The report orders edges through the engine's own rule rather than a
        copy of it; ordering them by priority alone would report the guarded
        edge as shadowed and the run would disagree.
        """

        _service, _run, report = self.run_with(
            {"severity": "high"},
            edges=(
                engine_tests.edge(
                    "to_urgent", "classify", "urgent", priority=9,
                    condition=exists("source.value.severity"),
                ),
                engine_tests.edge("to_normal", "classify", "normal", priority=0),
            ),
        )
        self.assertEqual("taken", report["to_urgent"]["status"])
        self.assertEqual("shadowed", report["to_normal"]["status"])

    def test_a_branch_a_higher_priority_edge_always_wins_is_shadowed(self) -> None:
        """True and still never followed — dead for a second reason.

        `not_taken` would be a lie here: the condition held. What killed the
        branch is an exclusive node having already chosen.
        """

        _service, _run, report = self.run_with(
            {"severity": "high"},
            edges=(
                engine_tests.edge(
                    "first", "classify", "normal", priority=0,
                    condition=exists("source.value.severity"),
                ),
                engine_tests.edge(
                    "second", "classify", "urgent", priority=1,
                    condition=exists("source.value.severity"),
                ),
            ),
        )
        self.assertEqual("taken", report["first"]["status"])
        self.assertEqual("shadowed", report["second"]["status"])

    def test_a_parallel_node_shadows_nothing(self) -> None:
        _service, _run, report = self.run_with(
            {"severity": "high"},
            route_mode="parallel",
            edges=(
                engine_tests.edge(
                    "first", "classify", "normal", priority=0,
                    condition=exists("source.value.severity"),
                ),
                engine_tests.edge(
                    "second", "classify", "urgent", priority=1,
                    condition=exists("source.value.severity"),
                ),
            ),
        )
        self.assertEqual(
            {"taken"}, {report[name]["status"] for name in ("first", "second")},
        )

    def test_an_error_edge_on_a_node_that_succeeded_is_not_a_dead_branch(self) -> None:
        """Kept apart so a report of dead branches is mostly dead branches."""

        _service, _run, report = self.run_with(
            {}, edges=(
                engine_tests.edge("to_normal", "classify", "normal"),
                engine_tests.edge(
                    "on_error", "classify", "urgent", route="error",
                ),
            ),
        )
        self.assertEqual("other_route", report["on_error"]["status"])

    def test_edges_below_a_node_that_never_ran_decided_nothing(self) -> None:
        """`not_reached` and `not_taken` are different findings.

        A branch under a node the run never got to says nothing about whether
        its condition is satisfiable, and reporting it as never taken would
        bury the branches that are.
        """

        _service, _run, report = self.run_with(
            {}, edges=(
                *self.branching(),
                engine_tests.edge("onward", "urgent", "normal"),
            ),
        )
        self.assertEqual("not_taken", report["to_urgent"]["status"])
        self.assertEqual("not_reached", report["onward"]["status"])
        self.assertEqual(0, report["onward"]["visits"])



class BranchHistoryTests(EdgeReportTests):
    """The tally across runs — the only place a branch can be called suspect.

    Inherits the fixture rather than the assertions: what is under test here
    is what many runs of one definition add up to, not what one run did.
    """

    def repeat(self, outputs, *, edges, starts=None):
        """Start one run per output, all against the same definition.

        `starts` holds some outputs back for runs the test starts itself.
        """

        from orbit.workflow.handlers.tools import ToolResult
        from orbit.workflow.langgraph_runtime.artifacts import LangGraphArtifactStore
        from orbit.workflow.langgraph_runtime.wiring import trusted_handlers

        pending = list(outputs)

        class Adapter:
            def execute(self, request, context):
                return ToolResult({"value": pending.pop(0)})

            def cancel(self, execution_ref, context):
                return None

            def recover(self, recovery_ref, context):
                return None

        fixture = engine_tests.LangGraphProductionWiringTests("run")
        registration = fixture.tool_registration(Adapter())
        manifest = registration.manifest
        classify = IRNode(
            "classify", "action",
            (engine_tests.port("value"),), (engine_tests.port("value"),),
            IRHandlerRef(manifest.name, manifest.version, manifest.fingerprint),
            {"tool_name": "example.read", "tool_version": "1.0.0"},
            (), None, None,
        )
        ir = engine_tests.workflow(
            (
                classify,
                engine_tests.node(
                    "urgent", inputs=("value",), outputs=("value",),
                    kind="terminal", handler=False,
                ),
                engine_tests.node(
                    "normal", inputs=("value",), outputs=("value",),
                    kind="terminal", handler=False,
                ),
            ),
            edges,
            entry=("classify",), terminals=("urgent", "normal"),
            result=("classify", "value"),
        )
        store = SQLiteWorkflowVersionStore(self.root / "workflows.sqlite3")
        store.publish(
            CompiledWorkflow(ir, definition_hash(ir), "test", "sha256:" + "c" * 64),
            expected_latest_version=0, source_format="json",
            source_text="{}", actor="test", dsl_version="1.3",
        )
        runs = self.root / "runs.sqlite3"
        service = LangGraphWorkflowService(
            store, trusted_handlers([registration], attempt_db_path=runs),
            run_db_path=runs,
            checkpoint_db_path=self.root / "checkpoints.sqlite3",
            artifact_store=LangGraphArtifactStore(runs, self.root / "artifacts"),
        )
        for index in range(len(outputs) if starts is None else starts):
            service.start(
                ir.workflow_id, {"value": {}},
                idempotency_key=f"run-{index}", actor="local",
            )
        return service, ir

    def history(self, service, ir, **kwargs):
        report = service.branch_history(ir.workflow_id, actor="local", **kwargs)
        for item in report["edges"]:
            assert item["verdict"] in BRANCH_VERDICTS, item["verdict"]
        return report, {item["edge_id"]: item for item in report["edges"]}

    def test_a_branch_no_run_ever_entered_is_the_finding(self) -> None:
        """Twelve decisions, zero entries — what one run could never say."""

        service, ir = self.repeat([{}] * 12, edges=self.branching())
        report, edges = self.history(service, ir)
        self.assertEqual(12, report["runs"])
        self.assertEqual("never_taken", edges["to_urgent"]["verdict"])
        self.assertEqual(12, edges["to_urgent"]["decided"])
        self.assertEqual(0, edges["to_urgent"]["taken"])
        self.assertEqual("taken", edges["to_normal"]["verdict"])

    def test_one_run_that_entered_it_settles_the_question(self) -> None:
        """Rare is not dead, and the tally must not confuse them.

        This is the whole reason the verdict is drawn from a count of
        entries rather than a ratio: a branch taken once in twelve is alive.
        """

        service, ir = self.repeat(
            [{}] * 11 + [{"severity": "high"}], edges=self.branching(),
        )
        _report, edges = self.history(service, ir)
        self.assertEqual("taken", edges["to_urgent"]["verdict"])
        self.assertEqual(1, edges["to_urgent"]["taken"])
        self.assertEqual(11, edges["to_urgent"]["not_taken"])

    def test_an_error_edge_on_runs_that_never_failed_is_not_a_finding(self) -> None:
        """Otherwise it would be the loudest one on the page, every time."""

        service, ir = self.repeat([{}] * 5, edges=(
            engine_tests.edge("to_normal", "classify", "normal"),
            engine_tests.edge("on_error", "classify", "urgent", route="error"),
        ))
        _report, edges = self.history(service, ir)
        self.assertEqual("no_evidence", edges["on_error"]["verdict"])
        self.assertEqual(0, edges["on_error"]["decided"])
        self.assertEqual(5, edges["on_error"]["other_route"])

    def test_pruning_runs_shrinks_the_evidence(self) -> None:
        """Every number is backed by a run that can still be opened.

        A counter would keep saying twelve after eleven of the runs behind
        it were deleted, and the one remaining run could not corroborate it.
        """

        service, ir = self.repeat([{}] * 12, edges=self.branching())
        self.assertEqual(12, self.history(service, ir)[0]["runs"])
        service.prune(before="2999-01-01T00:00:00Z")
        report, edges = self.history(service, ir)
        self.assertEqual(0, report["runs"])
        self.assertEqual({}, edges)

    def test_a_limit_bounds_how_far_back_it_looks(self) -> None:
        service, ir = self.repeat([{}] * 12, edges=self.branching())
        report, edges = self.history(service, ir, limit=4)
        self.assertEqual(4, report["runs"])
        self.assertEqual(4, edges["to_urgent"]["decided"])
        with self.assertRaises(ValueError):
            service.branch_history(ir.workflow_id, actor="local", limit=0)

    def test_an_edge_id_is_only_the_same_edge_within_one_version(self) -> None:
        """Two definitions' tallies must not be added together.

        A published change can reuse an edge id for a different condition,
        or between different nodes. Counting both under one heading would
        report an edge as never entered on the strength of runs of a
        definition where it did not exist.
        """

        # Seven outputs: four runs of version 1 here, three of version 2 below.
        service, ir = self.repeat([{"severity": "high"}] * 7, starts=4, edges=(
            engine_tests.edge(
                "branch", "classify", "urgent",
                condition=exists("source.value.severity"),
            ),
            engine_tests.edge("fallback", "classify", "normal"),
        ))
        first = self.history(service, ir)[1]
        self.assertEqual("taken", first["branch"]["verdict"])

        # The same id, now guarding a field nothing produces.
        revised = engine_tests.workflow(
            tuple(ir.nodes),
            (
                engine_tests.edge(
                    "branch", "classify", "urgent",
                    condition=exists("source.value.nothing_makes_this"),
                ),
                engine_tests.edge("fallback", "classify", "normal"),
            ),
            entry=("classify",), terminals=("urgent", "normal"),
            result=("classify", "value"),
        )
        SQLiteWorkflowVersionStore(self.root / "workflows.sqlite3").publish(
            CompiledWorkflow(
                revised, definition_hash(revised), "test", "sha256:" + "d" * 64,
            ),
            expected_latest_version=1, source_format="json",
            source_text="{}", actor="test", dsl_version="1.3",
        )
        for index in range(3):
            service.start(
                revised.workflow_id, {"value": {}},
                idempotency_key=f"v2-{index}", actor="local",
            )

        report, edges = self.history(service, ir)
        self.assertEqual(2, report["workflow_version"])
        # Three runs of version 2, not seven of both.
        self.assertEqual(3, report["runs"])
        self.assertEqual("never_taken", edges["branch"]["verdict"])
        self.assertEqual(3, edges["branch"]["decided"])

        older, older_edges = self.history(service, ir, workflow_version=1)
        self.assertEqual(4, older["runs"])
        self.assertEqual("taken", older_edges["branch"]["verdict"])

    def test_another_actor_sees_none_of_it(self) -> None:
        service, ir = self.repeat([{}] * 3, edges=self.branching())
        report = service.branch_history(ir.workflow_id, actor="somebody-else")
        self.assertEqual(0, report["runs"])
        self.assertEqual((), report["edges"])



class ReadingARunWhileItIsWrittenTests(unittest.TestCase):
    """A read of a run must not need the lock its writer is holding.

    `SqliteSaver.setup()` runs on every fresh instance and its first statement
    is `PRAGMA journal_mode=WAL`, which SQLite performs only under an
    exclusive lock and refuses immediately — no busy timeout, no retry — when
    another connection holds one. Every `/steps` call opened such an instance,
    so reading a run while it executed failed at random, and a background run
    whose own saver lost the race failed the run outright.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "runtime.db"
        publish_human_workflow(self.path)
        self.engine = build_service(
            self.path, [transform_registration()],
            state_directory=self.root / "langgraph",
        )

    def test_steps_read_through_a_writer_holding_the_checkpoint_file(self) -> None:
        run = self.engine.start(
            "workflow:human", {"value": 1}, idempotency_key="held", actor="local",
        )
        # Exactly what an executing run holds: an open write transaction on
        # the checkpoint database.
        holder = sqlite3.connect(self.engine.checkpoint_db_path, timeout=30)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute(
            "INSERT INTO writes VALUES ('other','','c','task',0,'ch','t',x'00')"
        )
        try:
            steps = self.engine.steps(run.run_id, actor="local")
            edges = self.engine.edges(run.run_id, actor="local")
        finally:
            holder.rollback()
        self.assertTrue(steps)
        self.assertTrue(edges)

    def test_a_reader_arriving_on_a_half_written_checkpoint_file(self) -> None:
        """The race as it actually happened: both savers on a new file.

        The run that has just started is still creating the schema when the
        page that started it asks where it got to.
        """

        service = build_service(
            self.path, [transform_registration()],
            state_directory=self.root / "fresh",
        )
        run = service.start(
            "workflow:human", {"value": 1}, idempotency_key="fresh", actor="local",
        )
        service.checkpoint_db_path.unlink()
        holder = sqlite3.connect(service.checkpoint_db_path, timeout=30)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("CREATE TABLE IF NOT EXISTS half_way(x)")
        try:
            steps = service.steps(run.run_id, actor="local")
        finally:
            holder.rollback()
        # Its checkpoints are gone, so nothing reads as having run; the
        # interrupt is on the run row and survives. The point is that it
        # answers at all.
        self.assertEqual(
            {"not_reached", "waiting"}, {step["status"] for step in steps},
        )



class DefinitionsAreParsedOnceTests(unittest.TestCase):
    """Reading a run costs a definition, and that is the expensive half.

    Resolving one re-reads the version row and puts the whole IR back through
    JSON Schema validation. A hundred-run branch tally did that a hundred
    times for the same definition — 83% of its work, measured. A published
    version is only ever inserted and a run's graph snapshot is written once
    when it starts, so nothing cached here can go stale; it can only be
    evicted.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "runtime.db"
        publish_human_workflow(self.path)
        self.engine = build_service(
            self.path, [transform_registration()],
            state_directory=self.root / "langgraph",
        )

    def counted(self, engine=None):
        """Count trips to the version store, the cost the cache removes."""

        store = (engine or self.engine).workflow_versions
        original = store.get
        calls = {"count": 0}

        def counting(workflow_id, version):
            calls["count"] += 1
            return original(workflow_id, version)

        store.get = counting
        self.addCleanup(setattr, store, "get", original)
        return calls

    def start(self, count: int):
        return [
            self.engine.start(
                "workflow:human", {"value": 1},
                idempotency_key=f"c{index}", actor="local",
            )
            for index in range(count)
        ]

    def test_a_tally_costs_the_same_whatever_the_run_count(self) -> None:
        """Constant, not a magic number: the definition is resolved per tally.

        Twelve runs and thirty-six of the same definition must cost the same
        trips to the version store, each against a service that has cached
        nothing yet.
        """

        costs = []
        for index, count in enumerate((12, 36)):
            engine = build_service(
                self.path, [transform_registration()],
                state_directory=self.root / f"tally{index}",
            )
            for run in range(count):
                engine.start(
                    "workflow:human", {"value": 1},
                    idempotency_key=f"t{index}-{run}", actor="local",
                )
            calls = self.counted(engine)
            report = engine.branch_history("workflow:human", actor="local")
            self.assertEqual(count, report["runs"])
            self.assertTrue(report["edges"])
            costs.append(calls["count"])
        self.assertEqual(costs[0], costs[1])
        self.assertLessEqual(costs[0], 2)

    def test_reading_a_run_twice_reads_the_definition_once(self) -> None:
        run = self.start(1)[0]
        self.engine.steps(run.run_id, actor="local")
        calls = self.counted()
        first = self.engine.steps(run.run_id, actor="local")
        second = self.engine.edges(run.run_id, actor="local")
        self.assertEqual(0, calls["count"])
        self.assertTrue(first)
        self.assertTrue(second)

    def test_the_key_is_the_version_and_the_snapshot(self) -> None:
        """Three definitions, one cache, and no two of them collapse.

        Keyed by the workflow alone this passes anyway — every lookup would
        return whichever definition was resolved first — so the three live on
        one service on purpose. A run of version 1 must keep reporting
        version 1 after version 2 is published, and a template run's graph
        lives on the run and nowhere else.
        """

        import dataclasses
        import json as json_module
        from orbit.workflow.domain.serialization import to_primitive

        first = self.engine._ir_for(
            run_id="langgraph_run:one", workflow_id="workflow:human",
            workflow_version=1, snapshot=None,
        )
        work = engine_tests.node("work", inputs=("value",), outputs=("value",))
        done = engine_tests.node(
            "done", inputs=("value",), kind="terminal", handler=False,
        )
        other = engine_tests.workflow(
            (work, done), (engine_tests.edge("w_d", "work", "done"),),
            entry=("work",), terminals=("done",), result=("work", "value"),
        )
        renamed = dataclasses.replace(other, workflow_id="workflow:human")
        SQLiteWorkflowVersionStore(self.path).publish(
            CompiledWorkflow(
                renamed, definition_hash(renamed), "test", "sha256:" + "e" * 64,
            ),
            expected_latest_version=1, source_format="json",
            source_text="{}", actor="test", dsl_version="1.3",
        )
        second = self.engine._ir_for(
            run_id="langgraph_run:two", workflow_id="workflow:human",
            workflow_version=2, snapshot=None,
        )
        third = self.engine._ir_for(
            run_id="langgraph_run:three", workflow_id="workflow:human",
            workflow_version=1,
            snapshot=json_module.dumps(to_primitive(other)),
        )
        names = lambda ir: {node.id for node in ir.nodes}
        self.assertEqual({"transform", "approve", "done"}, names(first))
        self.assertEqual({"work", "done"}, names(second))
        self.assertEqual({"work", "done"}, names(third))
        # Version 1 is still version 1 after version 2 exists.
        self.assertEqual(names(first), names(self.engine._ir_for(
            run_id="langgraph_run:one", workflow_id="workflow:human",
            workflow_version=1, snapshot=None,
        )))

    def test_a_template_run_reports_its_own_definition(self) -> None:
        """End to end, through the run it belongs to."""

        work = engine_tests.node("work", inputs=("value",), outputs=("value",))
        done = engine_tests.node(
            "done", inputs=("value",), kind="terminal", handler=False,
        )
        ir = engine_tests.workflow(
            (work, done), (engine_tests.edge("w_d", "work", "done"),),
            entry=("work",), terminals=("done",), result=("work", "value"),
        )
        engine = LangGraphWorkflowService(
            SQLiteWorkflowVersionStore(self.path),
            LangGraphHandlerRegistry([engine_tests.binding(
                "work", lambda values, config, context: dict(values),
            )]),
            run_db_path=self.root / "template-runs.sqlite3",
            checkpoint_db_path=self.root / "template-checkpoints.sqlite3",
        )
        run = engine.start_snapshot(
            "workflow:template", ir, {"value": 1},
            template_id="t1", idempotency_key="tpl", actor="local",
        )
        steps = {step["node_id"] for step in engine.steps(run.run_id, actor="local")}
        self.assertEqual({"work", "done"}, steps)

    def test_the_cache_is_bounded(self) -> None:
        from orbit.workflow.langgraph_runtime.service import IR_CACHE_SIZE

        import json as json_module
        from orbit.workflow.domain.serialization import to_primitive

        run = self.start(1)[0]
        work = engine_tests.node("work", inputs=("value",), outputs=("value",))
        done = engine_tests.node(
            "done", inputs=("value",), kind="terminal", handler=False,
        )
        snapshot = json_module.dumps(to_primitive(engine_tests.workflow(
            (work, done), (engine_tests.edge("w_d", "work", "done"),),
            entry=("work",), terminals=("done",), result=("work", "value"),
        )))
        # Each is a different run's own graph, which is the entry that can
        # grow without bound in a long-lived process.
        for index in range(IR_CACHE_SIZE * 2):
            self.engine._ir_for(
                run_id=f"langgraph_run:filler{index}",
                workflow_id="workflow:human", workflow_version=1,
                snapshot=snapshot,
            )
        self.assertLessEqual(len(self.engine._ir_cache), IR_CACHE_SIZE)
        # Evicting is not forgetting: the answer is re-derived, not lost.
        self.assertTrue(self.engine.steps(run.run_id, actor="local"))



class ShutdownIsActuallyBoundedTests(unittest.TestCase):
    """The timeout has to bound the process, not just the wait on it.

    `wait_for_background` returned inside its timeout and the runtime reported
    its stragglers, but the process then sat waiting for them anyway: the
    interpreter joins every non-daemon thread on the way out, and a thread
    pool's workers are not daemon. Measured on a Handler that sleeps thirty
    seconds against a half-second bound: the wait returned in half a second
    and the process exited in thirty. Unregistering the pool's own exit hook
    changed nothing, because it is the interpreter's join, not that hook.

    A hung Handler is not even the common case. A worker parked on an empty
    queue is also a non-daemon thread, so any process that had *ever* deferred
    a run would refuse to exit at all — every run finished, nothing to wait
    for, and the interpreter waiting anyway.
    """

    def exits_within(self, seconds: float, body: str) -> str:
        root = str(Path(__file__).resolve().parents[1])
        script = f"import sys\nsys.path.insert(0, {root!r})\n" + textwrap.dedent(body)
        began = time.monotonic()
        finished = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=seconds,
        )
        elapsed = time.monotonic() - began
        self.assertLess(
            elapsed, seconds, f"the process took {elapsed:.1f}s to exit",
        )
        return finished.stdout + finished.stderr

    def test_a_process_that_deferred_a_run_can_still_exit(self) -> None:
        """Every run finished; nothing should be holding the door."""

        output = self.exits_within(30, '''
            import tempfile
            from pathlib import Path
            sys.path.insert(0, ".")
            from orbit.workflow.langgraph_runtime import build_service
            from tests.test_web_composition import (
                publish_human_workflow, transform_registration,
            )

            root = Path(tempfile.mkdtemp())
            path = root / "runtime.db"
            publish_human_workflow(path)
            engine = build_service(
                path, [transform_registration()],
                state_directory=root / "langgraph",
            )
            engine.start(
                "workflow:human", {"value": 1}, idempotency_key="one",
                actor="local", wait=False,
            )
            print("stragglers", len(engine.wait_for_background(timeout=10)),
                  flush=True)
        ''')
        self.assertIn("stragglers 0", output)

    def test_a_handler_that_will_not_return_does_not_hold_the_process(self) -> None:
        output = self.exits_within(45, '''
            import tempfile, threading, time
            from pathlib import Path
            sys.path.insert(0, ".")
            from orbit.workflow.domain.definitions import CompiledWorkflow
            from orbit.workflow.domain.serialization import definition_hash
            from orbit.workflow.langgraph_runtime.compiler import (
                LangGraphHandlerRegistry,
            )
            from orbit.workflow.langgraph_runtime.service import (
                LangGraphWorkflowService,
            )
            from orbit.workflow.persistence.workflow_versions import (
                SQLiteWorkflowVersionStore,
            )
            import tests.test_workflow_langgraph_runtime as engine_tests

            root = Path(tempfile.mkdtemp())
            entered = threading.Event()

            def never_returns(values, config, context):
                entered.set()
                time.sleep(300)
                return dict(values)

            work = engine_tests.node("work", inputs=("value",), outputs=("value",))
            done = engine_tests.node(
                "done", inputs=("value",), kind="terminal", handler=False,
            )
            ir = engine_tests.workflow(
                (work, done), (engine_tests.edge("w_d", "work", "done"),),
                entry=("work",), terminals=("done",), result=("work", "value"),
            )
            store = SQLiteWorkflowVersionStore(root / "workflows.sqlite3")
            store.publish(
                CompiledWorkflow(
                    ir, definition_hash(ir), "test", "sha256:" + "c" * 64,
                ),
                expected_latest_version=0, source_format="json",
                source_text="{}", actor="test", dsl_version="1.3",
            )
            service = LangGraphWorkflowService(
                store,
                LangGraphHandlerRegistry([
                    engine_tests.binding("work", never_returns),
                ]),
                run_db_path=root / "runs.sqlite3",
                checkpoint_db_path=root / "checkpoints.sqlite3",
            )
            service.start(
                ir.workflow_id, {"value": 1}, idempotency_key="hangs",
                actor="local", wait=False,
            )
            entered.wait(20)
            print("stragglers", len(service.wait_for_background(timeout=0.5)),
                  flush=True)
        ''')
        self.assertIn("stragglers 1", output)

    def test_finished_runs_are_not_remembered_for_ever(self) -> None:
        """The bookkeeping is a count of what is outstanding, not a log.

        Holding a handle per deferred run meant a long-lived process grew one
        for every run anybody started without waiting, and nothing removed
        them until shutdown.
        """

        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        path = root / "runtime.db"
        publish_human_workflow(path)
        engine = build_service(
            path, [transform_registration()], state_directory=root / "langgraph",
        )
        for index in range(12):
            engine.start(
                "workflow:human", {"value": 1}, idempotency_key=f"d{index}",
                actor="local", wait=False,
            )
        self.assertEqual((), engine.wait_for_background(timeout=10))
        self.assertEqual(0, engine._background_pending)
        # Whatever is retained must not be one entry per run.
        self.assertLessEqual(len(engine._background_workers), 8)



class RunScopedGraphTests(unittest.TestCase):
    """A run is drawn from its own definition, not its workflow's latest.

    The catalog serves the current version and says so. Drawing a finished run
    from there put a graph the run never executed beside a step list derived
    from the definition it really used — two pictures of one run that
    disagreed. A template run, whose definition lives on the run and was never
    published, had nothing to draw at all.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "runtime.db"
        publish_human_workflow(self.path)
        self.engine = build_service(
            self.path, [transform_registration()],
            state_directory=self.root / "langgraph",
        )

    def other_ir(self):
        work = engine_tests.node("work", inputs=("value",), outputs=("value",))
        done = engine_tests.node(
            "done", inputs=("value",), kind="terminal", handler=False,
        )
        return engine_tests.workflow(
            (work, done), (engine_tests.edge("w_d", "work", "done"),),
            entry=("work",), terminals=("done",), result=("work", "value"),
        )

    def test_republishing_does_not_redraw_a_finished_run(self) -> None:
        import dataclasses

        run = self.engine.start(
            "workflow:human", {"value": 1}, idempotency_key="drawn", actor="local",
        )
        before = self.engine.graph(run.run_id, actor="local")
        self.assertEqual(
            {"transform", "approve", "done"},
            {node["node_id"] for node in before["nodes"]},
        )

        revised = dataclasses.replace(self.other_ir(), workflow_id="workflow:human")
        SQLiteWorkflowVersionStore(self.path).publish(
            CompiledWorkflow(
                revised, definition_hash(revised), "test", "sha256:" + "f" * 64,
            ),
            expected_latest_version=1, source_format="json",
            source_text="{}", actor="test", dsl_version="1.3",
        )
        after = self.engine.graph(run.run_id, actor="local")
        self.assertEqual(before, after)
        # And it still agrees with the steps drawn beside it.
        self.assertEqual(
            {node["node_id"] for node in after["nodes"]},
            {step["node_id"] for step in
             self.engine.steps(run.run_id, actor="local")},
        )

    def test_a_template_run_has_a_graph_at_all(self) -> None:
        """Its definition was never published, so the catalog has none."""

        ir = self.other_ir()
        engine = LangGraphWorkflowService(
            SQLiteWorkflowVersionStore(self.path),
            LangGraphHandlerRegistry([engine_tests.binding(
                "work", lambda values, config, context: dict(values),
            )]),
            run_db_path=self.root / "template-runs.sqlite3",
            checkpoint_db_path=self.root / "template-checkpoints.sqlite3",
        )
        run = engine.start_snapshot(
            "workflow:template", ir, {"value": 1},
            template_id="t1", idempotency_key="tpl", actor="local",
        )
        graph = engine.graph(run.run_id, actor="local")
        self.assertEqual(
            {"work", "done"}, {node["node_id"] for node in graph["nodes"]},
        )
        self.assertEqual(["w_d"], [edge["edge_id"] for edge in graph["edges"]])
        self.assertTrue(graph["layout"]["positions"])

    def test_it_is_the_same_projection_the_catalog_draws(self) -> None:
        """One renderer, so one vocabulary — `from`/`to`, not the IR's names."""

        run = self.engine.start(
            "workflow:human", {"value": 1}, idempotency_key="shape", actor="local",
        )
        graph = self.engine.graph(run.run_id, actor="local")
        self.assertEqual(
            {"nodes", "edges", "entry", "terminals", "layout"}, set(graph),
        )
        for edge in graph["edges"]:
            self.assertIn("from", edge)
            self.assertIn("to", edge)
            self.assertNotIn("source_node", edge)

    def test_somebody_else_cannot_read_the_graph(self) -> None:
        run = self.engine.start(
            "workflow:human", {"value": 1}, idempotency_key="mine", actor="local",
        )
        with self.assertRaises(LookupError):
            self.engine.graph(run.run_id, actor="another")



class CancellingAQueuedRunTests(unittest.TestCase):
    """A cancelled run must not reach a Handler afterwards.

    A deferred run can sit in the queue for as long as the runs ahead of it
    take. Cancelling it there signalled nothing — it had no attempt to cancel
    and nothing took it out of the queue — so a worker picked it up later and
    its Handlers ran, producing external effects after the cancellation, on a
    run already recorded as `cancelled`.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # One worker, so "still queued" is a fact rather than a race.
        self.original_workers = service_module.BACKGROUND_WORKERS
        service_module.BACKGROUND_WORKERS = 1
        self.addCleanup(
            setattr, service_module, "BACKGROUND_WORKERS", self.original_workers,
        )
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.calls: list[str] = []

    def build(self, *, nodes=1):
        """A workflow whose Tool blocks, wired through the real adapters."""

        from orbit.workflow.handlers.tools import ToolResult
        from orbit.workflow.langgraph_runtime.artifacts import LangGraphArtifactStore
        from orbit.workflow.langgraph_runtime.wiring import trusted_handlers

        outer = self

        class Adapter:
            def execute(self, request, context):
                # `execute` is handed what `prepare` returned; the
                # original request is on the context.
                outer.calls.append(context.request.attempt_id)
                outer.release.wait(20)
                return ToolResult({"value": 1})

            def cancel(self, execution_ref, context):
                outer.release.set()
                return None

            def recover(self, recovery_ref, context):
                return None

        fixture = engine_tests.LangGraphProductionWiringTests("run")
        registration = fixture.tool_registration(Adapter())
        manifest = registration.manifest
        steps = tuple(
            IRNode(
                f"tool{index}", "action",
                (engine_tests.port("value"),), (engine_tests.port("value"),),
                IRHandlerRef(
                    manifest.name, manifest.version, manifest.fingerprint,
                ),
                {"tool_name": "example.read", "tool_version": "1.0.0"},
                (), None,
            )
            for index in range(nodes)
        )
        edges = tuple(
            engine_tests.edge(f"e{index}", f"tool{index}", f"tool{index + 1}")
            for index in range(nodes - 1)
        ) + (engine_tests.edge("last", f"tool{nodes - 1}", "done"),)
        ir = engine_tests.workflow(
            (*steps, engine_tests.node(
                "done", inputs=("value",), kind="terminal", handler=False,
            )),
            edges, entry=("tool0",), terminals=("done",),
            result=(f"tool{nodes - 1}", "value"),
        )
        store = SQLiteWorkflowVersionStore(self.root / "workflows.sqlite3")
        store.publish(
            CompiledWorkflow(ir, definition_hash(ir), "test", "sha256:" + "c" * 64),
            expected_latest_version=0, source_format="json",
            source_text="{}", actor="test", dsl_version="1.3",
        )
        runs = self.root / "runs.sqlite3"
        service = LangGraphWorkflowService(
            store, trusted_handlers([registration], attempt_db_path=runs),
            run_db_path=runs,
            checkpoint_db_path=self.root / "checkpoints.sqlite3",
            artifact_store=LangGraphArtifactStore(runs, self.root / "artifacts"),
        )
        return service, ir

    def wait_for_calls(self, count: int, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while len(self.calls) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(len(self.calls), count, "the Handler never started")

    def test_a_run_cancelled_while_queued_never_reaches_its_handler(self) -> None:
        service, ir = self.build()
        service.start(
            ir.workflow_id, {"value": 1}, idempotency_key="first",
            actor="local", wait=False,
        )
        self.wait_for_calls(1)
        queued = service.start(
            ir.workflow_id, {"value": 1}, idempotency_key="second",
            actor="local", wait=False,
        )
        service.cancel(
            queued.run_id, expected_revision=queued.revision,
            idempotency_key="cancel-second", actor="local",
        )
        self.assertEqual("cancelled", service.get(queued.run_id).status)

        self.release.set()
        service.wait_for_background(timeout=20)
        # One call, from the run that was never cancelled.
        self.assertEqual(1, len(self.calls))
        self.assertEqual("cancelled", service.get(queued.run_id).status)

    def test_a_queued_run_is_refused_before_anything_is_compiled(self) -> None:
        """Not every Handler is an Agent or a Tool.

        The adapters refuse work for a cancelled run, but a node bound
        directly — a transform, anything registered without the Agent and
        Tool wiring — has no adapter to refuse on its behalf. The engine has
        to decline the run itself, which also spares compiling a graph and
        opening a checkpointer for a run nobody wants.
        """

        release = threading.Event()
        self.addCleanup(release.set)
        calls: list[str] = []

        def blocking(values, config, context):
            calls.append("first")
            release.wait(20)
            return dict(values)

        work = engine_tests.node("work", inputs=("value",), outputs=("value",))
        done = engine_tests.node(
            "done", inputs=("value",), kind="terminal", handler=False,
        )
        ir = engine_tests.workflow(
            (work, done), (engine_tests.edge("w_d", "work", "done"),),
            entry=("work",), terminals=("done",), result=("work", "value"),
        )
        store = SQLiteWorkflowVersionStore(self.root / "plain.sqlite3")
        store.publish(
            CompiledWorkflow(ir, definition_hash(ir), "test", "sha256:" + "c" * 64),
            expected_latest_version=0, source_format="json",
            source_text="{}", actor="test", dsl_version="1.3",
        )
        service = LangGraphWorkflowService(
            store, LangGraphHandlerRegistry([
                engine_tests.binding("work", blocking),
            ]),
            run_db_path=self.root / "plain-runs.sqlite3",
            checkpoint_db_path=self.root / "plain-checkpoints.sqlite3",
        )
        service.start(
            ir.workflow_id, {"value": 1}, idempotency_key="ahead",
            actor="local", wait=False,
        )
        deadline = time.monotonic() + 20
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(1, len(calls), "the first run never started")

        queued = service.start(
            ir.workflow_id, {"value": 1}, idempotency_key="behind",
            actor="local", wait=False,
        )
        service.cancel(
            queued.run_id, expected_revision=queued.revision,
            idempotency_key="cancel-behind", actor="local",
        )
        release.set()
        service.wait_for_background(timeout=20)
        self.assertEqual(1, len(calls), "the cancelled run ran anyway")
        self.assertEqual("cancelled", service.get(queued.run_id).status)

    def test_a_later_step_does_not_start_after_the_run_is_cancelled(self) -> None:
        """The window the queue check alone would leave open.

        By the time the first node's Handler is running, the run has passed
        every status check the engine makes. What stops the second node is the
        adapter refusing under the same lock a cancel takes.
        """

        service, ir = self.build(nodes=2)
        run = service.start(
            ir.workflow_id, {"value": 1}, idempotency_key="two-step",
            actor="local", wait=False,
        )
        self.wait_for_calls(1)
        service.cancel(
            run.run_id, expected_revision=service.get(run.run_id).revision,
            idempotency_key="cancel-mid", actor="local",
        )
        self.release.set()
        service.wait_for_background(timeout=20)
        self.assertEqual(1, len(self.calls), "the second node ran anyway")
        self.assertEqual("cancelled", service.get(run.run_id).status)

    def test_a_cancel_during_prepare_stops_the_execution_it_created(self) -> None:
        """The window between deciding to start and being cancellable.

        The lock is released across `prepare` on purpose — holding it would
        serialise every attempt this adapter runs behind one external call —
        so a cancel can land while `prepare` is in flight, and it sees no
        active entry to signal. Registering and re-reading the mark in one
        acquisition is what closes that: whatever `prepare` created is
        cancelled rather than executed.
        """

        from orbit.workflow.handlers.tools import ToolResult
        from orbit.workflow.langgraph_runtime.artifacts import LangGraphArtifactStore
        from orbit.workflow.langgraph_runtime.wiring import trusted_handlers

        preparing = threading.Event()
        proceed = threading.Event()
        self.addCleanup(proceed.set)
        events: list[str] = []

        class Adapter:
            def execute(self, request, context):
                events.append("execute")
                return ToolResult({"value": 1})

            def cancel(self, execution_ref, context):
                events.append(f"cancel:{execution_ref}")
                return None

            def recover(self, recovery_ref, context):
                return None

        fixture = engine_tests.LangGraphProductionWiringTests("run")
        registration = fixture.tool_registration(Adapter())
        manifest = registration.manifest
        # `prepare` belongs to the Tool Handler, not to the adapter under it,
        # and holding it open is the only way to stand inside the window.
        handler = registration.implementation
        original = handler.prepare

        def slow_prepare(request, context):
            preparing.set()
            proceed.wait(20)
            return original(request, context)

        handler.prepare = slow_prepare

        ir = engine_tests.workflow(
            (
                IRNode(
                    "tool0", "action",
                    (engine_tests.port("value"),), (engine_tests.port("value"),),
                    IRHandlerRef(
                        manifest.name, manifest.version, manifest.fingerprint,
                    ),
                    {"tool_name": "example.read", "tool_version": "1.0.0"},
                    (), None,
                ),
                engine_tests.node(
                    "done", inputs=("value",), kind="terminal", handler=False,
                ),
            ),
            (engine_tests.edge("last", "tool0", "done"),),
            entry=("tool0",), terminals=("done",), result=("tool0", "value"),
        )
        store = SQLiteWorkflowVersionStore(self.root / "window.sqlite3")
        store.publish(
            CompiledWorkflow(ir, definition_hash(ir), "test", "sha256:" + "c" * 64),
            expected_latest_version=0, source_format="json",
            source_text="{}", actor="test", dsl_version="1.3",
        )
        runs = self.root / "window-runs.sqlite3"
        service = LangGraphWorkflowService(
            store, trusted_handlers([registration], attempt_db_path=runs),
            run_db_path=runs,
            checkpoint_db_path=self.root / "window-checkpoints.sqlite3",
            artifact_store=LangGraphArtifactStore(runs, self.root / "window"),
        )
        run = service.start(
            ir.workflow_id, {"value": 1}, idempotency_key="window",
            actor="local", wait=False,
        )
        self.assertTrue(preparing.wait(20), "prepare never started")
        service.cancel(
            run.run_id, expected_revision=service.get(run.run_id).revision,
            idempotency_key="cancel-preparing", actor="local",
        )
        proceed.set()
        service.wait_for_background(timeout=20)

        # The Tool Handler's execution ref is `tool:{attempt_id}`, so a
        # cancel naming one is the prepared execution being cleaned up.
        self.assertTrue(
            [item for item in events if item.startswith("cancel:tool:")],
            f"the prepared execution was left behind: {events}",
        )
        self.assertNotIn("execute", events, "the Tool ran after the cancel")
        self.assertEqual("cancelled", service.get(run.run_id).status)

    def test_a_mark_outlives_any_number_of_other_cancellations(self) -> None:
        """Its life is the run's, not a guess about how long a race lasts.

        A fixed window of recent cancellations was wrong for exactly the run
        that needs it most: one cancelled during a Handler that takes minutes,
        while other runs are cancelled meanwhile. Evicting its mark would let
        its next node start.
        """

        from orbit.workflow.langgraph_runtime.wiring import _CancelledRuns

        marks = _CancelledRuns()
        marks.add("langgraph_run:slow")
        for index in range(5000):
            marks.add(f"langgraph_run:other{index}")
        self.assertIn("langgraph_run:slow", marks)

        # Released by name. One run finishing must not clear the marks of
        # every other run still being driven beside it.
        marks.discard("langgraph_run:slow")
        self.assertNotIn("langgraph_run:slow", marks)
        self.assertIn("langgraph_run:other0", marks)
        self.assertEqual(5000, len(marks))

    def test_a_drive_releases_what_it_made_the_handlers_hold(self) -> None:
        """Somebody has to say when refusing can stop, and only the drive can.

        The adapters see attempts, not runs, and a run's last attempt is only
        recognisable afterwards — so the mark's life is the drive's life, and
        the engine ends it.
        """

        service, ir = self.build()
        released: list[str] = []
        original = service.handlers.finish
        service.handlers.finish = lambda run_id: (
            released.append(run_id), original(run_id),
        )[1]

        run = service.start(
            ir.workflow_id, {"value": 1}, idempotency_key="released",
            actor="local", wait=False,
        )
        self.wait_for_calls(1)
        service.cancel(
            run.run_id, expected_revision=service.get(run.run_id).revision,
            idempotency_key="cancel-released", actor="local",
        )
        self.release.set()
        service.wait_for_background(timeout=20)
        self.assertIn(run.run_id, released)

    def test_only_the_last_overlapping_drive_releases_handler_state(self) -> None:
        """A first exit must not clear cancellation state used by its sibling."""

        service, _ir = self.build()
        released: list[str] = []
        service.handlers.finish = released.append
        run_id = "langgraph_run:overlapping"

        with service._executing(run_id):
            self.assertEqual(1, service._in_flight[run_id])
            with service._executing(run_id):
                self.assertEqual(2, service._in_flight[run_id])
            self.assertEqual(1, service._in_flight[run_id])
            self.assertEqual([], released)

        self.assertNotIn(run_id, service._in_flight)
        self.assertEqual([run_id], released)

    def test_a_run_nothing_will_drive_is_released_at_once(self) -> None:
        """A run waiting for a person is not queued behind anything.

        Only a resume can reach it again, and a resume reads the status the
        cancellation just wrote. Holding a mark for it would be holding one
        nothing ever releases.
        """

        temp_root = self.root / "waiting"
        path = temp_root / "runtime.db"
        publish_human_workflow(path)
        service = build_service(
            path, [transform_registration()],
            state_directory=temp_root / "langgraph",
        )
        released: list[str] = []
        original = service.handlers.finish
        service.handlers.finish = lambda run_id: (
            released.append(run_id), original(run_id),
        )[1]

        run = service.start(
            "workflow:human", {"value": 1}, idempotency_key="paused",
            actor="local",
        )
        self.assertEqual("interrupted", service.get(run.run_id).status)
        released.clear()
        service.cancel(
            run.run_id, expected_revision=run.revision,
            idempotency_key="cancel-paused", actor="local",
        )
        self.assertEqual([run.run_id], released)

    def test_a_join_deadline_drive_releases_handler_state(self) -> None:
        """Deadline firing bypasses `_execute` but owns the same lifecycle."""

        temp_root = self.root / "deadline-finish"
        path = temp_root / "runtime.db"
        publish_human_workflow(path)
        service = build_service(
            path, [transform_registration()],
            state_directory=temp_root / "langgraph",
        )
        run = service.start(
            "workflow:human", {"value": 1}, idempotency_key="deadline",
            actor="local",
        )
        released: list[str] = []
        original = service.handlers.finish
        service.handlers.finish = lambda run_id: (
            released.append(run_id), original(run_id),
        )[1]

        with self.assertRaises(ValueError):
            service._fire_join_deadline(
                run.run_id, service._run_ir(run), "not-a-join",
            )
        self.assertEqual([run.run_id], released)

    def test_bound_handler_keeps_its_original_positional_contract(self) -> None:
        """Adding finish_run must not shift transports or retry safety."""

        from orbit.workflow.langgraph_runtime.compiler import BoundHandler

        def cancel(_run_id: str) -> bool:
            return True

        transports = frozenset({"inline", "artifact_ref"})
        handler = BoundHandler(
            "public", "1.0.0", "sha256:" + "a" * 64,
            lambda values, config, context: values,
            cancel, transports, True,
        )
        self.assertIs(handler.cancel_run, cancel)
        self.assertEqual(transports, handler.supported_transports)
        self.assertTrue(handler.retry_safe)
        self.assertIsNone(handler.finish_run)

    def test_a_cancellation_is_not_something_to_retry(self) -> None:
        """Otherwise a retry policy would resurrect a cancelled run."""

        from orbit.workflow.langgraph_runtime.compiler import (
            LangGraphRetryableError, LangGraphRunCancelled,
        )
        from orbit.workflow.langgraph_runtime.wiring import _retryable
        from orbit.workflow.domain.durable_execution import ExecutionSafety

        manifest = SimpleNamespace(execution_safety=ExecutionSafety.REPLAY_SAFE)
        cancelled = LangGraphRunCancelled("gone")
        self.assertNotIsInstance(
            _retryable(manifest, cancelled), LangGraphRetryableError,
        )


if __name__ == "__main__":
    unittest.main()
