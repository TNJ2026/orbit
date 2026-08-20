"""Static single-Agent graph templates instantiated directly into Run snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Mapping, Sequence
import uuid

from .agent_binding import is_agent_node, preferred_agent, rebind_agents
from .application.workflows import WorkflowDefinitionService
from .api.workflow_catalog import WorkflowCatalogReadModelService
from .catalogs import HandlerManifest


@dataclass(frozen=True)
class WorkflowTemplate:
    template_id: str
    name: str
    description: str
    steps: tuple[tuple[str, str, str], ...]
    edges: tuple[tuple[str, str], ...]


TEMPLATES = (
    WorkflowTemplate(
        "direct", "直接执行", "由当前 Agent 完成目标并返回结果。",
        (("execute", "agent", "执行目标"), ("done", "terminal", "完成")),
        (("execute", "done"),),
    ),
    WorkflowTemplate(
        "plan-execute", "规划后执行", "先制定计划，再由同一个 Agent 执行。",
        (("plan", "agent", "制定计划"), ("execute", "agent", "执行计划"),
         ("done", "terminal", "完成")),
        (("plan", "execute"), ("execute", "done")),
    ),
    WorkflowTemplate(
        "execute-review", "执行后人工审核", "Agent 完成目标后等待人工审核。",
        (("execute", "agent", "执行目标"), ("review", "human", "人工审核"),
         ("done", "terminal", "完成")),
        (("execute", "review"), ("review", "done")),
    ),
)


class SingleAgentTemplateService:
    def __init__(
        self,
        definitions: WorkflowDefinitionService,
        manifests: Sequence[HandlerManifest],
        langgraph_service,
        connected_clients: Callable[[], Sequence[str]],
    ) -> None:
        self.definitions = definitions
        self.manifests = {item.name: item for item in manifests}
        self.langgraph = langgraph_service
        self.connected_clients = connected_clients

    def _binding(self) -> tuple[str, HandlerManifest] | None:
        clients = sorted(self.connected_clients())
        if not clients:
            return None
        # One policy for *which* Agent, shared with single-Agent mode's start
        # path. Readiness stays this service's own rule: a template run is
        # started by a connected Agent App, so no client means not ready even
        # where the Runtime could name an Agent perfectly well.
        manifest = preferred_agent(tuple(self.manifests.values()), clients[:1])
        return None if manifest is None else (clients[0], manifest)

    def list(self) -> Mapping[str, Any]:
        binding = self._binding()
        published = []
        if self.definitions.store is not None:
            catalog = WorkflowCatalogReadModelService(
                self.definitions.store.path, self.definitions.catalogs.schemas,
            )
            for item in catalog.list():
                if item["labels"].get("orbit.mode") != "single-agent-template":
                    continue
                detail = catalog.detail(item["workflow_id"])
                published.append({
                    "workflow_id": item["workflow_id"], "name": item["name"],
                    "description": item["description"],
                    "template_id": item["labels"].get("orbit.template"),
                    "graph": detail["graph"],
                })
        return {
            "connected_agent": None if binding is None else binding[0],
            "ready": binding is not None,
            "templates": [self._public(item) for item in TEMPLATES],
            "published": published,
        }

    @staticmethod
    def _public(template: WorkflowTemplate) -> Mapping[str, Any]:
        return {
            "template_id": template.template_id,
            "name": template.name,
            "description": template.description,
            "parameters": [],
            "graph": {
                "nodes": [
                    {"id": node_id, "type": kind, "label": label}
                    for node_id, kind, label in template.steps
                ],
                "edges": [
                    {"from": source, "to": target}
                    for source, target in template.edges
                ],
            },
        }

    def start(
        self, template_id: str | None, goal: str, *, actor: str,
        idempotency_key: str, workflow_id: str | None = None,
    ):
        goal = goal.strip()
        if not goal:
            raise ValueError("goal is required")
        binding = self._binding()
        if binding is None:
            raise ValueError("no unambiguous connected MCP Agent Handler")
        _agent_name, manifest = binding
        if workflow_id:
            version = self.definitions.store.latest_version(workflow_id)
            record = self.definitions.get_workflow_version(workflow_id, version)
            if record is None or record.ir.labels.get("orbit.mode") != "single-agent-template":
                raise ValueError(f"published template workflow not found: {workflow_id!r}")
            ir = self._bind_current_agent(record.ir, manifest)
            template_id = record.ir.labels.get("orbit.template") or "published"
            run_workflow_id = workflow_id
        else:
            template = self._template(template_id)
            compiled = self.definitions.validate_workflow(
                json.dumps(self._document(template, manifest), ensure_ascii=False),
                source_name=f"<template:{template_id}>", source_format="json",
            )
            ir = compiled.ir
            run_workflow_id = f"template:{template_id}"
        return self.langgraph.start_snapshot(
            run_workflow_id, ir, {"prompt": {"goal": goal}},
            template_id=template_id, idempotency_key=idempotency_key, actor=actor,
            # Bound into the graph's input above and kept beside the run here:
            # the first is what the workflow reads, the second is what a person
            # reads on a list of their own goals.
            goal=goal,
        )

    def publish(self, template_id: str, name: str, *, actor: str):
        name = name.strip()
        if not name:
            raise ValueError("workflow name is required")
        if len(name) > 80:
            raise ValueError("workflow name must be at most 80 characters")
        binding = self._binding()
        if binding is None:
            raise ValueError("no unambiguous connected MCP Agent Handler")
        template = self._template(template_id)
        workflow_id = "single-" + uuid.uuid4().hex
        document = self._document(template, binding[1])
        document["metadata"].update({
            "id": workflow_id, "name": name,
            "labels": {
                "orbit.mode": "single-agent-template",
                "orbit.template": template_id,
                "orbit.agent_binding": "current",
            },
        })
        return self.definitions.publish_workflow(
            json.dumps(document, ensure_ascii=False),
            source_name=f"<template:{template_id}>", source_format="json",
            expected_latest_version=0, actor=actor,
        )

    @staticmethod
    def _template(template_id: str | None) -> WorkflowTemplate:
        template = next(
            (item for item in TEMPLATES if item.template_id == template_id), None
        )
        if template is None:
            raise ValueError(f"unknown workflow template: {template_id!r}")
        return template

    @staticmethod
    def _bind_current_agent(ir, manifest: HandlerManifest):
        """The published template, run by whichever Agent is current.

        The same rebinding every Workflow gets in single-Agent mode, plus one
        rule of its own: a *template* with no Agent node left is not a
        template, so an empty graph is an error here rather than a graph that
        simply needs no work.
        """

        if not any(is_agent_node(node) for node in ir.nodes):
            raise ValueError("published single-Agent workflow has no Agent node")
        rebinding = rebind_agents(ir, manifest)
        return ir if rebinding is None else rebinding.ir

    @staticmethod
    def _document(template: WorkflowTemplate, manifest: HandlerManifest) -> dict[str, Any]:
        input_id, input_schema = next(iter(manifest.inputs.items()))
        output_id, output_schema = next(iter(manifest.outputs.items()))
        nodes = []
        previous_schema = input_schema
        for node_id, kind, label in template.steps:
            if kind == "agent":
                nodes.append({
                    "id": node_id, "kind": "action", "label": label,
                    "inputs": [{"id": input_id, "schema_id": previous_schema}],
                    "outputs": [{"id": output_id, "schema_id": output_schema}],
                    "handler": {"name": manifest.name, "version": manifest.version},
                    "config": {"prompt": label},
                })
                previous_schema = output_schema
            elif kind == "human":
                nodes.append({
                    "id": node_id, "kind": "human", "label": label,
                    "inputs": [{"id": output_id, "schema_id": previous_schema}],
                    "outputs": [{"id": output_id, "schema_id": output_schema}],
                    "config": {"task_kind": "approval", "participants": ["local"],
                               "quorum": "any"},
                })
                previous_schema = output_schema
            else:
                nodes.append({
                    "id": node_id, "kind": "terminal", "label": label,
                    "inputs": [{"id": output_id, "schema_id": previous_schema}],
                })
        node_by_id = {item[0]: item[1] for item in template.steps}
        edges = []
        for index, (source, target) in enumerate(template.edges):
            source_port = output_id
            target_port = input_id if node_by_id[target] == "agent" else output_id
            edges.append({
                "id": f"edge-{index + 1}",
                "from": {"node": source, "port": source_port},
                "to": {"node": target, "port": target_port},
            })
        first = template.steps[0][0]
        last_agent = next(item[0] for item in reversed(template.steps) if item[1] != "terminal")
        return {
            "dsl_version": "1.3",
            "metadata": {"id": f"template-{template.template_id}", "name": template.name},
            "inputs": [{"id": input_id, "schema_id": input_schema}],
            "nodes": nodes, "edges": edges, "entry": [first],
            "terminals": [item[0] for item in template.steps if item[1] == "terminal"],
            "result": {"node": last_agent, "port": output_id},
        }
