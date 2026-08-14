"""Where a run got to, derived from its definition and its checkpoint.

There is no step table. The definition says what the run could do, the
checkpoint says what it did, and the difference is the progress — so most of
what is worth testing here is whether the derivation tells the truth at the
moments it would be easiest to get wrong.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
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
    publish_human_workflow, transform_registration,
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


if __name__ == "__main__":
    unittest.main()
