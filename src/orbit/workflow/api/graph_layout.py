"""One layout for every picture of a Workflow graph.

The workflow catalog and a run's plan draw the same definition at different
moments — the catalog before it ever ran, the plan with statuses on top. They
must place a node in the same spot, or switching between them reads as the
graph having changed. So the layout lives here, server side, and both read
models emit it; the browser positions, it does not decide.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence


def graph_layout(
    node_ids: Sequence[str], edges: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """A deterministic, definition-only layout hint for the static UI.

    Depth is derived from authored forward edges, never runtime events. Back
    edges are excluded so loops cannot make the layout cyclic.
    """

    ids = list(node_ids)
    depth = {node_id: 0 for node_id in ids}
    incoming: dict[str, list[str]] = {node_id: [] for node_id in ids}
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in ids}
    for edge in edges:
        if edge.get("back_edge"):
            continue
        if edge["from"] in outgoing and edge["to"] in incoming:
            outgoing[edge["from"]].append(edge["to"])
            incoming[edge["to"]].append(edge["from"])
    # Relax depth in topological order. Reading the nodes once in list order
    # instead would let a node be placed before its parent had a depth — a
    # fan-out whose branches sort after their own successors came out with the
    # successors sharing the branch's column.
    remaining = {node_id: len(incoming[node_id]) for node_id in ids}
    queue = [node_id for node_id in ids if not remaining[node_id]]
    settled = 0
    while queue:
        node_id = queue.pop(0)
        settled += 1
        for child in outgoing[node_id]:
            depth[child] = max(depth[child], depth[node_id] + 1)
            remaining[child] -= 1
            if not remaining[child]:
                queue.append(child)
    if settled != len(ids):
        # Forward edges should be acyclic once back edges are dropped. If a
        # definition ever slips through anyway, place the leftovers rather
        # than dropping them from the picture.
        for node_id in ids:
            if remaining[node_id]:
                depth[node_id] = max(
                    (depth[parent] + 1 for parent in incoming[node_id]), default=0
                )
    widths: dict[int, int] = {}
    positions = []
    for node_id in ids:
        layer = depth[node_id]
        lane = widths.get(layer, 0)
        widths[layer] = lane + 1
        positions.append({"node_id": node_id, "depth": layer, "lane": lane})
    branched = any(len(targets) > 1 for targets in outgoing.values())
    joined = any(len(sources) > 1 for sources in incoming.values())
    return {
        "mode": "branching" if branched or joined else "outline",
        "positions": positions,
    }
