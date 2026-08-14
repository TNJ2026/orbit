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
import tempfile
import time
import unittest

from orbit.workflow.domain.definitions import (
    CompiledWorkflow, IRHandlerRef, IRNode, IRPolicy,
)
from orbit.workflow.domain.serialization import definition_hash
from orbit.workflow.langgraph_runtime import build_service
from orbit.workflow.langgraph_runtime.compiler import LangGraphHandlerRegistry
from orbit.workflow.langgraph_runtime.service import LangGraphWorkflowService
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
            value={port["id"]: {"decision": "approve"} for port in ports},
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


if __name__ == "__main__":
    unittest.main()
