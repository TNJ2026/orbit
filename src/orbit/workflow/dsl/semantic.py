"""Deterministic semantic and graph validation for Workflow DSL Core 1.2."""

from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Any, Mapping

from jsonschema import Draft202012Validator

from ..domain.data import ArtifactVisibility, PortDataPolicy, PortTransport
from ..catalogs.extensions import InMemoryExtensionRegistry
from ..catalogs.handlers import HandlerCatalog, HandlerManifest
from ..catalogs.schemas import InMemorySchemaCatalog
from ..domain.serialization import to_primitive
from .diagnostics import Diagnostic, DiagnosticError, JsonPath, sorted_diagnostics
from .parser import ParsedDslDocument


@dataclass(frozen=True)
class SemanticAnalysis:
    handlers: Mapping[str, HandlerManifest]
    outgoing: Mapping[str, tuple[str, ...]]
    incoming: Mapping[str, tuple[str, ...]]


def _diagnostic(document: ParsedDslDocument, code: str, message: str, path: JsonPath, *, hint: str | None = None) -> Diagnostic:
    return Diagnostic(
        code=code,
        message=message,
        phase="semantic",
        path=path,
        source_range=document.source_map.get(path),
        hint=hint,
    )


def _duplicates(values: list[str]) -> set[str]:
    seen: set[str] = set()
    repeated: set[str] = set()
    for value in values:
        if value in seen:
            repeated.add(value)
        seen.add(value)
    return repeated


def _port_policy(value: Mapping[str, Any]) -> PortDataPolicy:
    return PortDataPolicy(
        PortTransport(value.get("transport", "inline")),
        value.get("max_size_bytes"), tuple(value.get("content_types", ())),
        None if "visibility" not in value else ArtifactVisibility(value["visibility"]),
    )


def _schema_accepts_type(schema: Mapping[str, Any], expected: str) -> bool:
    declared = schema.get("type")
    if declared is None:
        return True
    if isinstance(declared, str):
        return declared == expected
    return isinstance(declared, list) and expected in declared


def _is_default_condition(value: Any) -> bool:
    """Recognize every DSL spelling that compiles to literal true fallback."""
    return (
        value is None
        or value is True
        or (isinstance(value, str) and value.strip() == "True")
        or value == {"op": "literal", "value": True}
    )


def _source_references(value: Any) -> tuple[str, ...]:
    """Collect source paths from either structured or textual conditions."""

    found: list[str] = []
    if isinstance(value, Mapping):
        if value.get("op") == "ref" and isinstance(value.get("path"), str):
            path = value["path"]
            if path.startswith("source."):
                found.append(path)
        for item in value.values():
            found.extend(_source_references(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_source_references(item))
    elif isinstance(value, str):
        found.extend(re.findall(r"\bsource(?:\.[A-Za-z_][A-Za-z0-9_]*)+", value))
    return tuple(found)


def _find_cycle(nodes: set[str], outgoing: Mapping[str, list[str]]) -> tuple[str, ...] | None:
    state: dict[str, int] = {}
    for root in sorted(nodes):
        if state.get(root, 0) != 0:
            continue
        active: list[str] = [root]
        positions = {root: 0}
        state[root] = 1
        stack: list[tuple[str, object]] = [
            (root, iter(sorted(outgoing.get(root, []))))
        ]
        while stack:
            node, targets = stack[-1]
            try:
                target = next(targets)  # type: ignore[arg-type]
            except StopIteration:
                stack.pop()
                active.pop()
                positions.pop(node)
                state[node] = 2
                continue
            target_state = state.get(target, 0)
            if target_state == 1:
                return tuple(active[positions[target] :] + [target])
            if target_state == 2:
                continue
            state[target] = 1
            positions[target] = len(active)
            active.append(target)
            stack.append((target, iter(sorted(outgoing.get(target, [])))))
    return None


def _reachable_from(start: str, edges, exclude: str | None = None) -> set[str]:
    """Nodes a run can arrive at from `start`, ignoring back edges.

    `exclude` drops one edge id, which is how the branches of a fan-out are
    told apart: each branch is what remains reachable through its own edge.
    """

    seen: set[str] = {start}
    queue = [start]
    while queue:
        current = queue.pop()
        for edge in edges:
            if edge.get("back_edge"):
                continue
            if (edge.get("from") or {}).get("node") != current:
                continue
            if edge.get("id") == exclude:
                continue
            target = (edge.get("to") or {}).get("node")
            if target is not None and target not in seen:
                seen.add(target)
                queue.append(target)
    return seen


def _unsatisfiable_join(join_id: str, edges, node_by_id, node_index):
    """Ports of an `all` join that no single run can fill together.

    Reported as (message, hint, path). Empty when the join is satisfiable, and
    deliberately conservative: it accuses only when two ports belong wholly to
    different branches of one exclusive node, which is a contradiction rather
    than a smell.

    Branches are followed by *edge*, not by source node. A fan-out that feeds
    the join directly on one branch is the common shape — "ask a person, or
    publish straight away" — and it belongs to no branch of itself, so asking
    which branch its node is in finds nothing.
    """

    incoming = [
        edge for edge in edges
        if (edge.get("to") or {}).get("node") == join_id and not edge.get("back_edge")
    ]
    ports = {(edge.get("to") or {}).get("port") for edge in incoming}
    if len(ports - {None}) < 2:
        return []

    findings = []
    for fan_out_id, fan_out in sorted(node_by_id.items()):
        if (fan_out.get("route_mode") or "exclusive") != "exclusive":
            continue
        outgoing = [
            edge for edge in edges
            if (edge.get("from") or {}).get("node") == fan_out_id
            and not edge.get("back_edge")
        ]
        if len(outgoing) < 2:
            continue
        reach = {
            edge["id"]: _reachable_from((edge.get("to") or {}).get("node", ""), edges)
            for edge in outgoing
        }

        def branch_of(edge):
            """Which of the fan-out's edges this one can only be reached through."""

            if (edge.get("from") or {}).get("node") == fan_out_id:
                return edge.get("id")
            owning = {
                edge_id for edge_id, nodes in reach.items()
                if (edge.get("from") or {}).get("node") in nodes
            }
            return next(iter(owning)) if len(owning) == 1 else None

        placed: dict[str, set[str | None]] = {}
        for edge in incoming:
            port = (edge.get("to") or {}).get("port")
            if port is not None:
                placed.setdefault(port, set()).add(branch_of(edge))
        # A port reachable through more than one branch, or through none of
        # them, is not evidence: only a port that lives in exactly one.
        owned = {
            port: next(iter(branches))
            for port, branches in placed.items()
            if len(branches) == 1 and next(iter(branches)) is not None
        }
        if len(set(owned.values())) > 1:
            findings.append((
                f"join {join_id!r} cannot be satisfied: {fan_out_id!r} routes "
                f"exclusively, so its branches cannot both fill "
                f"{', '.join(sorted(owned))}",
                "declare route_mode 'parallel' on the node that fans out, "
                "or give the join mode 'any'",
                ("nodes", node_index[fan_out_id], "route_mode"),
            ))
    return findings


def analyze_dsl(
    document: ParsedDslDocument,
    handlers: HandlerCatalog,
    schemas: InMemorySchemaCatalog,
    extensions: InMemoryExtensionRegistry | None = None,
) -> SemanticAnalysis:
    extensions = extensions or InMemoryExtensionRegistry()
    data = to_primitive(document.data)
    diagnostics: list[Diagnostic] = []
    nodes = data["nodes"]
    edges = data["edges"]
    policies = data.get("policies", [])

    extension_values = [
        (value, ("extensions", index))
        for index, value in enumerate(data.get("extensions", []))
    ]
    extension_values.extend(
        (node["extension"], ("nodes", index, "extension"))
        for index, node in enumerate(nodes)
        if "extension" in node
    )
    for value, path in extension_values:
        manifest = extensions.get(value["extension_id"], value["extension_version"])
        if manifest is None:
            diagnostics.append(
                _diagnostic(
                    document,
                    "DSL_UNSUPPORTED_VERSION",
                    f"extension {value['extension_id']}@{value['extension_version']} is not registered",
                    path,
                )
            )
            continue
        for error in Draft202012Validator(to_primitive(manifest.config_schema)).iter_errors(value["config"]):
            diagnostics.append(
                _diagnostic(
                    document,
                    "DSL_SCHEMA_ERROR",
                    f"extension config: {error.message}",
                    path + tuple(error.path),
                )
            )

    for direction in ("inputs", "outputs"):
        ports = data.get(direction, [])
        for repeated in sorted(_duplicates([item["id"] for item in ports])):
            diagnostics.append(
                _diagnostic(
                    document,
                    "DSL_DUPLICATE_ID",
                    f"duplicate workflow {direction[:-1]} id {repeated!r}",
                    (direction,),
                )
            )
        for index, port in enumerate(ports):
            if schemas.get(port["schema_id"]) is None:
                diagnostics.append(
                    _diagnostic(
                        document,
                        "DSL_REFERENCE_NOT_FOUND",
                        f"schema {port['schema_id']!r} is not registered",
                        (direction, index, "schema_id"),
                    )
                )
            try:
                policy = _port_policy(port)
            except (TypeError, ValueError) as error:
                diagnostics.append(
                    _diagnostic(
                        document, "DSL_PORT_INCOMPATIBLE", str(error),
                        (direction, index),
                    )
                )
            else:
                if "default" in port and policy.transport is not PortTransport.INLINE:
                    diagnostics.append(
                        _diagnostic(
                            document, "DSL_PORT_INCOMPATIBLE",
                            "only inline ports may declare defaults",
                            (direction, index, "default"),
                        )
                    )

    for collection, label in [(nodes, "node"), (edges, "edge"), (policies, "policy")]:
        ids = [item["id"] for item in collection]
        for repeated in sorted(_duplicates(ids)):
            index = next(i for i, item in enumerate(collection) if item["id"] == repeated)
            diagnostics.append(
                _diagnostic(
                    document,
                    "DSL_DUPLICATE_ID",
                    f"duplicate {label} id {repeated!r}",
                    (("nodes" if label == "node" else "edges" if label == "edge" else "policies"), index, "id"),
                )
            )

    node_by_id = {item["id"]: item for item in nodes}
    node_index = {item["id"]: index for index, item in enumerate(nodes)}
    policy_ids = {item["id"] for item in policies}
    policy_kinds = {item["id"]: item.get("kind") for item in policies}
    resolved: dict[str, HandlerManifest] = {}

    for index, node in enumerate(nodes):
        node_path: JsonPath = ("nodes", index)
        for direction in ("inputs", "outputs"):
            ports = node.get(direction, [])
            for repeated in sorted(_duplicates([item["id"] for item in ports])):
                diagnostics.append(
                    _diagnostic(
                        document,
                        "DSL_DUPLICATE_ID",
                        f"duplicate {direction[:-1]} port id {repeated!r}",
                        node_path + (direction,),
                    )
                )
            for port_index, port in enumerate(ports):
                if schemas.get(port["schema_id"]) is None:
                    diagnostics.append(
                        _diagnostic(
                            document,
                            "DSL_REFERENCE_NOT_FOUND",
                            f"schema {port['schema_id']!r} is not registered",
                            node_path + (direction, port_index, "schema_id"),
                        )
                    )
                try:
                    policy = _port_policy(port)
                except (TypeError, ValueError) as error:
                    diagnostics.append(
                        _diagnostic(
                            document, "DSL_PORT_INCOMPATIBLE", str(error),
                            node_path + (direction, port_index),
                        )
                    )
                else:
                    if "default" in port and policy.transport is not PortTransport.INLINE:
                        diagnostics.append(
                            _diagnostic(
                                document, "DSL_PORT_INCOMPATIBLE",
                                "only inline ports may declare defaults",
                                node_path + (direction, port_index, "default"),
                            )
                        )

        kind = node["kind"]
        handler_ref = node.get("handler")
        if kind == "action" and handler_ref is None:
            diagnostics.append(
                _diagnostic(document, "DSL_HANDLER_NOT_FOUND", "action node requires a handler", node_path + ("handler",))
            )
        elif kind in {"human", "agentic", "foreach", "subflow", "decision", "join", "terminal"} and handler_ref is not None:
            diagnostics.append(
                _diagnostic(document, "DSL_HANDLER_NOT_FOUND", f"{kind} node cannot declare a handler", node_path + ("handler",))
            )
        elif handler_ref is not None:
            manifest = handlers.resolve(handler_ref["name"], handler_ref["version"])
            if manifest is None:
                diagnostics.append(
                    _diagnostic(
                        document,
                        "DSL_HANDLER_NOT_FOUND",
                        f"handler {handler_ref['name']!r} does not match {handler_ref['version']!r}",
                        node_path + ("handler",),
                    )
                )
            else:
                resolved[node["id"]] = manifest
                if kind not in manifest.node_kinds:
                    diagnostics.append(
                        _diagnostic(
                            document,
                            "DSL_HANDLER_NOT_FOUND",
                            f"handler {manifest.name}@{manifest.version} does not support {kind!r} nodes",
                            node_path + ("kind",),
                        )
                    )
                declared_inputs = {item["id"]: item["schema_id"] for item in node.get("inputs", [])}
                declared_outputs = {item["id"]: item["schema_id"] for item in node.get("outputs", [])}
                if declared_inputs != dict(manifest.inputs) or declared_outputs != dict(manifest.outputs):
                    diagnostics.append(
                        _diagnostic(
                            document,
                            "DSL_PORT_INCOMPATIBLE",
                            f"node ports do not match handler {manifest.name}@{manifest.version}",
                            node_path,
                        )
                    )
                config_errors = sorted(
                    Draft202012Validator(to_primitive(manifest.config_schema)).iter_errors(node.get("config", {})),
                    key=lambda error: tuple(str(item) for item in error.path),
                )
                for error in config_errors:
                    diagnostics.append(
                        _diagnostic(
                            document,
                            "DSL_SCHEMA_ERROR",
                            f"handler config: {error.message}",
                            node_path + ("config",) + tuple(error.path),
                        )
                    )
        if kind == "extension" and "extension" not in node:
            diagnostics.append(
                _diagnostic(document, "DSL_UNSUPPORTED_VERSION", "extension node requires an extension envelope", node_path + ("extension",))
            )
        if kind == "human":
            config = node.get("config", {})
            outputs = node.get("outputs", [])
            if len(outputs) != 1:
                diagnostics.append(_diagnostic(
                    document, "DSL_PORT_INCOMPATIBLE",
                    "human node requires exactly one result output",
                    node_path + ("outputs",),
                ))
            else:
                output_schema = schemas.get(outputs[0].get("schema_id", ""))
                if output_schema is not None:
                    sample = {"decision": "approve", "value": None}
                    if any(Draft202012Validator(to_primitive(output_schema)).iter_errors(sample)):
                        diagnostics.append(_diagnostic(
                            document, "DSL_PORT_INCOMPATIBLE",
                            "human output port schema must accept the submission result {decision, value}",
                            node_path + ("outputs", 0, "schema_id"),
                        ))
            if config.get("task_kind") != "approval":
                diagnostics.append(_diagnostic(
                    document, "DSL_SCHEMA_ERROR",
                    "static human config.task_kind must be approval",
                    node_path + ("config", "task_kind"),
                ))
            participants = config.get("participants", [])
            if (
                not isinstance(participants, list) or not participants
                or any(not isinstance(actor, str) or not actor.strip() for actor in participants)
                or len(set(participants)) != len(participants)
            ):
                diagnostics.append(_diagnostic(
                    document, "DSL_SCHEMA_ERROR",
                    "human config.participants must contain unique actor names",
                    node_path + ("config", "participants"),
                ))
            if config.get("quorum", "any") != "any":
                diagnostics.append(_diagnostic(
                    document, "DSL_SCHEMA_ERROR",
                    "static human nodes currently require quorum 'any'",
                    node_path + ("config", "quorum"),
                ))
        for policy_index, policy_id in enumerate(node.get("policies", [])):
            if policy_id not in policy_ids:
                diagnostics.append(
                    _diagnostic(
                        document,
                        "DSL_REFERENCE_NOT_FOUND",
                        f"policy {policy_id!r} is not defined",
                        node_path + ("policies", policy_index),
                    )
                )
                continue
            # Retry re-runs the work a node did. A node with no Handler does
            # none — a terminal, join or decision carries a value through, and
            # a human node is answered rather than re-executed. This was
            # accepted here and refused by the engine as a node whose
            # "retry policy requires a retry-safe Handler", which is a Handler
            # that kind may not declare: an author was told to fix something
            # the contract forbids them to have.
            if (
                policy_kinds.get(policy_id) == "retry"
                and node["kind"] in {"terminal", "join", "decision", "human"}
            ):
                diagnostics.append(_diagnostic(
                    document,
                    "DSL_POLICY_INVALID",
                    f"a {node['kind']} node cannot carry a retry policy",
                    node_path + ("policies", policy_index),
                    hint=(
                        "retry re-runs a Handler's work, and this node has "
                        "none; a human node is chased with reminder and "
                        "escalation rather than retried"
                    ),
                ))

    outgoing: dict[str, list[str]] = {node_id: [] for node_id in node_by_id}
    incoming: dict[str, list[str]] = {node_id: [] for node_id in node_by_id}
    dag_outgoing: dict[str, list[str]] = {node_id: [] for node_id in node_by_id}
    dag_incoming: dict[str, list[str]] = {node_id: [] for node_id in node_by_id}
    input_writers: dict[tuple[str, str], str] = {}
    for index, edge in enumerate(edges):
        edge_path: JsonPath = ("edges", index)
        source_id = edge["from"]["node"]
        target_id = edge["to"]["node"]
        source = node_by_id.get(source_id)
        target = node_by_id.get(target_id)
        if source is None:
            diagnostics.append(_diagnostic(document, "DSL_REFERENCE_NOT_FOUND", f"source node {source_id!r} is not defined", edge_path + ("from", "node")))
        if target is None:
            diagnostics.append(_diagnostic(document, "DSL_REFERENCE_NOT_FOUND", f"target node {target_id!r} is not defined", edge_path + ("to", "node")))
        if source is None or target is None:
            continue
        if source.get("kind") == "human" and not _is_default_condition(
            edge.get("condition")
        ):
            expected = f"source.{edge['from']['port']}.decision"
            invalid = sorted({
                path for path in _source_references(edge.get("condition"))
                if path != expected
            })
            if invalid:
                diagnostics.append(_diagnostic(
                    document,
                    "DSL_GRAPH_CONDITION_INVALID",
                    "human approval conditions may only read the standard "
                    f"decision field {expected!r}; found {invalid}",
                    edge_path + ("condition",),
                ))
        outgoing[source_id].append(target_id)
        incoming[target_id].append(source_id)
        if not edge.get("back_edge", False):
            dag_outgoing[source_id].append(target_id)
            dag_incoming[target_id].append(source_id)
        elif edge.get("policy") not in policy_ids:
            diagnostics.append(
                _diagnostic(
                    document, "DSL_REFERENCE_NOT_FOUND",
                    "back edge requires a defined loop or rework policy",
                    edge_path + ("policy",),
                )
            )
        source_ports = {item["id"]: item for item in source.get("outputs", [])}
        target_ports = {item["id"]: item for item in target.get("inputs", [])}
        source_port = source_ports.get(edge["from"]["port"])
        target_port = target_ports.get(edge["to"]["port"])
        if source_port is None:
            diagnostics.append(_diagnostic(document, "DSL_REFERENCE_NOT_FOUND", f"output port {edge['from']['port']!r} is not defined", edge_path + ("from", "port")))
        if target_port is None:
            diagnostics.append(_diagnostic(document, "DSL_REFERENCE_NOT_FOUND", f"input port {edge['to']['port']!r} is not defined", edge_path + ("to", "port")))
        if source_port is not None and target_port is not None:
            try:
                source_policy = _port_policy(source_port)
                target_policy = _port_policy(target_port)
            except (TypeError, ValueError):
                continue
            if source_policy.transport is not target_policy.transport:
                diagnostics.append(
                    _diagnostic(
                        document, "DSL_PORT_INCOMPATIBLE",
                        "source and target port transports must match", edge_path,
                    )
                )
            if source_policy.transport is PortTransport.ARTIFACT_REF:
                if source_policy.visibility is ArtifactVisibility.NODE:
                    diagnostics.append(
                        _diagnostic(
                            document, "DSL_PORT_INCOMPATIBLE",
                            "node-visible Artifact cannot cross a node edge", edge_path,
                        )
                    )
                if source_policy.visibility is not target_policy.visibility:
                    diagnostics.append(
                        _diagnostic(
                            document, "DSL_PORT_INCOMPATIBLE",
                            "Artifact visibility must match across an edge", edge_path,
                        )
                    )
                if not set(source_policy.content_types).issubset(target_policy.content_types):
                    diagnostics.append(
                        _diagnostic(
                            document, "DSL_PORT_INCOMPATIBLE",
                            "target port does not accept every source content type", edge_path,
                        )
                    )
                if source_policy.max_size_bytes > target_policy.max_size_bytes:
                    diagnostics.append(
                        _diagnostic(
                            document, "DSL_PORT_INCOMPATIBLE",
                            "target Artifact size limit is smaller than the source limit", edge_path,
                        )
                    )
            if source_policy.transport in {PortTransport.ARTIFACT_REF, PortTransport.SECRET_REF}:
                if edge.get("mapping") not in (None, {}):
                    diagnostics.append(
                        _diagnostic(
                            document, "DSL_MAPPING_INVALID",
                            "Artifact and Secret ports only support exact identity binding",
                            edge_path + ("mapping",),
                        )
                    )
            mapped_schema = edge.get("mapping", {}).get("schema_id", source_port["schema_id"])
            if not schemas.compatible(mapped_schema, target_port["schema_id"]):
                diagnostics.append(
                    _diagnostic(
                        document,
                        "DSL_PORT_INCOMPATIBLE",
                        f"{mapped_schema!r} cannot be connected to {target_port['schema_id']!r}",
                        edge_path,
                    )
                )
            writer_key = (target_id, target_port["id"])
            if writer_key in input_writers and target.get("kind") != "join" and not edge.get("back_edge", False):
                diagnostics.append(
                    _diagnostic(
                        document,
                        "DSL_PORT_INCOMPATIBLE",
                        f"input {target_id}.{target_port['id']} already has a writer",
                        edge_path + ("to",),
                        hint="merge forward branches through an explicit join node before this input",
                    )
                )
            input_writers[writer_key] = edge["id"]

        policy_ref = edge.get("policy")
        if policy_ref is not None and policy_ref not in policy_ids:
            diagnostics.append(
                _diagnostic(document, "DSL_REFERENCE_NOT_FOUND", f"policy {policy_ref!r} is not defined", edge_path + ("policy",))
            )

    policy_by_id = {item["id"]: item for item in policies}
    supported_policy_kinds = {"route", "join", "retry", "rework", "loop", "completion"}
    for index, policy in enumerate(policies):
        kind = policy["kind"]
        config = policy["config"]
        path = ("policies", index)
        if kind not in supported_policy_kinds:
            diagnostics.append(_diagnostic(document, "DSL_POLICY_INVALID", f"unsupported policy kind {kind!r}", path + ("kind",)))
            continue
        positive_fields = {
            "retry": ("max_attempts",), "rework": ("max_generations",),
            "loop": ("max_iterations",), "join": (),
        }.get(kind, ())
        for field in positive_fields:
            value = config.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                diagnostics.append(_diagnostic(document, "DSL_POLICY_INVALID", f"{kind} policy requires positive {field}", path + ("config", field)))
        if kind == "join":
            mode = config.get("mode")
            if mode not in {"all", "any", "n_of_m", "all_successful", "deadline"}:
                diagnostics.append(_diagnostic(document, "DSL_POLICY_INVALID", "join policy has invalid mode", path + ("config", "mode")))
            if mode == "n_of_m" and (isinstance(config.get("threshold"), bool) or not isinstance(config.get("threshold"), int) or config["threshold"] < 1):
                diagnostics.append(_diagnostic(document, "DSL_POLICY_INVALID", "n_of_m join requires positive threshold", path + ("config", "threshold")))
            if mode == "deadline" and (isinstance(config.get("deadline_seconds"), bool) or not isinstance(config.get("deadline_seconds"), int) or config["deadline_seconds"] < 1):
                diagnostics.append(_diagnostic(document, "DSL_POLICY_INVALID", "deadline join requires positive deadline_seconds", path + ("config", "deadline_seconds")))

    for node_id, node in node_by_id.items():
        degree = len(dag_incoming[node_id])
        path = ("nodes", node_index[node_id])
        if node["kind"] == "join":
            join_policies = [policy_by_id[item] for item in node.get("policies", ()) if item in policy_by_id and policy_by_id[item]["kind"] == "join"]
            if degree < 2:
                diagnostics.append(_diagnostic(document, "DSL_JOIN_INVALID", "join node requires at least two incoming edges", path))
            if len(join_policies) != 1:
                diagnostics.append(_diagnostic(document, "DSL_JOIN_INVALID", "join node requires exactly one join policy", path + ("policies",)))
            else:
                # An `all` join needs every input port filled, and an
                # exclusive node fires exactly one of its outgoing edges. If
                # two of this join's ports are fed from different branches of
                # one exclusive fan-out, no single run can fill both — the run
                # dies at the join, after the Agents upstream have done their
                # work. The author meant the fan-out to be parallel; saying so
                # here costs nothing and saying it at run time costs the work.
                if join_policies[0]["config"].get("mode", "all") in {
                    "all", "all_successful",
                }:
                    for message, hint, where in _unsatisfiable_join(
                        node_id, edges, node_by_id, node_index,
                    ):
                        diagnostics.append(_diagnostic(
                            document, "DSL_JOIN_INVALID", message, where, hint=hint,
                        ))
                merge_mode = join_policies[0]["config"].get(
                    "merge_mode", "array_by_edge"
                )
                expected_type = {
                    "array_by_edge": "array",
                    "object_by_edge": "object",
                }.get(merge_mode)
                if expected_type is not None:
                    for output_index, output in enumerate(node.get("outputs", ())):
                        schema = schemas.get(output["schema_id"])
                        if schema is not None and not _schema_accepts_type(
                            schema, expected_type,
                        ):
                            diagnostics.append(_diagnostic(
                                document,
                                "DSL_JOIN_INVALID",
                                f"join merge_mode {merge_mode!r} produces an "
                                f"{expected_type}, but output {output['id']!r} "
                                f"uses schema {output['schema_id']!r}",
                                path + ("outputs", output_index, "schema_id"),
                                hint=(
                                    "use object_by_edge for an object output, or "
                                    "declare an array schema for array_by_edge"
                                ),
                            ))
        elif degree > 1:
            diagnostics.append(_diagnostic(
                document, "DSL_GRAPH_AMBIGUOUS_MERGE",
                "multiple incoming edges require an explicit join node", path,
                hint="use join mode 'any' for alternatives or 'all' for parallel branches",
            ))

    for index, edge in enumerate(edges):
        if not edge.get("back_edge", False):
            continue
        policy = policy_by_id.get(edge.get("policy"))
        if policy is not None and policy["kind"] not in {"loop", "rework"}:
            diagnostics.append(_diagnostic(document, "DSL_POLICY_INVALID", "back edge policy must be loop or rework", ("edges", index, "policy")))

    for node_id, node in node_by_id.items():
        if node.get("route_mode", "exclusive") != "exclusive":
            continue
        for route in ("success", "error", "timeout", "cancel"):
            route_edges = [
                (index, edge) for index, edge in enumerate(edges)
                if edge["from"]["node"] == node_id
                and edge.get("route", "success") == route
            ]
            defaults = [
                (index, edge) for index, edge in route_edges
                if _is_default_condition(edge.get("condition"))
            ]
            if len(defaults) > 1:
                for index, _ in defaults[1:]:
                    diagnostics.append(_diagnostic(
                        document, "DSL_POLICY_INVALID",
                        f"exclusive {route} route may declare only one default edge",
                        ("edges", index, "condition"),
                    ))
            elif defaults and len(route_edges) > 1:
                default_index, default_edge = defaults[0]
                ordered = sorted(
                    route_edges,
                    key=lambda item: (item[1].get("priority", 0), item[1]["id"]),
                )
                if ordered[-1][1]["id"] != default_edge["id"]:
                    diagnostics.append(_diagnostic(
                        document, "DSL_POLICY_INVALID",
                        f"exclusive {route} default edge must sort after every conditional edge",
                        ("edges", default_index, "priority"),
                        hint="assign the default edge a greater priority value than every conditional edge",
                    ))

    entries = data["entry"]
    terminals = data["terminals"]
    for field, values in [("entry", entries), ("terminals", terminals)]:
        for index, node_id in enumerate(values):
            if node_id not in node_by_id:
                diagnostics.append(_diagnostic(document, "DSL_REFERENCE_NOT_FOUND", f"{field} node {node_id!r} is not defined", (field, index)))
    for node_id in terminals:
        if node_id in node_by_id:
            index = node_index[node_id]
            if node_by_id[node_id]["kind"] != "terminal":
                diagnostics.append(_diagnostic(document, "DSL_GRAPH_NO_TERMINAL_PATH", f"terminal {node_id!r} must have kind 'terminal'", ("nodes", index, "kind")))
            if outgoing[node_id]:
                diagnostics.append(_diagnostic(document, "DSL_GRAPH_NO_TERMINAL_PATH", f"terminal {node_id!r} cannot have outgoing edges", ("terminals", terminals.index(node_id))))

    cycle = _find_cycle(set(node_by_id), dag_outgoing)
    if cycle is not None:
        diagnostics.append(_diagnostic(document, "DSL_GRAPH_CYCLE", "cycle detected: " + " -> ".join(cycle), ("edges",)))

    reachable: set[str] = set()
    stack = [item for item in entries if item in node_by_id]
    while stack:
        node_id = stack.pop()
        if node_id not in reachable:
            reachable.add(node_id)
            stack.extend(outgoing[node_id])
    for node_id in sorted(set(node_by_id) - reachable):
        diagnostics.append(_diagnostic(document, "DSL_GRAPH_UNREACHABLE", f"node {node_id!r} is unreachable", ("nodes", node_index[node_id], "id")))

    can_finish: set[str] = set(item for item in terminals if item in node_by_id)
    stack = list(can_finish)
    while stack:
        node_id = stack.pop()
        for parent in incoming[node_id]:
            if parent not in can_finish:
                can_finish.add(parent)
                stack.append(parent)
    for node_id in sorted(reachable - can_finish):
        diagnostics.append(_diagnostic(document, "DSL_GRAPH_NO_TERMINAL_PATH", f"node {node_id!r} has no path to a terminal", ("nodes", node_index[node_id], "id")))

    if diagnostics:
        raise DiagnosticError(sorted_diagnostics(diagnostics))
    return SemanticAnalysis(
        handlers=MappingProxyType(resolved),
        outgoing=MappingProxyType({key: tuple(sorted(value)) for key, value in outgoing.items()}),
        incoming=MappingProxyType({key: tuple(sorted(value)) for key, value in incoming.items()}),
    )
