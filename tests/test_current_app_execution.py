"""Published CLI graphs run through the owning App without a CLI installed."""

from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from orbit.web.app import HandlerRegistration
from orbit.workflow.domain.data import PortDataPolicy, PortTransport
from orbit.workflow.domain.definitions import CompiledWorkflow, IREdge, IRNode, IRPolicy, IRResult
from orbit.workflow.domain.serialization import definition_hash
from orbit.workflow.langgraph_runtime.current_app import bind_current_app
from orbit.workflow.langgraph_runtime.execution_worker import start_execution_worker
from orbit.workflow.langgraph_runtime.harness_subagent import APP_DELEGATE_MANIFEST, AppDelegationHandler, DelegationQueue
from orbit.workflow.langgraph_runtime.service import LangGraphWorkflowService, LangGraphRunConflict
from orbit.workflow.langgraph_runtime.wiring import trusted_handlers
from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
from tests.test_agent_binding import agent_step, port, single_step_workflow


ACTOR = "session:current-app"


class CurrentAppExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "langgraph-runs.sqlite3"
        self.queue = DelegationQueue(self.path, require_execution_lease=False)
        self.registration = HandlerRegistration(
            APP_DELEGATE_MANIFEST, AppDelegationHandler(self.queue, poll_seconds=0.005),
            "app.delegate@1.0.0",
        )
        self.registry = trusted_handlers([self.registration], attempt_db_path=self.path)
        self.store = SQLiteWorkflowVersionStore(self.root / "workflows.db")

    def engine(self, ir, *, registry=None):
        self.store.publish(
            CompiledWorkflow(ir, definition_hash(ir), "test", "sha256:" + "c" * 64),
            expected_latest_version=0, source_format="json", source_text="{}",
            actor="test:author", dsl_version="1.3",
        )
        return self.reopen(registry=registry)

    def reopen(self, *, registry=None):
        return LangGraphWorkflowService(
            self.store, registry or self.registry, run_db_path=self.path,
            checkpoint_db_path=self.root / "checkpoints.sqlite3",
        )

    def eventually(self, callback):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = callback()
            if value:
                return value
            time.sleep(0.01)
        with sqlite3.connect(self.path) as db:
            rows = db.execute("select status,error from langgraph_runs").fetchall()
            attempts = db.execute("select node_id,status,error from langgraph_handler_attempts").fetchall()
            delegations = db.execute("select delegation_id,status from harness_delegations").fetchall()
        self.fail(f"timed out waiting for run/delegation: {rows}; attempts={attempts}; delegations={delegations}")

    def claim(self):
        return self.eventually(lambda: self.queue.claim(actor=ACTOR, worker_id="app-task", lease_seconds=300))

    def complete(self, item, result):
        return self.queue.complete(item["delegation_id"], actor=ACTOR, worker_id="app-task", result=result)

    def finished(self, engine, run):
        result = self.eventually(lambda: (r if (r := engine.get(run.run_id)).status in {"completed", "failed", "unknown"} else None))
        self.assertEqual("completed", result.status, result.error)
        return result

    def test_missing_cli_overridden_and_snapshot_survives_reopen(self):
        ir = single_step_workflow(agent_step())
        engine = self.engine(ir)
        self.assertFalse(engine.compatibility(ir.workflow_id)["compatible"])
        self.assertTrue(engine.compatibility(ir.workflow_id, execution_mode="current_app")["compatible"])
        run = engine.start(ir.workflow_id, {"prompt": {"goal": "translate"}},
                           actor=ACTOR, idempotency_key="one", execution_mode="current_app")
        item = self.claim()
        task = item["request"]["input"]["task"]
        self.assertEqual("do the thing", task["instructions"])
        self.assertEqual({"prompt": {"goal": "translate"}}, task["input"])
        self.assertEqual("agent.absent", task["original_handler"]["name"])
        self.assertIsNone(self.queue.claim(actor="session:other", worker_id="other"))
        self.complete(item, {"text": "translated"})
        result = self.finished(engine, run)
        self.assertEqual({"text": "translated"}, result.result)
        self.assertEqual("current_app", self.reopen().get(run.run_id).execution_mode)
        self.assertEqual("app.delegate", self.reopen()._run_ir(result).nodes[0].handler.name)
        self.assertEqual("agent.absent", self.store.get(ir.workflow_id, 1).ir.nodes[0].handler.name)
        replay = engine.start(ir.workflow_id, {"prompt": {"goal": "translate"}}, actor=ACTOR,
                              idempotency_key="one", execution_mode="current_app")
        self.assertEqual(run.run_id, replay.run_id)
        with self.assertRaises(LangGraphRunConflict):
            engine.start(ir.workflow_id, {"prompt": {"goal": "translate"}}, actor=ACTOR, idempotency_key="one")

    def test_parallel_nodes_can_both_be_leased_before_either_completes(self):
        left, right = agent_step("left"), agent_step("right")
        join = IRNode("join", "join", (port("result"),), (port("result"),), None, {}, ("all",), None)
        ir = replace(single_step_workflow(left), nodes=(left, right, join),
                     entry=("left", "right"), terminals=("join",), result=IRResult("join", "result"),
                     policies=(IRPolicy("all", "join", {"mode": "all", "merge_mode": "array_by_edge"}),),
                     edges=tuple(IREdge(n, n, "result", "join", "result", "success", {"op": "literal", "value": True}, {"op": "identity"})
                                 for n in ("left", "right")))
        registry, worker = start_execution_worker([self.registration], state_directory=self.root)
        self.addCleanup(worker.stop)
        engine = self.engine(ir, registry=registry)
        run = engine.start(ir.workflow_id, {"prompt": {}}, actor=ACTOR, idempotency_key="parallel", execution_mode="current_app")
        first, second = self.claim(), self.claim()
        self.assertNotEqual(first["delegation_id"], second["delegation_id"])
        self.assertEqual({"left", "right"}, {i["request"]["execution"]["node_id"] for i in (first, second)})
        self.complete(second, {"text": "second"})
        self.assertEqual("running", engine.get(run.run_id).status)
        self.complete(first, {"text": "first"})
        self.finished(engine, run)

    def test_artifact_output_is_materialized_and_passed_to_next_app_step(self):
        policy = PortDataPolicy(PortTransport.ARTIFACT_REF, 4096, ("text/markdown",))
        first = replace(agent_step("first"), outputs=(replace(port("result"), data_policy=policy),))
        second = replace(agent_step("second"), inputs=(replace(port("prompt"), data_policy=policy),))
        ir = replace(single_step_workflow(first), nodes=(first, second), terminals=("second",),
                     edges=(IREdge("next", "first", "result", "second", "prompt", "success", {"op": "literal", "value": True}, {"op": "identity"}),),
                     result=IRResult("second", "result"))
        engine = self.engine(ir)
        run = engine.start(ir.workflow_id, {"prompt": {}}, actor=ACTOR, idempotency_key="artifacts", execution_mode="current_app")
        self.complete(self.claim(), {"text": "# Draft"})
        second_item = self.claim()
        self.assertEqual("# Draft", second_item["request"]["input"]["task"]["input"]["prompt"])
        self.complete(second_item, {"text": "approved"})
        self.finished(engine, run)

    def test_a_port_accepting_several_types_is_judged_by_all_of_them(self):
        """`content_types` is a sorted set, not a preference order.

        Reading `[0]` as "the primary type" answered a question about
        alphabetical order: a markdown port that also accepted PDF was refused
        because `application/pdf` sorts first, and a JSON port that also
        accepted PNG was admitted because `application/json` does. What
        decides is whether an App — which produces prose or JSON and nothing
        else — can satisfy the port at all.
        """

        from orbit.workflow.langgraph_runtime.current_app import bind_current_app

        for label, declared, adaptable in (
            ("markdown, and PDF too", ("text/markdown", "application/pdf"), True),
            ("JSON, and PNG too", ("application/json", "image/png"), True),
            ("nothing an App can write", ("image/png", "application/pdf"), False),
        ):
            with self.subTest(port=label):
                policy = PortDataPolicy(PortTransport.ARTIFACT_REF, 4096, declared)
                step = replace(
                    agent_step(), outputs=(replace(port("result"), data_policy=policy),),
                )
                ir = single_step_workflow(step)
                if adaptable:
                    bind_current_app(ir, self.registry)
                else:
                    with self.assertRaisesRegex(ValueError, "text or JSON artifact"):
                        bind_current_app(ir, self.registry)

    def test_prose_is_written_as_text_even_where_json_is_also_accepted(self):
        """The result decides the type; the port only says what it accepts.

        A port accepting both `application/json` and `text/markdown` used to
        take the first of those alphabetically — so an App's document was
        stored as a JSON string containing it, and the next node read JSON
        where the author wrote prose.
        """

        policy = PortDataPolicy(
            PortTransport.ARTIFACT_REF, 4096, ("text/markdown", "application/json"),
        )
        first = replace(
            agent_step("first"), outputs=(replace(port("result"), data_policy=policy),),
        )
        second = replace(
            agent_step("second"), inputs=(replace(port("prompt"), data_policy=policy),),
        )
        ir = replace(
            single_step_workflow(first), nodes=(first, second), terminals=("second",),
            edges=(IREdge(
                "next", "first", "result", "second", "prompt", "success",
                {"op": "literal", "value": True}, {"op": "identity"},
            ),),
            result=IRResult("second", "result"),
        )
        engine = self.engine(ir)
        run = engine.start(
            ir.workflow_id, {"prompt": {}}, actor=ACTOR,
            idempotency_key="both-types", execution_mode="current_app",
        )

        self.complete(self.claim(), {"text": "# Draft"})
        second_item = self.claim()

        # The document, not `{"text": "# Draft"}` rendered as JSON.
        self.assertEqual(
            "# Draft", second_item["request"]["input"]["task"]["input"]["prompt"],
        )
        self.complete(second_item, {"text": "approved"})
        self.finished(engine, run)

    def test_invalid_mode_or_missing_app_handler_creates_no_run(self):
        from orbit.workflow.langgraph_runtime.compiler import LangGraphHandlerRegistry
        ir = single_step_workflow(agent_step())
        engine = self.engine(ir, registry=LangGraphHandlerRegistry([]))
        for mode in ("typo", None, "current_app"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                engine.start(ir.workflow_id, {"prompt": {}}, idempotency_key=str(mode), execution_mode=mode)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(0, db.execute("select count(*) from langgraph_runs").fetchone()[0])

    def test_installed_agent_is_also_overridden(self):
        from orbit.workflow.langgraph_runtime.compiler import BoundHandler, LangGraphHandlerRegistry
        step = agent_step()
        installed = BoundHandler(step.handler.name, step.handler.version, step.handler.manifest_fingerprint,
                                 lambda *_: self.fail("CLI should never run"), capabilities=frozenset({"agent.invoke"}))
        registry = LangGraphHandlerRegistry([installed, *self.registry._entries.values()])
        binding = bind_current_app(single_step_workflow(step), registry)
        self.assertEqual("app.delegate", binding.ir.nodes[0].handler.name)

    def test_human_resume_keeps_app_binding_and_returns_before_delegation(self):
        human = IRNode("review", "human", (port("prompt"),), (port("result"),), None, {}, (), None)
        action = agent_step("after_review")
        ir = replace(single_step_workflow(action), nodes=(human, action), entry=("review",),
                     edges=(IREdge("next", "review", "result", "after_review", "prompt", "success",
                                   {"op": "literal", "value": True}, {"op": "identity"}),))
        engine = self.engine(ir)
        run = engine.start(ir.workflow_id, {"prompt": {}}, actor=ACTOR,
                           idempotency_key="human", execution_mode="current_app")
        interrupted = self.eventually(lambda: (r if (r := engine.get(run.run_id)).status == "interrupted" else None))
        engine = self.reopen()
        resumed = engine.resume(run.run_id, {"result": {"approved": True}},
                                expected_revision=interrupted.revision, idempotency_key="resume", actor=ACTOR)
        self.assertEqual("current_app", resumed.execution_mode)
        item = self.claim()
        self.assertEqual({"prompt": {"approved": True}}, item["request"]["input"]["task"]["input"])
        self.complete(item, {"text": "done"})
        self.finished(engine, run)

    def test_reconciled_artifact_is_normalized_without_executing_agent_again(self):
        policy = PortDataPolicy(PortTransport.ARTIFACT_REF, 4096, ("text/markdown",))
        action = replace(agent_step(), outputs=(replace(port("result"), data_policy=policy),))
        ir = single_step_workflow(action)
        engine = self.engine(ir)
        run = engine.start(ir.workflow_id, {"prompt": {}}, actor=ACTOR,
                           idempotency_key="reconcile", execution_mode="current_app")
        item = self.claim()
        with sqlite3.connect(self.path) as db:
            db.execute("update harness_delegations set status='unknown' where delegation_id=?", (item["delegation_id"],))
        self.eventually(lambda: engine.get(run.run_id).status == "unknown")
        result = {"text": "# Confirmed"}
        self.queue.reconcile(item["delegation_id"], actor=ACTOR, outcome="confirmed_succeeded",
                             result=result, note="verified", idempotency_key="reconciled")
        engine.resolve_unknown_delegation(item["delegation_id"], actor=ACTOR,
                                          outcome="confirmed_succeeded", result=result)
        settled = self.finished(engine, run)
        self.assertIn("artifact_id", settled.result)
        self.assertIsNone(self.queue.claim(actor=ACTOR, worker_id="again"))

    def test_mcp_start_and_read_only_preview_accept_current_app(self):
        from orbit.web.api_v1 import Authorizer, READ_SCOPE, WRITE_SCOPE
        from orbit.web.mcp import build_mcp_dispatcher
        from orbit.workflow.catalogs import InMemorySchemaCatalog
        ir = single_step_workflow(agent_step())
        engine = self.engine(ir)
        dispatch = build_mcp_dispatcher(
            self.root / "workflows.db", langgraph_service=engine,
            schema_catalog=InMemorySchemaCatalog({"schema://object/1.0": {"type": "object"}}),
            authorizer=Authorizer(lambda actor: [READ_SCOPE, WRITE_SCOPE]),
        )

        def call(name, arguments):
            reply = dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}}, ACTOR)
            self.assertNotIn("error", reply, reply)
            self.assertFalse(reply["result"].get("isError"), reply)
            return reply["result"]["structuredContent"]

        preview = call("inspect_workflow_definition", {"workflow_id": ir.workflow_id, "execution_mode": "current_app"})
        self.assertTrue(preview["execution_compatibility"]["compatible"])
        started = call("start_run", {"workflow_id": ir.workflow_id, "goal": "translate this",
                                     "execution_mode": "current_app", "idempotency_key": "mcp"})
        self.assertEqual("current_app", started["execution_mode"])
        item = self.claim()
        self.assertEqual({"prompt": {"goal": "translate this"}}, item["request"]["input"]["task"]["input"])
        self.complete(item, {"text": "translated"})
        self.finished(engine, engine.get(started["run_id"]))

    def test_http_start_accepts_current_app(self):
        from orbit.web.api_v1 import Authorizer, READ_SCOPE, WRITE_SCOPE
        from orbit.web.app import create_app
        from tests.test_web_composition import AsgiHarness
        ir = single_step_workflow(agent_step())
        engine = self.engine(ir)
        app = create_app(
            self.root / "runtime.db", workflow_db_path=self.root / "workflows.db",
            handlers=[self.registration], schemas={"schema://object/1.0": {"type": "object"}},
            langgraph_service=engine, authenticator=lambda r: r.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: [READ_SCOPE, WRITE_SCOPE]),
        )
        with AsgiHarness(app) as client:
            response = client.post("/api/v1/langgraph-runs", actor=ACTOR, key="http", body={
                "workflow_id": ir.workflow_id, "input": {"prompt": {}}, "execution_mode": "current_app",
            })
            self.assertEqual(200, response.status_code, response.json())
            run = response.json()["data"]["run"]
            self.assertEqual("current_app", run["execution_mode"])
            self.complete(self.claim(), {"text": "HTTP result"})
            self.finished(engine, engine.get(run["run_id"]))

    def test_secret_reference_is_forwarded_without_resolving_a_secret(self):
        action = replace(agent_step(), inputs=(replace(
            port("prompt"), data_policy=PortDataPolicy(PortTransport.SECRET_REF),
        ),))
        ir = replace(single_step_workflow(action), inputs=action.inputs)
        engine = self.engine(ir)
        reference = {"logical_name": "publishing-credential", "version": "1"}
        run = engine.start(ir.workflow_id, {"prompt": reference}, actor=ACTOR,
                           idempotency_key="secret-ref", execution_mode="current_app")
        item = self.claim()
        self.assertEqual({"prompt": reference}, item["request"]["input"]["task"]["input"])
        self.complete(item, {"text": "used authorized App tooling"})
        self.finished(engine, run)


if __name__ == "__main__":
    unittest.main()
