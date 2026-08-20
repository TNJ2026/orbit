"""Single-Agent mode: one Agent runs every Agent step, in any Workflow.

The two modes share one catalog. A Workflow published against `agent.codex`
is the same Workflow in single-Agent mode — it simply runs on whichever Agent
this Runtime speaks for, and the Agent its author picked is ignored. These
tests cover the three places that has to be true: the rebinding itself, the
engine that starts a run with it, and the catalog that has to stop calling a
rebound step broken.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from orbit.workflow.agent_binding import (
    AgentRebindError,
    SingleAgentBinder,
    preferred_agent,
    rebind_agents,
)
from orbit.workflow.catalogs.agent_discovery import (
    TRUSTED_AGENT_CLIS, DiscoveredAgent, agent_manifest,
)
from orbit.workflow.domain.definitions import (
    CompiledWorkflow, IRHandlerRef, IRNode, IRPort, IRResult, WorkflowIR,
)
from orbit.workflow.domain.serialization import definition_hash
from orbit.workflow.langgraph_runtime.compiler import (
    BoundHandler, LangGraphHandlerRegistry,
)
from orbit.workflow.langgraph_runtime.service import (
    LangGraphRunConflict, LangGraphWorkflowService,
)
from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
from orbit.web.api_v1 import READ_SCOPE, WRITE_SCOPE, Authorizer

OBJECT = "schema://object/1.0"
ABSENT = IRHandlerRef("agent.absent", "9.9.9", "sha256:" + "b" * 64)


def manifest_for(name: str, version: str = "1.2.3"):
    """A real Agent manifest, minted the way discovery mints one."""

    spec = next(item for item in TRUSTED_AGENT_CLIS if item.name == name)
    return agent_manifest(
        DiscoveredAgent(spec, f"/usr/local/bin/{spec.executable}", version)
    )


def port(name: str, schema: str = OBJECT) -> IRPort:
    return IRPort(name, schema, True, False, None, "")


def agent_step(
    node_id: str = "execute",
    *,
    handler: IRHandlerRef = ABSENT,
    config=None,
    inputs=None,
    outputs=None,
) -> IRNode:
    return IRNode(
        node_id, "action",
        inputs if inputs is not None else (port("prompt"),),
        outputs if outputs is not None else (port("result"),),
        handler,
        {"prompt": "do the thing"} if config is None else config,
        (), None,
    )


def single_step_workflow(step: IRNode, workflow_id="workflow:single") -> WorkflowIR:
    return WorkflowIR(
        "1.3", workflow_id, "Single", "", {},
        (port("prompt"),), (port("result"),), (step,), (),
        (step.id,), (step.id,), (), (), {}, IRResult(step.id, "result"),
    )


class AgentInterchangeabilityTests(unittest.TestCase):
    """The fact the whole feature rests on, asserted rather than assumed.

    Rebinding is only ever safe because every Agent Handler this Runtime mints
    has the same shape. Nothing forces a new `AgentCliSpec` to keep that shape,
    so this is where somebody adding one finds out — rather than a person
    finding out at a start button that stopped working.
    """

    def test_every_trusted_agent_can_stand_in_for_every_other(self) -> None:
        manifests = [
            manifest_for(spec.name) for spec in TRUSTED_AGENT_CLIS
            if spec.runtime_compatible
        ]
        self.assertGreater(len(manifests), 1)
        first = manifests[0]
        for manifest in manifests[1:]:
            with self.subTest(agent=manifest.name):
                self.assertEqual(dict(first.inputs), dict(manifest.inputs))
                self.assertEqual(dict(first.outputs), dict(manifest.outputs))
                self.assertEqual(
                    first.config_schema["properties"].keys(),
                    manifest.config_schema["properties"].keys(),
                )
                self.assertEqual(first.node_kinds, manifest.node_kinds)
                self.assertIn("agent.invoke", manifest.capabilities)


class RebindTests(unittest.TestCase):
    def test_every_agent_step_moves_to_the_current_agent(self) -> None:
        claude = manifest_for("claude")
        ir = single_step_workflow(agent_step())
        rebinding = rebind_agents(ir, claude)

        self.assertIsNotNone(rebinding)
        self.assertEqual(("execute",), rebinding.rebound)
        self.assertEqual("agent.claude@1.2.3", rebinding.identity)
        self.assertEqual(
            IRHandlerRef("agent.claude", "1.2.3", claude.fingerprint),
            rebinding.ir.nodes[0].handler,
        )
        # The step's own instruction is not the Agent's, and does not move.
        self.assertEqual("do the thing", rebinding.ir.nodes[0].config["prompt"])

    def test_steps_that_are_not_agents_are_left_alone(self) -> None:
        transform = IRNode(
            "shape", "action", (port("result"),), (port("result"),),
            IRHandlerRef("transform.identity", "1.0.0", "sha256:" + "d" * 64),
            {}, (), None,
        )
        ir = WorkflowIR(
            "1.3", "workflow:mixed", "Mixed", "", {},
            (port("prompt"),), (port("result"),),
            (agent_step(), transform), (),
            ("execute",), ("shape",), (), (), {},
            IRResult("shape", "result"),
        )

        rebinding = rebind_agents(ir, manifest_for("claude"))

        self.assertEqual(("execute",), rebinding.rebound)
        self.assertEqual(transform.handler, rebinding.ir.nodes[1].handler)

    def test_a_graph_already_on_this_agent_needs_no_rebinding(self) -> None:
        claude = manifest_for("claude")
        reference = IRHandlerRef("agent.claude", "1.2.3", claude.fingerprint)
        ir = single_step_workflow(agent_step(handler=reference))

        self.assertIsNone(rebind_agents(ir, claude))

    def test_a_graph_with_no_agent_step_needs_no_agent(self) -> None:
        transform = IRNode(
            "shape", "action", (port("prompt"),), (port("result"),),
            IRHandlerRef("transform.identity", "1.0.0", "sha256:" + "d" * 64),
            {}, (), None,
        )
        ir = single_step_workflow(transform, workflow_id="workflow:plain")

        self.assertIsNone(rebind_agents(ir, manifest_for("claude")))
        # And through the binder, where there is no Agent to be found at all:
        # a Workflow that never wanted one must still start.
        self.assertIsNone(SingleAgentBinder([])(ir))

    def test_a_port_the_agent_does_not_offer_is_refused(self) -> None:
        ir = single_step_workflow(agent_step(inputs=(port("payload"),)))

        with self.assertRaisesRegex(AgentRebindError, "input ports"):
            rebind_agents(ir, manifest_for("claude"))

    def test_a_budget_above_the_new_agents_ceiling_is_lowered(self) -> None:
        claude = manifest_for("claude")
        ceiling = claude.resource_profile.max_duration_seconds
        ir = single_step_workflow(agent_step(
            config={"prompt": "do it", "timeout_seconds": ceiling + 600},
        ))

        rebound = rebind_agents(ir, claude).ir.nodes[0]

        self.assertEqual(ceiling, rebound.config["timeout_seconds"])
        # A budget the new Agent can honour is the author's, and stays.
        under = single_step_workflow(agent_step(
            config={"prompt": "do it", "timeout_seconds": 60},
        ))
        self.assertEqual(
            60, rebind_agents(under, claude).ir.nodes[0].config["timeout_seconds"],
        )


class AgentSelectionTests(unittest.TestCase):
    def test_a_connected_client_names_the_agent(self) -> None:
        agents = [manifest_for("claude"), manifest_for("codex")]

        self.assertEqual(
            "agent.codex", preferred_agent(agents, ["codex"]).name,
        )
        # The client's name for itself is not always the CLI's.
        self.assertEqual(
            "agent.codex", preferred_agent(agents, ["chatgpt"]).name,
        )

    def test_one_registered_agent_needs_no_client(self) -> None:
        agents = [manifest_for("claude")]

        self.assertEqual("agent.claude", preferred_agent(agents, []).name)

    def test_two_agents_and_no_client_is_ambiguous(self) -> None:
        agents = [manifest_for("claude"), manifest_for("codex")]

        self.assertIsNone(preferred_agent(agents, []))
        with self.assertRaisesRegex(AgentRebindError, "which Agent"):
            SingleAgentBinder(agents)(single_step_workflow(agent_step()))

    def test_no_agent_at_all_says_so(self) -> None:
        with self.assertRaisesRegex(AgentRebindError, "no Agent Handler"):
            SingleAgentBinder([])(single_step_workflow(agent_step()))


class SingleAgentEngineTests(unittest.TestCase):
    """What a start does when the Workflow names an Agent that is not here."""

    def engine(self, root: Path, ir: WorkflowIR, *, manifests, rebind=True):
        store = SQLiteWorkflowVersionStore(root / "workflows.db")
        store.publish(
            CompiledWorkflow(ir, definition_hash(ir), "test", "sha256:" + "c" * 64),
            expected_latest_version=0, source_format="json", source_text="{}",
            actor="test:author", dsl_version="1.3",
        )
        return LangGraphWorkflowService(
            store,
            LangGraphHandlerRegistry([
                BoundHandler(
                    manifest.name, manifest.version, manifest.fingerprint,
                    lambda values, config, context: {
                        "result": {"prompt": config.get("prompt")},
                    },
                )
                for manifest in manifests
            ]),
            run_db_path=root / "runs.sqlite3",
            checkpoint_db_path=root / "checkpoints.sqlite3",
            rebind=SingleAgentBinder(manifests) if rebind else None,
            clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    def test_a_workflow_pinned_to_a_missing_agent_runs_on_this_one(self) -> None:
        claude = manifest_for("claude")
        ir = single_step_workflow(agent_step())
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            engine = self.engine(Path(directory), ir, manifests=[claude])

            self.assertEqual(
                {
                    "compatible": True, "workflow_version": 1,
                    "engine": "langgraph", "agent_binding": "agent.claude@1.2.3",
                },
                engine.compatibility("workflow:single"),
            )
            run = engine.start(
                "workflow:single", {"prompt": {"goal": "x"}},
                idempotency_key="start-1", actor="local",
            )

            self.assertEqual("completed", run.status)
            self.assertEqual({"prompt": "do the thing"}, run.result)

    def test_the_same_workflow_refuses_without_the_binding(self) -> None:
        """The premise: this is a Workflow multi-Agent mode genuinely cannot run."""

        claude = manifest_for("claude")
        ir = single_step_workflow(agent_step())
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            engine = self.engine(
                Path(directory), ir, manifests=[claude], rebind=False,
            )

            answer = engine.compatibility("workflow:single")

            self.assertFalse(answer["compatible"])
            self.assertIn("agent.absent", answer["detail"])

    def test_the_bound_graph_is_written_down_with_the_run(self) -> None:
        """Or a recovered run would revert to the Agent it was told to ignore.

        A run is named by `(workflow_id, version)`, and everything that reads a
        run's definition later — resume, recovery, the step projection — starts
        from that name. The rebinding happened once, at the start; if it is not
        stored, the half of the run that outlives this process runs on the
        published Agent instead.
        """

        claude = manifest_for("claude")
        ir = single_step_workflow(agent_step())
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            engine = self.engine(Path(directory), ir, manifests=[claude])
            run = engine.start(
                "workflow:single", {"prompt": {"goal": "x"}},
                idempotency_key="start-1", actor="local",
            )

            stored = engine._run_ir(engine.get(run.run_id))

            self.assertEqual(
                IRHandlerRef("agent.claude", "1.2.3", claude.fingerprint),
                stored.nodes[0].handler,
            )

    def test_the_run_remembers_who_ran_it_after_the_binding_moves(self) -> None:
        """A finished run is auditable by what ran it, not by what runs today."""

        claude, codex = manifest_for("claude"), manifest_for("codex")
        ir = single_step_workflow(agent_step())
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            engine = self.engine(Path(directory), ir, manifests=[claude])
            run = engine.start(
                "workflow:single", {"prompt": {"goal": "x"}},
                idempotency_key="start-1", actor="local",
            )
            self.assertEqual("agent.claude@1.2.3", run.agent_binding)

            # The Runtime moves on; the run does not.
            engine.rebind = SingleAgentBinder([codex])

            self.assertEqual(
                "agent.claude@1.2.3", engine.get(run.run_id).agent_binding,
            )
            self.assertEqual(
                "agent.claude@1.2.3", engine.list_runs()[0].agent_binding,
            )

    def test_a_run_that_rebinds_nothing_stores_no_graph(self) -> None:
        """A Workflow with no Agent step is the same run in either mode."""

        transform = IRNode(
            "shape", "action", (port("prompt"),), (port("result"),),
            IRHandlerRef("transform.identity", "1.0.0", "sha256:" + "d" * 64),
            {}, (), None,
        )
        manifest = manifest_for("claude")
        ir = single_step_workflow(transform, workflow_id="workflow:plain")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            engine = self.engine(root, ir, manifests=[manifest])
            # Registered under the name the graph actually pins.
            engine.handlers = LangGraphHandlerRegistry([BoundHandler(
                "transform.identity", "1.0.0", "sha256:" + "d" * 64,
                lambda values, config, context: {"result": {"ok": True}},
            )])
            run = engine.start(
                "workflow:plain", {"prompt": {"goal": "x"}},
                idempotency_key="start-1", actor="local",
            )

            with engine._connect() as connection:
                snapshot = connection.execute(
                    "SELECT graph_snapshot_json FROM langgraph_runs WHERE run_id=?",
                    (run.run_id,),
                ).fetchone()["graph_snapshot_json"]

            self.assertIsNone(snapshot)
            self.assertIsNone(engine.get(run.run_id).agent_binding)

    def test_replaying_a_start_after_the_agent_changed_is_a_conflict(self) -> None:
        """The receipt has to know which Agent ran, or it hands back the wrong run."""

        claude, codex = manifest_for("claude"), manifest_for("codex")
        ir = single_step_workflow(agent_step())
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            engine = self.engine(Path(directory), ir, manifests=[claude])
            engine.start(
                "workflow:single", {"prompt": {"goal": "x"}},
                idempotency_key="start-1", actor="local",
            )
            engine.rebind = SingleAgentBinder([codex])

            with self.assertRaises(LangGraphRunConflict):
                engine.start(
                    "workflow:single", {"prompt": {"goal": "x"}},
                    idempotency_key="start-1", actor="local",
                )

    def test_an_ambiguous_binding_is_reported_as_the_runtimes_fault(self) -> None:
        ir = single_step_workflow(agent_step())
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            engine = self.engine(
                Path(directory), ir,
                manifests=[manifest_for("claude"), manifest_for("codex")],
            )

            answer = engine.compatibility("workflow:single")

            self.assertFalse(answer["compatible"])
            self.assertEqual("agent_binding_unavailable", answer["reason"])


class SingleAgentCatalogTests(unittest.TestCase):
    """What the catalog says about a step that is going to be rebound.

    A pinned Agent that is not installed is drift everywhere else, and drift
    is offered a recompile. In single-Agent mode it is neither: the step needs
    no repair, because the start it is waiting for will rebind it.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "runtime.db"
        self.claude = manifest_for("claude")
        ir = single_step_workflow(agent_step())
        SQLiteWorkflowVersionStore(self.db).publish(
            CompiledWorkflow(ir, definition_hash(ir), "test", "sha256:" + "c" * 64),
            expected_latest_version=0, source_format="json", source_text="{}",
            actor="test:author", dsl_version="1.3",
        )

    def app(self, ui_mode: str):
        from orbit.web.app import HandlerRegistration, create_app
        from orbit.workflow.handlers import TransformHandler
        from tests.test_web_composition import SCHEMAS, transform_registration

        return create_app(
            self.db,
            handlers=[
                transform_registration(),
                HandlerRegistration(
                    self.claude, TransformHandler(),
                    f"{self.claude.name}@{self.claude.version}",
                ),
            ],
            schemas=SCHEMAS, poll_seconds=0.02,
            authenticator=lambda request: request.headers.get("x-orbit-actor"),
            authorizer=Authorizer(lambda actor: [READ_SCOPE, WRITE_SCOPE]),
            single_goal_mode=False,
            workflow_ui_mode=ui_mode,
            langgraph_state_directory=self.root / ui_mode,
        )

    def catalog(self, ui_mode: str):
        from tests.test_web_composition import AsgiHarness

        with AsgiHarness(self.app(ui_mode)) as client:
            workflows = client.get(
                "/api/v1/workflows", actor="local",
            ).json()["data"]["workflows"]
            capabilities = client.get(
                "/api/v1/capabilities", actor="local",
            ).json()["data"]
        return next(
            item for item in workflows if item["workflow_id"] == "workflow:single"
        ), capabilities

    def test_a_rebound_step_is_not_drift(self) -> None:
        item, capabilities = self.catalog("single-agent")

        binding = item["handler_compatibility"]["bindings"][0]
        self.assertEqual("rebound", binding["status"])
        self.assertEqual("agent.claude", binding["rebound_to"])
        self.assertTrue(item["handler_compatibility"]["compatible"])
        self.assertTrue(item["langgraph_compatibility"]["compatible"])
        self.assertEqual("ready", item["goal_readiness"])
        self.assertIsNone(item["readiness_reason"])
        self.assertEqual(
            {"handler_name": "agent.claude", "version": "1.2.3"},
            capabilities["product_mode"]["agent_binding"],
        )

    def test_multi_agent_mode_still_calls_it_missing(self) -> None:
        """The same catalog, the same Workflow, and the honest older answer."""

        item, capabilities = self.catalog("multi-agent")

        binding = item["handler_compatibility"]["bindings"][0]
        self.assertEqual("missing", binding["status"])
        self.assertFalse(item["handler_compatibility"]["compatible"])
        self.assertEqual("handler_binding_unavailable", item["readiness_reason"])
        self.assertIsNone(capabilities["product_mode"]["agent_binding"])


if __name__ == "__main__":
    unittest.main()
