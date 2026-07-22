"""The drawable graph of an unpublished draft.

A draft is authored DSL, not compiled IR, so the published catalog's
projection cannot read it: the DSL names an edge's ends ``from.node``/
``to.node``. This translates the authored document into the same plan dialect
the catalog emits, so the editor and the workflow detail page draw the same
picture from the same renderer and the same server-side layout.

A draft is allowed to be wrong — that is what the editor is for. Anything the
renderer could not place (bad JSON, missing nodes, edges pointing nowhere)
yields ``None`` and the editor falls back to the node list.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .graph_layout import graph_layout


def draft_graph(source: str | None) -> dict[str, Any] | None:
    if not source:
        return None
    try:
        document = json.loads(source)
    except (TypeError, ValueError):
        return None
    if not isinstance(document, Mapping):
        return None
    try:
        nodes = _nodes(document)
        edges = _edges(document, {node["node_id"] for node in nodes})
    except (AttributeError, KeyError, TypeError):
        return None
    if not nodes:
        return None
    return {
        "nodes": nodes,
        "edges": edges,
        "entry": [str(value) for value in document.get("entry") or ()],
        "terminals": [str(value) for value in document.get("terminals") or ()],
        "layout": graph_layout([node["node_id"] for node in nodes], edges),
    }


def _nodes(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    nodes = []
    # The compiler canonicalizes by id, so ordering here by id too keeps a
    # draft in the same lanes as the version it publishes into.
    for node in sorted(document.get("nodes") or (), key=lambda item: str(item["id"])):
        handler = node.get("handler") or {}
        nodes.append({
            "node_id": str(node["id"]),
            "kind": str(node["kind"]),
            "handler_name": handler.get("name"),
            "handler_version": handler.get("version"),
        })
    return nodes


def _edges(
    document: Mapping[str, Any], known: set[str],
) -> list[dict[str, Any]]:
    edges = []
    for edge in sorted(document.get("edges") or (), key=lambda item: str(item["id"])):
        source = str((edge.get("from") or {})["node"])
        target = str((edge.get("to") or {})["node"])
        # An edge to a node that does not exist has nowhere to be drawn; the
        # compiler will say so, the picture just leaves it out.
        if source not in known or target not in known:
            continue
        edges.append({
            "edge_id": str(edge["id"]),
            "from": source,
            "to": target,
            "route": edge.get("route", "success"),
            "priority": edge.get("priority", 0),
            "back_edge": bool(edge.get("back_edge", False)),
        })
    return edges
