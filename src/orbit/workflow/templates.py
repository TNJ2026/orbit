"""Static single-Agent graph templates instantiated directly into Run snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Mapping, Sequence

from .application.workflows import WorkflowDefinitionService
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
        client = clients[0]
        aliases = {"chatgpt": "codex", "claude-desktop": "claude"}
        preferred = f"agent.{aliases.get(client, client)}"
        agents = sorted(
            (
                item for item in self.manifests.values()
                if "agent.invoke" in item.capabilities
            ),
            key=lambda item: item.name,
        )
        manifest = self.manifests.get(preferred)
        if manifest is not None and "agent.invoke" in manifest.capabilities:
            return f"app:{client}", manifest
        if len(agents) == 1:
            return f"app:{client}", agents[0]
        return None

    def list(self) -> Mapping[str, Any]:
        binding = self._binding()
        return {
            "connected_agent": None if binding is None else binding[0],
            "ready": binding is not None,
            "templates": [self._public(item) for item in TEMPLATES],
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
        self, template_id: str, goal: str, *, actor: str, idempotency_key: str,
    ):
        goal = goal.strip()
        if not goal:
            raise ValueError("goal is required")
        template = next(
            (item for item in TEMPLATES if item.template_id == template_id), None
        )
        if template is None:
            raise ValueError(f"unknown workflow template: {template_id!r}")
        binding = self._binding()
        if binding is None:
            raise ValueError("no unambiguous connected MCP Agent Handler")
        _agent_name, manifest = binding
        document = self._document(template, manifest)
        compiled = self.definitions.validate_workflow(
            json.dumps(document, ensure_ascii=False),
            source_name=f"<template:{template_id}>", source_format="json",
        )
        return self.langgraph.start_snapshot(
            f"template:{template_id}", compiled.ir, {"prompt": {"goal": goal}},
            template_id=template_id, idempotency_key=idempotency_key, actor=actor,
        )

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
