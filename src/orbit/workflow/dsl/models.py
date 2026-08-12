"""Typed authoring contract for the LangGraph workflow subset of DSL Core 1.3.

One shape, three consumers:

* a model asked for structured output binds to `AuthoredWorkflow` directly, so
  a malformed reply is a validation error the caller can retry on rather than
  prose that has to be salvaged out of a fence;
* the browser editor consumes `authoring_json_schema()` to constrain what a
  canvas can draw before anything is submitted;
* the server parses a submission back into `AuthoredWorkflow` and emits
  `to_dsl_document()`, which is exactly what `compile_source(..., "json")`
  already accepts.

Two properties keep those three honest.

**This is deliberately narrower than `WORKFLOW_DSL_SCHEMA`.** It admits only
the node kinds the LangGraph runtime compiles — `action`, `decision`, `human`,
`join`, `terminal` — and has no `extension` field on nodes and no top-level
`extensions`. Those belong to the legacy Runtime contract. Expressing the
boundary as a type means an editor cannot draw an unsupported node and a model
cannot emit one, instead of both being told "no" after the fact.

**This is not a validator.** It checks that a document is internally consistent
— ids unique, references resolving, ports existing. It cannot check anything
that needs the world: whether a Handler name is registered, whether a port's
`schema_id` is in the catalog, whether two connected ports have compatible
schemas. `compile_document` remains the only thing that decides a workflow is
valid, and passing here means only that it is worth its turn.

The one field this file will never grow is a Handler fingerprint. A node names
a Handler by `name` and `version`; the fingerprint that binds an IR node to an
exact reviewed `BoundHandler` is resolved server-side by `analyze_dsl`. A
document that could carry its own fingerprint would let whoever wrote it choose
what it binds to, which is the whole boundary.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .schema import ID_PATTERN


# The node kinds `langgraph_runtime.compiler` compiles. `agentic`, `foreach`,
# `subflow` and `extension` are absent on purpose — see the module docstring.
LANGGRAPH_NODE_KINDS = ("action", "decision", "human", "join", "terminal")

NodeKind = Literal["action", "decision", "human", "join", "terminal"]
Transport = Literal["inline", "artifact_ref", "secret_ref"]
Route = Literal["success", "error", "timeout", "cancel"]
RouteMode = Literal["exclusive", "parallel"]
Visibility = Literal["node", "run", "subflow", "workflow"]

Identifier = Field(pattern=ID_PATTERN)


class _Strict(BaseModel):
    """Reject unknown fields, and accept a field by name or by alias.

    `extra="forbid"` mirrors `additionalProperties: false` in the JSON Schema:
    a misspelled field is a loud error at the point it was written, not a
    silently dropped instruction discovered when the run does the wrong thing.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class WorkflowMetadata(_Strict):
    id: str = Identifier
    name: str = Field(min_length=1)
    slug: str | None = Field(default=None, pattern=ID_PATTERN)
    description: str = ""
    labels: dict[str, str] = Field(default_factory=dict)


class Port(_Strict):
    """A typed input or output socket.

    `schema_id` is carried, never invented: it has to be one of the ids the
    schema catalog already knows, and only the server can say which those are.
    """

    id: str = Identifier
    schema_id: str = Field(min_length=1)
    required: bool = True
    default: Any = None
    description: str = ""
    transport: Transport = "inline"
    max_size_bytes: int | None = Field(default=None, ge=0)
    content_types: list[str] = Field(default_factory=list)
    visibility: Visibility | None = None


class HandlerRef(_Strict):
    """The Handler a node calls, named but not bound.

    No fingerprint field, by design. See the module docstring.
    """

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


class Node(_Strict):
    id: str = Identifier
    kind: NodeKind
    inputs: list[Port] = Field(default_factory=list)
    outputs: list[Port] = Field(default_factory=list)
    handler: HandlerRef | None = None
    # What a reader calls this step. Beside `config`, not inside it: `config`
    # belongs to the Handler, and a Handler with a closed config schema rejects
    # a field meant for people.
    label: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"\S")
    config: dict[str, Any] = Field(default_factory=dict)
    policies: list[str] = Field(default_factory=list)
    route_mode: RouteMode | None = None


class Endpoint(_Strict):
    node: str = Identifier
    port: str = Identifier


class Edge(_Strict):
    """One output port carried to one input port.

    `condition` and `mapping` are compiled expression ASTs and stay untyped
    here. Giving them a shape would mean maintaining a second copy of the
    grammar that `compile_condition` and `compile_mapping` already own, and a
    second copy is a drift waiting to happen. They are validated where they are
    compiled.
    """

    id: str = Identifier
    source: Endpoint = Field(alias="from")
    target: Endpoint = Field(alias="to")
    condition: str | bool | dict[str, Any] | None = None
    mapping: dict[str, Any] | None = None
    route: Route = "success"
    priority: int = Field(default=0, ge=0)
    back_edge: bool = False
    policy: str | None = Field(default=None, pattern=ID_PATTERN)


class Policy(_Strict):
    id: str = Identifier
    kind: str = Field(min_length=1)
    config: dict[str, Any] = Field(default_factory=dict)


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    repeated: set[str] = set()
    for value in values:
        (repeated if value in seen else seen).add(value)
    return sorted(repeated)


class AuthoredWorkflow(_Strict):
    """A workflow as authored, before the world has been consulted."""

    dsl_version: Literal["1.3"] = "1.3"
    metadata: WorkflowMetadata
    inputs: list[Port] = Field(default_factory=list)
    outputs: list[Port] = Field(default_factory=list)
    nodes: list[Node] = Field(min_length=1)
    edges: list[Edge] = Field(default_factory=list)
    entry: list[str] = Field(min_length=1)
    terminals: list[str] = Field(min_length=1)
    # Required, not optional: `compile_document` refuses a 1.3 document without
    # one ("DSL 1.3 requires a primary result declaration"), and this model is
    # pinned to 1.3. Carrying it as optional would mean the single most common
    # omission costs a compile round trip to discover.
    result: Endpoint
    policies: list[Policy] = Field(default_factory=list)

    @model_validator(mode="after")
    def _internally_consistent(self) -> AuthoredWorkflow:
        """Everything this document can be judged on without a catalog.

        Deliberately not a substitute for `analyze_dsl`: this cannot know a
        Handler exists or that two schemas are compatible. What it can know is
        that the document does not contradict itself, and saying so here turns
        the most common authoring mistakes into a retryable validation error
        instead of a compile diagnostic one round trip later.
        """

        problems: list[str] = []

        for label, ids in (
            ("node", [node.id for node in self.nodes]),
            ("edge", [edge.id for edge in self.edges]),
            ("policy", [policy.id for policy in self.policies]),
        ):
            for duplicate in _duplicates(ids):
                problems.append(f"duplicate {label} id {duplicate!r}")

        nodes = {node.id: node for node in self.nodes}
        policies = {policy.id for policy in self.policies}

        for name, referenced in (("entry", self.entry), ("terminals", self.terminals)):
            for node_id in referenced:
                if node_id not in nodes:
                    problems.append(f"{name} names unknown node {node_id!r}")

        for node in self.nodes:
            for policy_id in node.policies:
                if policy_id not in policies:
                    problems.append(
                        f"node {node.id!r} names unknown policy {policy_id!r}"
                    )

        for edge in self.edges:
            problems.extend(self._edge_problems(edge, nodes, policies))

        problems.extend(
            self._endpoint_problems("result", self.result, nodes, "outputs")
        )

        if problems:
            raise ValueError("; ".join(problems))
        return self

    @staticmethod
    def _endpoint_problems(
        where: str, endpoint: Endpoint, nodes: Mapping[str, Node], side: str
    ) -> list[str]:
        node = nodes.get(endpoint.node)
        if node is None:
            return [f"{where} names unknown node {endpoint.node!r}"]
        if endpoint.port not in {port.id for port in getattr(node, side)}:
            return [
                f"{where} names port {endpoint.port!r}, which node "
                f"{endpoint.node!r} does not declare as an {side[:-1]}"
            ]
        return []

    @classmethod
    def _edge_problems(
        cls, edge: Edge, nodes: Mapping[str, Node], policies: set[str]
    ) -> list[str]:
        problems = cls._endpoint_problems(
            f"edge {edge.id!r} source", edge.source, nodes, "outputs"
        )
        problems += cls._endpoint_problems(
            f"edge {edge.id!r} target", edge.target, nodes, "inputs"
        )
        if edge.policy is not None and edge.policy not in policies:
            problems.append(f"edge {edge.id!r} names unknown policy {edge.policy!r}")
        if edge.back_edge and edge.policy is None:
            # The LangGraph compiler refuses to route a back edge without one
            # ("back edge {id!r} requires a loop policy"), because an unbounded
            # loop has no way to terminate. Saying so at authoring time costs
            # nothing and saves a compile round trip.
            problems.append(
                f"back edge {edge.id!r} requires a loop or rework policy"
            )
        return problems

    def to_dsl_document(self) -> dict[str, Any]:
        """The DSL JSON document this describes.

        `by_alias` restores `from`/`to` on edges, which cannot be Python
        attribute names. `exclude_none` drops absent optionals rather than
        emitting nulls the DSL schema would reject — which does mean a port
        whose default is an explicit JSON `null` is indistinguishable from one
        with no default. No Handler has wanted that, and the alternative is a
        sentinel in every port.

        Fields left implicit by the author come back explicit — `route:
        "success"`, `config: {}`, and so on. The DSL schema declares the same
        defaults, so this adds and loses nothing, and it compiles to the same
        IR; but it means the output is a normal form rather than a copy of the
        input. Feeding it back through `model_validate` is a fixed point.
        """

        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


def authoring_json_schema() -> dict[str, Any]:
    """The JSON Schema for `AuthoredWorkflow`, for editors and model clients.

    Aliased, so the emitted `edges[].from`/`to` match the DSL document rather
    than this file's Python attribute names.
    """

    return AuthoredWorkflow.model_json_schema(by_alias=True)
