"""Built-in workflow node-handler registry.

The current runner remains asynchronous, so handlers declare dispatch semantics
rather than owning subprocesses yet.  This is the compatibility seam that lets
the engine add handlers without teaching workflow routing about node IDs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NodeHandler:
    name: str
    node_types: frozenset[str]
    dispatch_mode: str
    requires_agent: bool
    max_agents: int = 1
    accepts_command: bool = False
    public: bool = True

    def supports(self, node_type: str) -> bool:
        return node_type in self.node_types


NODE_HANDLERS = {
    "agent": NodeHandler("agent", frozenset({"action"}), "runner", True, 3),
    "command": NodeHandler(
        "command", frozenset({"action"}), "runner", False, accepts_command=True
    ),
    "legacy.decompose": NodeHandler(
        "legacy.decompose", frozenset({"action"}), "runner", True, public=False
    ),
    "git.merge": NodeHandler(
        "git.merge", frozenset({"action"}), "runner", True, public=False
    ),
    "human": NodeHandler("human", frozenset({"approval"}), "human", False),
    "decision": NodeHandler("decision", frozenset({"decision"}), "decision", False),
    "join": NodeHandler("join", frozenset({"join"}), "join", False),
    "foreach": NodeHandler("foreach", frozenset({"foreach"}), "foreach", True, 3),
    "end": NodeHandler("end", frozenset({"end"}), "end", False),
}


def get_node_handler(step: dict[str, Any]) -> NodeHandler:
    name = str(step.get("handler") or "agent")
    handler = NODE_HANDLERS.get(name)
    if handler is None:
        raise ValueError(f"unknown workflow handler: {name}")
    node_type = str(step.get("type") or "action")
    if not handler.supports(node_type):
        raise ValueError(
            f"workflow handler {name!r} does not support node type {node_type!r}"
        )
    return handler


def handler_requires_agent(step: dict[str, Any]) -> bool:
    return get_node_handler(step).requires_agent


def workflow_node_schema() -> dict[str, Any]:
    """Public authoring capabilities consumed by the workflow editor."""
    node_types = [
        {
            "id": "action",
            "default_handler": "agent",
            "default_ports": ["success"],
            "default_port": "success",
        },
        {
            "id": "approval",
            "default_handler": "human",
            "default_ports": ["approved", "changes_requested", "cancelled"],
            "default_port": "approved",
        },
        {
            "id": "decision",
            "default_handler": "decision",
            "default_ports": ["matched", "default"],
            "default_port": "default",
        },
        {
            "id": "join",
            "default_handler": "join",
            "default_ports": ["success"],
            "default_port": "success",
        },
        {
            "id": "foreach",
            "default_handler": "foreach",
            "default_ports": ["success"],
            "default_port": "success",
        },
        {
            "id": "end",
            "default_handler": "end",
            "default_ports": ["success"],
            "default_port": "success",
        },
    ]
    handlers = [
        {
            "id": handler.name,
            "node_types": sorted(handler.node_types),
            "dispatch_mode": handler.dispatch_mode,
            "requires_agent": handler.requires_agent,
            "max_agents": handler.max_agents,
            "accepts_command": handler.accepts_command,
        }
        for handler in NODE_HANDLERS.values()
        if handler.public
    ]
    return {"node_types": node_types, "handlers": handlers, "max_agents": 3}
