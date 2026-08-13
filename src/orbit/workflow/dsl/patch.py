"""Changes to a workflow, stated as operations rather than as a new document.

Revising a workflow has meant handing a model the whole definition and taking
a whole one back. The model is then free to rewrite parts nobody asked about,
and the only defence is noticing afterwards — `change_summary` says what it
claims to have changed and `semanticWorkflowDiff` shows what it actually did,
but by then the edit exists and someone has to read a diff to catch it.

An operation cannot do that. "Add this node" says what it changes and is
structurally incapable of touching anything else.

Two decisions carry the design.

**Everything is named by id, never by position.** A JSON Patch pointer like
`/nodes/3/config` asks a model to count array elements, and any insertion
above shifts every path below it. `node_id` survives reordering and says what
it means.

**Applying is a pure function over the plain document.** A sequence passes
through states that are not yet whole — a node added before its edges names
nothing yet — so `AuthoredWorkflow` validates the result rather than each
step. What each operation *carries* is typed, so a model cannot smuggle in a
node shape the contract does not admit.

A patch does not replace compiling. `compile_source` still judges the document
that comes out; this only decides how it got there.
"""

from __future__ import annotations

import copy
from typing import Annotated, Any, Literal, Mapping, Sequence, Union

from pydantic import BaseModel, ConfigDict, Field

from .models import Edge, Endpoint, Node, Policy, Port, _Strict


class PatchError(ValueError):
    """One operation could not be applied, and which one.

    Carrying the index is the point: a model that gets "operation 3 names a
    node that does not exist" can fix operation 3, where one that gets "the
    patch failed" can only start again.
    """

    def __init__(self, index: int, message: str) -> None:
        super().__init__(f"operation {index}: {message}")
        self.index = index
        self.reason = message


class _Op(_Strict):
    """Operations are closed like the rest of the contract."""


class AddNode(_Op):
    op: Literal["add_node"]
    node: Node


class RemoveNode(_Op):
    """Remove a node, and everything that named it.

    Edges bound to it, its place in `entry` and `terminals`, and a `result`
    that read from it all go: each would otherwise be a reference the compiler
    rejects, and leaving them turns one requested change into a diagnostic.
    """

    op: Literal["remove_node"]
    node_id: str


class SetNodeLabel(_Op):
    op: Literal["set_node_label"]
    node_id: str
    label: str | None = None


class SetNodeConfig(_Op):
    op: Literal["set_node_config"]
    node_id: str
    config: dict[str, Any] | None = None


class SetNodePorts(_Op):
    op: Literal["set_node_ports"]
    node_id: str
    side: Literal["inputs", "outputs"]
    ports: list[Port]


class SetNodePolicies(_Op):
    op: Literal["set_node_policies"]
    node_id: str
    policies: list[str]


class AddEdge(_Op):
    op: Literal["add_edge"]
    edge: Edge


class RemoveEdge(_Op):
    op: Literal["remove_edge"]
    edge_id: str


class SetEdge(_Op):
    """Replace one edge wholesale, keeping its id.

    An edge's condition, mapping, route and priority are read together — a
    condition that moved without the priority that orders it is a different
    edge — so they are set together rather than one field at a time.
    """

    op: Literal["set_edge"]
    edge: Edge


class AddPolicy(_Op):
    op: Literal["add_policy"]
    policy: Policy


class RemovePolicy(_Op):
    """Remove a policy, and every reference to it, for the same reason."""

    op: Literal["remove_policy"]
    policy_id: str


class SetResult(_Op):
    op: Literal["set_result"]
    result: Endpoint


class SetEntry(_Op):
    op: Literal["set_entry"]
    entry: list[str]


class SetTerminals(_Op):
    op: Literal["set_terminals"]
    terminals: list[str]


class SetMetadata(_Op):
    op: Literal["set_metadata"]
    name: str | None = None
    description: str | None = None


class SetInputs(_Op):
    op: Literal["set_inputs"]
    inputs: list[Port]


GraphOperation = Annotated[
    Union[
        AddNode, RemoveNode, SetNodeLabel, SetNodeConfig, SetNodePorts,
        SetNodePolicies, AddEdge, RemoveEdge, SetEdge, AddPolicy, RemovePolicy,
        SetResult, SetEntry, SetTerminals, SetMetadata, SetInputs,
    ],
    Field(discriminator="op"),
]


class GraphPatch(BaseModel):
    """A change to one workflow, against the version it was computed from.

    `base_version` is not decoration: a patch worked out against v3 and applied
    to v5 produces something nobody asked for, and it is exactly the mistake
    that cannot be seen in the result.
    """

    model_config = ConfigDict(extra="forbid")

    base_version: int = Field(ge=0)
    operations: list[GraphOperation] = Field(min_length=1)
    summary: str | None = None


def _index(items: Sequence[Mapping[str, Any]], key: str, value: str) -> int:
    for position, item in enumerate(items):
        if item.get(key) == value:
            return position
    return -1


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def _apply(document: dict[str, Any], op: Any, index: int) -> None:
    nodes = document.setdefault("nodes", [])
    edges = document.setdefault("edges", [])

    if isinstance(op, AddNode):
        if _index(nodes, "id", op.node.id) >= 0:
            raise PatchError(index, f"node {op.node.id!r} already exists")
        nodes.append(_dump(op.node))
        return

    if isinstance(op, RemoveNode):
        position = _index(nodes, "id", op.node_id)
        if position < 0:
            raise PatchError(index, f"node {op.node_id!r} does not exist")
        nodes.pop(position)
        document["edges"] = [
            edge for edge in edges
            if edge.get("from", {}).get("node") != op.node_id
            and edge.get("to", {}).get("node") != op.node_id
        ]
        for key in ("entry", "terminals"):
            if isinstance(document.get(key), list):
                document[key] = [item for item in document[key] if item != op.node_id]
        if document.get("result", {}).get("node") == op.node_id:
            document.pop("result", None)
        return

    if isinstance(op, (SetNodeLabel, SetNodeConfig, SetNodePorts, SetNodePolicies)):
        position = _index(nodes, "id", op.node_id)
        if position < 0:
            raise PatchError(index, f"node {op.node_id!r} does not exist")
        node = dict(nodes[position])
        if isinstance(op, SetNodeLabel):
            _assign(node, "label", op.label)
        elif isinstance(op, SetNodeConfig):
            _assign(node, "config", op.config or None)
        elif isinstance(op, SetNodePorts):
            _assign(node, op.side, [_dump(port) for port in op.ports] or None)
        else:
            _assign(node, "policies", sorted(op.policies) or None)
        nodes[position] = node
        return

    if isinstance(op, AddEdge):
        if _index(edges, "id", op.edge.id) >= 0:
            raise PatchError(index, f"edge {op.edge.id!r} already exists")
        edges.append(_dump(op.edge))
        return

    if isinstance(op, RemoveEdge):
        position = _index(edges, "id", op.edge_id)
        if position < 0:
            raise PatchError(index, f"edge {op.edge_id!r} does not exist")
        edges.pop(position)
        return

    if isinstance(op, SetEdge):
        position = _index(edges, "id", op.edge.id)
        if position < 0:
            raise PatchError(index, f"edge {op.edge.id!r} does not exist")
        edges[position] = _dump(op.edge)
        return

    if isinstance(op, AddPolicy):
        policies = document.setdefault("policies", [])
        if _index(policies, "id", op.policy.id) >= 0:
            raise PatchError(index, f"policy {op.policy.id!r} already exists")
        policies.append(_dump(op.policy))
        return

    if isinstance(op, RemovePolicy):
        policies = document.get("policies", [])
        position = _index(policies, "id", op.policy_id)
        if position < 0:
            raise PatchError(index, f"policy {op.policy_id!r} does not exist")
        policies.pop(position)
        if not policies:
            document.pop("policies", None)
        for node in nodes:
            named = node.get("policies")
            if isinstance(named, list) and op.policy_id in named:
                remaining = [item for item in named if item != op.policy_id]
                _assign(node, "policies", remaining or None)
        for edge in edges:
            if edge.get("policy") == op.policy_id:
                edge.pop("policy", None)
        return

    if isinstance(op, SetResult):
        document["result"] = _dump(op.result)
        return

    if isinstance(op, (SetEntry, SetTerminals)):
        key = "entry" if isinstance(op, SetEntry) else "terminals"
        named = op.entry if isinstance(op, SetEntry) else op.terminals
        missing = [item for item in named if _index(nodes, "id", item) < 0]
        if missing:
            raise PatchError(index, f"{key} names unknown node(s): {', '.join(missing)}")
        document[key] = list(named)
        return

    if isinstance(op, SetMetadata):
        metadata = dict(document.get("metadata") or {})
        # The id is never patched: publishing an edit onto a different
        # aggregate is not an edit, and no instruction should be able to.
        if op.name is not None:
            _assign(metadata, "name", op.name.strip() or None)
        if op.description is not None:
            _assign(metadata, "description", op.description.strip() or None)
        document["metadata"] = metadata
        return

    if isinstance(op, SetInputs):
        _assign(document, "inputs", [_dump(port) for port in op.inputs] or None)
        return

    raise PatchError(index, f"unsupported operation {type(op).__name__}")


def _assign(target: dict[str, Any], key: str, value: Any) -> None:
    """Set a field, or remove it — absent and empty are not the same."""

    if value is None:
        target.pop(key, None)
    else:
        target[key] = value


def apply_patch(document: Mapping[str, Any], patch: GraphPatch) -> dict[str, Any]:
    """The document these operations describe, applied in order.

    The argument is never mutated: a caller keeps the document it had, which is
    what lets it show the change before accepting it.

    The result is not validated here. A sequence passes through states that are
    not yet whole, and it is `AuthoredWorkflow` — and then `compile_source` —
    that says whether what came out is a workflow.
    """

    result = copy.deepcopy(dict(document))
    for index, operation in enumerate(patch.operations):
        _apply(result, operation, index)
    return result
