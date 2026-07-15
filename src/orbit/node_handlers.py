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

    def supports(self, node_type: str) -> bool:
        return node_type in self.node_types


NODE_HANDLERS = {
    "agent": NodeHandler("agent", frozenset({"action"}), "runner", True),
    "command": NodeHandler("command", frozenset({"action"}), "runner", False),
    "legacy.decompose": NodeHandler(
        "legacy.decompose", frozenset({"action"}), "runner", True
    ),
    "git.merge": NodeHandler("git.merge", frozenset({"action"}), "runner", True),
    "human": NodeHandler("human", frozenset({"approval"}), "human", False),
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
