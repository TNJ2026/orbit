from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from orbit.workflow.application.workflows import WorkflowCatalogs, WorkflowDefinitionService
from orbit.workflow.catalogs import HandlerManifest, InMemoryHandlerCatalog, InMemorySchemaCatalog
from orbit.workflow.catalogs.extensions import InMemoryExtensionRegistry
from orbit.workflow.domain.durable_execution import ExecutionSafety
from orbit.workflow.domain.handlers import ResourceProfile
from orbit.workflow.templates import SingleAgentTemplateService, TEMPLATES
from orbit.workflow.authoring import ExternalAuthoringBroker
from orbit.workflow.handlers.agent import AgentHandler, AgentResponse, FakeAgentClient
from orbit.web.api_v1 import READ_SCOPE, WRITE_SCOPE, Authorizer
from orbit.web.app import HandlerRegistration, create_app
from tests.test_web_composition import AsgiHarness


AGENT = HandlerManifest(
    "agent.codex", "1.0.0", ("action",),
    {"prompt": "schema://object/1.0"}, {"result": "schema://object/1.0"},
    {"type": "object"}, ExecutionSafety.UNKNOWN_ON_LEASE_LOSS,
    ResourceProfile(100_000, 100_000, 0, 300, 0, "agent"),
    "schema://object/1.0", ("agent.invoke",), (), True, True,
)


class FakeLangGraph:
    def __init__(self) -> None:
        self.calls = []

    def start_snapshot(self, workflow_id, ir, inputs, **kwargs):
        self.calls.append((workflow_id, ir, inputs, kwargs))
        return {"started": True}


class SingleAgentTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        catalogs = WorkflowCatalogs(
            InMemoryHandlerCatalog([AGENT]),
            InMemorySchemaCatalog({"schema://object/1.0": {"type": "object"}}),
            InMemoryExtensionRegistry(),
        )
        self.langgraph = FakeLangGraph()
        self.clients = ["chatgpt"]
        self.service = SingleAgentTemplateService(
            WorkflowDefinitionService(catalogs), [AGENT], self.langgraph,
            lambda: self.clients,
        )

    def test_lists_static_templates_and_current_mcp_agent(self) -> None:
        result = self.service.list()
        self.assertTrue(result["ready"])
        self.assertEqual("app:chatgpt", result["connected_agent"])
        self.assertEqual(
            [item.template_id for item in TEMPLATES],
            [item["template_id"] for item in result["templates"]],
        )

    def test_every_template_compiles_and_starts_as_a_run_snapshot(self) -> None:
        for template in TEMPLATES:
            with self.subTest(template=template.template_id):
                self.service.start(
                    template.template_id, "ship it", actor="local",
                    idempotency_key=f"start:{template.template_id}",
                )
        self.assertEqual(len(TEMPLATES), len(self.langgraph.calls))
        for workflow_id, ir, inputs, kwargs in self.langgraph.calls:
            self.assertTrue(workflow_id.startswith("template:"))
            self.assertEqual({"prompt": {"goal": "ship it"}}, inputs)
            self.assertEqual({"agent.codex"}, {
                node.handler.name for node in ir.nodes if node.handler is not None
            })
            self.assertEqual(0, kwargs.get("workflow_version", 0))

    def test_refuses_to_start_without_a_connected_agent(self) -> None:
        self.clients.clear()
        self.assertFalse(self.service.list()["ready"])
        with self.assertRaisesRegex(ValueError, "connected MCP Agent"):
            self.service.start(
                "direct", "ship it", actor="local", idempotency_key="start:none",
            )

    def test_single_agent_http_starts_a_template_without_publishing(self) -> None:
        broker = ExternalAuthoringBroker()
        broker.claim(actor="test:agent", client="chatgpt")
        client = FakeAgentClient(AgentResponse({"result": {"ok": True}}, None, None))
        registration = HandlerRegistration(
            AGENT, AgentHandler(client), "agent.codex@1.0.0",
        )
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                Path(directory) / "runtime.db",
                workflow_db_path=Path(directory) / "workflows.db",
                handlers=[registration],
                schemas={"schema://object/1.0": {"type": "object"}},
                authoring_broker=broker,
                authenticator=lambda request: request.headers.get("x-orbit-actor"),
                authorizer=Authorizer(lambda _actor: (READ_SCOPE, WRITE_SCOPE)),
                langgraph_state_directory=directory,
                workflow_ui_mode="single-agent", legacy_execution=False,
                discover_agents=False,
            )
            with AsgiHarness(app) as http:
                listed = http.get(
                    "/api/v1/workflow-templates", actor="test:operator",
                )
                self.assertEqual(200, listed.status_code, listed.text)
                data = listed.json()["data"]
                self.assertEqual("app:chatgpt", data["connected_agent"])
                command = data["templates"][0]["allowed_commands"][0]
                started = http.post(
                    command["href"], actor="test:operator", key="template-http",
                    body={"template_id": "direct", "goal": "ship it"},
                )

            self.assertEqual(200, started.status_code, started.text)
            run = started.json()["data"]["run"]
            self.assertEqual("direct", run["template_id"])
            self.assertEqual(0, run["workflow_version"])
            self.assertEqual("completed", run["status"])


if __name__ == "__main__":
    unittest.main()
