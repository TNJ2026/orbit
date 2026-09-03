"""Restricted WorkflowIR-to-LangGraph compiler.

The authoring Agent never supplies Python.  It produces Orbit's declarative
DSL, the existing compiler turns that into a validated ``WorkflowIR``, and
this module binds the IR to an explicit allow-list of trusted callables.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from threading import Lock
from types import MappingProxyType
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from ..data.mapping import evaluate_mapping
from ..domain.definitions import IREdge, IRNode, WorkflowIR
from ..domain.serialization import freeze_json, to_primitive
from ..dsl.compiler import compile_source
from ..graph.conditions import evaluate_condition
from ..graph.input_assembly import assemble_join_inputs
from ..domain.graph import JoinMergeMode


class LangGraphCompileError(ValueError):
    """The validated IR uses semantics this adapter cannot execute safely."""


class HandlerBindingError(LangGraphCompileError):
    """A node does not resolve to the exact trusted Handler build in its IR."""


class LangGraphUnknownExternalResult(RuntimeError):
    """An external Handler may have acted and must not be executed again."""


class LangGraphRetryableError(RuntimeError):
    """A Handler explicitly classified a failure as safe to retry."""


class LangGraphRetryRequested(RuntimeError):
    def __init__(self, node_id, attempt_id, policy, cause, generation=1):
        self.node_id, self.attempt_id = node_id, attempt_id
        self.policy, self.cause = policy, cause
        # Which visit to this node failed. A node inside a bounded loop is
        # executed once per generation, and each generation gets the retry
        # budget the policy grants — counting them together spends one
        # generation's allowance on the failures of the ones before it.
        self.generation = generation
        super().__init__(str(cause))


class LangGraphJoinDeadlineExceeded(RuntimeError):
    """A deadline join fired without enough successful branches."""


class LangGraphCompletionUnsatisfied(RuntimeError):
    """A finished graph did not satisfy its terminal completion policy."""


@dataclass(frozen=True)
class LangGraphExecutionContext:
    workflow_id: str
    node_id: str
    run_id: str = ""
    attempt_id: str = ""
    input_ports: tuple[Mapping[str, Any], ...] = ()
    output_ports: tuple[Mapping[str, Any], ...] = ()
    actor: str = "system:langgraph"
    # The resolved `workspace_access` policy's own `config`, or None. Carried
    # here rather than as a bare mode string because a per-file grant needs
    # this node's own `files` list too — the same object the compile-time
    # cross-check already read out of `ir.policies`, handed down instead of
    # re-derived.
    workspace_access: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class HandlerOutcome:
    """A trusted Handler's data plus the edge family that should consume it."""

    output: Mapping[str, Any]
    route: str = "success"

    def __post_init__(self) -> None:
        if self.route not in {"success", "error", "timeout", "cancel"}:
            raise ValueError(f"unsupported Handler outcome route: {self.route!r}")
        if not isinstance(self.output, Mapping):
            raise TypeError("Handler outcome output must be a mapping")


HandlerCallable = Callable[
    [Mapping[str, Any], Mapping[str, Any], LangGraphExecutionContext],
    Mapping[str, Any] | HandlerOutcome,
]


@dataclass(frozen=True)
class BoundHandler:
    name: str
    version: str
    manifest_fingerprint: str
    invoke: HandlerCallable
    cancel_run: Callable[[str], bool] | None = None
    supported_transports: frozenset[str] = frozenset({"inline"})
    retry_safe: bool = False
    # The manifest's own `capabilities`, carried onto the compiled binding so
    # a policy cross-check (`workspace_access` needs `workspace.read`, the way
    # `retry` needs `retry_safe`) can ask the same object it already asks
    # about transports and retry-safety, instead of reaching back through the
    # registry for the manifest a second time.
    capabilities: frozenset[str] = frozenset()
    # Appended after the original public fields so embedders that construct a
    # BoundHandler positionally keep the meaning of every existing argument.
    finish_run: Callable[[str], None] | None = None
    # Attempt-scoped pruning for winner joins. Unlike ``cancel_run`` this must
    # not poison the whole run: the winner and its downstream join continue.
    cancel_attempts: Callable[[str, frozenset[str]], bool] | None = None
    # What this Handler's fingerprint was while the build number was part of
    # it. Published WorkflowVersions are immutable, so the ones written before
    # the version left the fingerprint still name that older value; accepting
    # it here is what keeps them runnable.
    legacy_manifest_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.version.strip():
            raise ValueError("handler name and version are required")
        if not self.manifest_fingerprint.startswith("sha256:"):
            raise ValueError("handler fingerprint must be sha256")
        if not callable(self.invoke):
            raise TypeError("handler invoke must be callable")
        if self.cancel_run is not None and not callable(self.cancel_run):
            raise TypeError("handler cancel_run must be callable")
        if self.cancel_attempts is not None and not callable(self.cancel_attempts):
            raise TypeError("handler cancel_attempts must be callable")
        if not self.supported_transports or not self.supported_transports <= {
            "inline", "artifact_ref", "secret_ref",
        }:
            raise ValueError("handler supported_transports are invalid")
        if not isinstance(self.retry_safe, bool):
            raise TypeError("handler retry_safe must be boolean")


class LangGraphRunCancelled(Exception):
    """Raised where a Handler would have started work on a cancelled run.

    Not a failure of the node and not retryable: the run was cancelled before
    this attempt claimed anything, so the honest outcome is that no work
    happened rather than that some work went wrong.
    """


class LangGraphHandlerRegistry:
    """Sealed allow-list used during graph compilation, keyed by Handler name.

    Identity is the name plus the manifest fingerprint — never the build
    number. A Handler's version says which release is installed, which is an
    operational fact that changes without the contract changing; binding on it
    meant a routine CLI upgrade made every Workflow that named the old build
    unresolvable, and there is nothing a Workflow author could have done
    differently to avoid that.
    """

    def __init__(self, handlers: Iterable[BoundHandler]) -> None:
        entries: dict[str, BoundHandler] = {}
        for handler in handlers:
            if handler.name in entries:
                raise ValueError(f"duplicate LangGraph handler: {handler.name}")
            entries[handler.name] = handler
        self._entries = MappingProxyType(entries)
        self._join_lock = Lock()
        self._join_progress: dict[
            tuple[str, str, int], dict[str, bool]
        ] = {}

    def resolve(self, node: IRNode) -> BoundHandler:
        reference = node.handler
        if reference is None:
            raise HandlerBindingError(f"node {node.id!r} has no Handler binding")
        handler = self._entries.get(reference.name)
        if handler is None:
            raise HandlerBindingError(
                f"handler not registered: {reference.name}"
            )
        accepted = {handler.manifest_fingerprint, handler.legacy_manifest_fingerprint}
        if reference.manifest_fingerprint not in accepted:
            raise HandlerBindingError(
                f"handler manifest mismatch: {reference.name}"
            )
        return handler

    def cancel(self, run_id: str) -> bool:
        """Best-effort signal to every handler currently executing this run."""

        signalled = False
        for handler in self._entries.values():
            if handler.cancel_run is not None:
                signalled = handler.cancel_run(run_id) or signalled
        return signalled

    def cancel_attempts(self, run_id: str, attempt_ids: frozenset[str]) -> bool:
        """Best-effort stop of selected attempts without cancelling their run."""

        if not attempt_ids:
            return False
        signalled = False
        for handler in self._entries.values():
            if handler.cancel_attempts is not None:
                signalled = (
                    handler.cancel_attempts(run_id, attempt_ids) or signalled
                )
        return signalled

    def settle_join_source(
        self,
        run_id: str,
        join_id: str,
        generation: int,
        incoming: tuple[tuple[str, str], ...],
        source_node: str,
        selected_edge_ids: frozenset[str],
        threshold: int,
        attempt_ids: Mapping[str, str],
    ) -> bool:
        """Prune a deterministic n-of-m suffix once its winners are fixed.

        Incoming edges are priority ordered.  Seeing any ``threshold`` results
        is not enough: an unresolved earlier edge could still displace a later
        result.  Once the prefix through the Nth selected edge is fully
        resolved, no edge after it can affect the join and its attempt may be
        stopped.
        """

        key = (run_id, join_id, generation)
        with self._join_lock:
            progress = self._join_progress.setdefault(key, {})
            for edge_id, edge_source in incoming:
                if edge_source == source_node:
                    progress[edge_id] = edge_id in selected_edge_ids
            selected = 0
            cutoff = None
            for index, (edge_id, _edge_source) in enumerate(incoming):
                if edge_id not in progress:
                    break
                if progress[edge_id]:
                    selected += 1
                    if selected == threshold:
                        cutoff = index
                        break
            if cutoff is None:
                return False
            losers = frozenset(
                attempt_ids[edge_source]
                for edge_id, edge_source in incoming[cutoff + 1:]
                if edge_id not in progress and edge_source in attempt_ids
            )
            self._join_progress.pop(key, None)
        return self.cancel_attempts(run_id, losers)

    def finish(self, run_id: str) -> None:
        """This process is done driving the run; drop what was held for it.

        Cancellation is remembered by the Handlers that must refuse to start
        work, and something has to say when refusing can stop. Nothing else
        knows: the adapters see attempts, not runs, and a run's last attempt
        is only recognisable afterwards.
        """

        for handler in self._entries.values():
            if handler.finish_run is not None:
                handler.finish_run(run_id)
        with self._join_lock:
            stale = [key for key in self._join_progress if key[0] == run_id]
            for key in stale:
                self._join_progress.pop(key, None)


def _merge_dicts(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    """Merge parallel results and let a loop replace its prior node output."""

    merged = dict(left)
    merged.update(right)
    return merged


def _append_order(left: Iterable[str], right: Iterable[str]) -> tuple[str, ...]:
    """Keep deterministic order across serializers that decode tuples as lists."""

    return (*left, *right)


class _GraphState(TypedDict):
    workflow_inputs: Mapping[str, Any]
    node_outputs: Annotated[dict[str, Mapping[str, Any]], _merge_dicts]
    node_routes: Annotated[dict[str, str], _merge_dicts]
    execution_order: Annotated[tuple[str, ...], _append_order]
    join_deadlines: Annotated[dict[str, bool], _merge_dicts]


def _edge_value(
    edge: IREdge,
    source_output: Mapping[str, Any],
    workflow_inputs: Mapping[str, Any],
) -> Any:
    mapped = evaluate_mapping(
        edge.mapping, source_output, workflow_inputs=workflow_inputs
    )
    if isinstance(mapped, Mapping):
        if edge.target_port in mapped:
            return mapped[edge.target_port]
        if edge.source_port in mapped:
            return mapped[edge.source_port]
    return mapped


def _assemble_inputs(
    ir: WorkflowIR, node: IRNode, state: _GraphState
) -> Mapping[str, Any]:
    workflow_inputs = state["workflow_inputs"]
    assembled: dict[str, Any] = (
        {
            port.id: workflow_inputs[port.id]
            for port in node.inputs
            if port.id in workflow_inputs
        }
        if node.id in ir.entry
        else {}
    )
    incoming = sorted(
        (edge for edge in ir.edges if edge.target_node == node.id),
        # Back edges carry the next generation's value and must supersede the
        # original ingress regardless of how their ids sort against it.
        key=lambda edge: (edge.back_edge, edge.priority, edge.id),
    )
    selected_values: list[tuple[Any, Any]] = []
    for edge in incoming:
        source_output = state["node_outputs"].get(edge.source_node)
        if source_output is None:
            continue
        if state["node_routes"].get(edge.source_node, "success") != edge.route:
            continue
        if not evaluate_condition(
            edge.condition, source_output, workflow_inputs=workflow_inputs
        ):
            continue
        value = _edge_value(edge, source_output, workflow_inputs)
        if node.kind == "join":
            selected_values.append((edge, value))
            continue
        if edge.target_port in assembled and assembled[edge.target_port] != value:
            # Once a loop has produced a value, its declared back edge is the
            # next generation's input and supersedes the original ingress.
            if not edge.back_edge:
                raise ValueError(
                    f"multiple selected edges write input {node.id}.{edge.target_port}"
                )
        assembled[edge.target_port] = value
    if node.kind == "join" and selected_values:
        policy = next(
            item for item in ir.policies
            if item.id in node.policies and item.kind == "join"
        )
        merge_mode = JoinMergeMode(
            policy.config.get("merge_mode", "array_by_edge")
        )
        mode = policy.config.get("mode")
        by_port: dict[str, list[tuple[Any, Any]]] = {}
        for edge, value in selected_values:
            by_port.setdefault(edge.target_port, []).append((edge, value))
        for port_id, values in by_port.items():
            if mode == "any":
                values = values[:1]
            elif mode == "n_of_m":
                threshold = int(policy.config["threshold"])
                if len(values) < threshold:
                    raise ValueError(
                        f"join {node.id!r} cannot satisfy threshold {threshold}"
                    )
                values = values[:threshold]
            ordered = tuple(edge.id for edge, _ in values)
            assembled[port_id] = to_primitive(assemble_join_inputs(
                merge_mode, {edge.id: value for edge, value in values}, ordered,
            ))
    for port in node.inputs:
        if port.id not in assembled and port.has_default:
            assembled[port.id] = to_primitive(port.default)
        # A join's policy decides how many of its inputs must be there, and
        # the per-port flag contradicts it: an `any` join is satisfied by one
        # branch and every other port is *meant* to be absent, while for `all`
        # a branch an upstream decision ruled out can never arrive at any
        # port. The mode is the authority — the threshold it implies is
        # checked above, and scheduling will not run a join before it holds.
        if node.kind == "join":
            continue
        if port.required and port.id not in assembled:
            raise ValueError(f"missing required input {node.id}.{port.id}")
    return to_primitive(assembled)


def _normalize_outputs(node: IRNode, result: Mapping[str, Any]) -> Mapping[str, Any]:
    declared = {port.id for port in node.outputs}
    unknown = set(result) - declared
    if unknown:
        raise ValueError(
            f"handler for {node.id!r} returned undeclared outputs: {sorted(unknown)}"
        )
    missing = {port.id for port in node.outputs if port.required and port.id not in result}
    if missing:
        raise ValueError(
            f"handler for {node.id!r} omitted required outputs: {sorted(missing)}"
        )
    return to_primitive(dict(result))


def validate_human_response(node: IRNode, resumed: Any) -> Mapping[str, Any]:
    """Normalize one Human answer and enforce the approval wire contract."""

    if isinstance(resumed, Mapping):
        human_output = resumed
    elif len(node.outputs) == 1:
        human_output = {node.outputs[0].id: resumed}
    else:
        raise ValueError(f"human node {node.id!r} requires an object response")
    normalized = _normalize_outputs(node, human_output)
    if node.config.get("task_kind") != "approval":
        return normalized
    if len(node.outputs) != 1:
        raise ValueError("approval response requires exactly one output port")
    submission = normalized[node.outputs[0].id]
    if not isinstance(submission, Mapping):
        raise ValueError("approval response must contain an object submission")
    if set(submission) != {"decision", "value"}:
        raise ValueError(
            "approval submission must contain exactly 'decision' and 'value'"
        )
    if submission.get("decision") not in {"approve", "reject"}:
        raise ValueError("approval decision must be 'approve' or 'reject'")
    return normalized


def _handlerless_outputs(
    node: IRNode, inputs: Mapping[str, Any]
) -> Mapping[str, Any]:
    if not node.outputs:
        return {}
    copied = {port.id: inputs[port.id] for port in node.outputs if port.id in inputs}
    if not copied and len(node.outputs) == 1:
        if len(node.inputs) == 1:
            # One in, one out, and no work in between: the value passes
            # straight through. True of every node without a Handler — a
            # decision routes what it was handed, a join is a rendezvous, a
            # human node's submission is its output — and matching the port
            # *names* was standing in for that. A node whose output happened
            # to be called something else produced nothing and failed as a
            # Handler that omitted its own declared output.
            copied[node.outputs[0].id] = inputs[node.inputs[0].id]
        elif node.kind == "join":
            # Several branches into one output. The merge policy has already
            # combined the edges feeding each input port; what was missing was
            # any rule for putting those ports together, so a join whose
            # output happened not to share a name with an input produced
            # nothing at all and failed as a Handler that omitted its own
            # declared output. Keyed by input port, because that is the name
            # the author gave each branch and the only one downstream can
            # refer to.
            copied[node.outputs[0].id] = dict(inputs)
    return _normalize_outputs(node, copied)


@dataclass(frozen=True)
class CompiledLangGraphWorkflow:
    """A compiled graph plus a small public result-oriented invocation API."""

    ir: WorkflowIR
    graph: Any

    def _config(self, config: Mapping[str, Any] | None) -> dict[str, Any]:
        value = dict(config or {})
        back_edge_limit = max(
            (
                int(policy.config[
                    "max_iterations" if policy.kind == "loop"
                    else "max_generations"
                ])
                for policy in self.ir.policies
                if policy.kind in {"loop", "rework"}
            ),
            default=0,
        )
        value.setdefault(
            "recursion_limit",
            max(25, back_edge_limit * max(1, len(self.ir.nodes)) + 10),
        )
        return value

    def invoke(
        self,
        inputs: Mapping[str, Any] | None,
        *,
        config: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        if inputs is None:
            state = self.graph.invoke(None, config=self._config(config))
            return self._result(state)
        normalized = self._accept(inputs)
        state = self.graph.invoke(
            {
                "workflow_inputs": to_primitive(normalized),
                "node_outputs": {},
                "node_routes": {},
                "execution_order": (),
                "join_deadlines": {},
            },
            config=self._config(config),
        )
        return self._result(state)

    def stream(
        self,
        inputs: Mapping[str, Any] | None,
        *,
        config: Mapping[str, Any] | None = None,
        stream_mode: str = "updates",
    ):
        """Yield LangGraph execution chunks without weakening input validation."""

        if inputs is None:
            graph_input = None
        else:
            graph_input = {
                "workflow_inputs": to_primitive(self._accept(inputs)),
                "node_outputs": {},
                "node_routes": {},
                "execution_order": (),
                "join_deadlines": {},
            }
        return self.graph.stream(
            graph_input, config=self._config(config), stream_mode=stream_mode
        )

    def validate_inputs(self, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
        """Validate and default a Run input without starting graph execution."""

        return self._accept(inputs)

    def _accept(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        """A run's input, checked against the interface and defaulted.

        One copy. `invoke` and `stream` each carried their own, so fixing the
        interface in one left the other refusing what the first accepted —
        and `start` goes through `invoke`.
        """

        interface = self._interface()
        unknown = set(inputs) - {port.id for port in interface}
        if unknown:
            raise ValueError(f"unknown workflow inputs: {sorted(unknown)}")
        normalized = dict(inputs)
        for port in interface:
            if port.id not in normalized and port.has_default:
                normalized[port.id] = to_primitive(port.default)
            if port.required and port.id not in normalized:
                raise ValueError(f"missing workflow input {port.id!r}")
        return normalized

    def _interface(self) -> tuple[Any, ...]:
        """The ports a run's input is accepted under.

        `inputs` when the definition declares them, and otherwise the entry
        nodes' own ports — which is what everything else already treats as the
        interface. The kernel delivers one input object to every entry node,
        the catalog reads those ports to decide a workflow is startable from a
        Goal, and the goal binding names one of them. Only this validation
        read `ir.inputs` alone, so a definition without that optional block was
        advertised as ready and then refused the very input the catalog said
        to send.
        """

        if self.ir.inputs:
            return tuple(self.ir.inputs)
        by_id = {node.id: node for node in self.ir.nodes}
        ports: dict[str, Any] = {}
        for node_id in self.ir.entry:
            for port in getattr(by_id.get(node_id), "inputs", ()):
                ports.setdefault(port.id, port)
        return tuple(ports.values())

    def resume(
        self,
        value: Any,
        *,
        config: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Resume a checkpointed LangGraph interrupt with its external value."""

        state = self.graph.invoke(Command(resume=value), config=self._config(config))
        return self._result(state)

    def fire_join_deadline(
        self, node_id: str, *, config: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Wake one deadline join from its durable checkpoint."""

        node = next((item for item in self.ir.nodes if item.id == node_id), None)
        if node is None or node.kind != "join":
            raise ValueError(f"deadline target is not a join node: {node_id!r}")
        policy = next((
            item for item in self.ir.policies
            if item.id in node.policies and item.kind == "join"
            and item.config.get("mode") == "deadline"
        ), None)
        if policy is None:
            raise ValueError(f"join {node_id!r} has no deadline policy")
        snapshot = self.graph.get_state(self._config(config))
        values = dict(snapshot.values)
        if (
            node_id in values.get("execution_order", ())
            and values.get("join_deadlines", {}).get(node_id)
        ):
            return self._result(values)
        outputs = values.get("node_outputs", {})
        incoming = tuple(
            edge for edge in self.ir.edges if edge.target_node == node_id
        )
        successful = sum(
            1 for edge in incoming if edge.source_node in outputs
            and values.get("node_routes", {}).get(edge.source_node, "success")
            == edge.route
            and evaluate_condition(
                edge.condition,
                outputs[edge.source_node],
                workflow_inputs=values.get("workflow_inputs", {}),
            )
        )
        minimum = int(policy.config["min_successful"])
        if successful < minimum:
            raise LangGraphJoinDeadlineExceeded(
                f"join {node_id!r} timed out with {successful}/{minimum} successes"
            )
        values["join_deadlines"] = {node_id: True}
        state = self.graph.invoke(
            Command(
                update={"join_deadlines": {node_id: True}},
                goto=Send(node_id, values),
            ),
            config=self._config(config),
        )
        # That invoke ran the join in memory and persisted nothing, so the join
        # is re-applied as a write. `update_state` branches from the *stored*
        # checkpoint, which is the one the interrupted superstep never
        # committed: the branches that finished before the deadline are still
        # only pending writes there. Re-applying just the join would therefore
        # publish a state in which the branch that won the deadline never ran —
        # absent from `node_outputs` for anything downstream that also reads
        # it, and absent from `execution_order`, which is what numbers a
        # handler's attempts.
        self.graph.update_state(
            self._config(config),
            self._pending_join_writes(
                node_id, values, state, config, deadline=True,
            ),
            as_node=node_id,
        )
        if any(edge.source_node == node_id for edge in self.ir.edges):
            state = self.graph.invoke(None, config=self._config(config))
        return self._result(state)

    def fire_ready_winner_join(
        self, *, config: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        """Commit a satisfied winner join past unrelated pending interrupts.

        LangGraph does not finish a superstep while any sibling task remains
        interrupted. A partial resume can therefore produce the answer that
        satisfies an any/n-of-m join without ever giving its router a turn. The
        state (including pending writes) is complete enough to run that join;
        persist it on a fresh checkpoint branch just as a deadline join does.
        """

        snapshot = self.graph.get_state(self._config(config))
        values = dict(snapshot.values)
        policies = {policy.id: policy for policy in self.ir.policies}
        for join in self.ir.nodes:
            if join.kind != "join" or join.id in values.get("execution_order", ()):
                continue
            policy = next((
                policies[item] for item in join.policies
                if item in policies and policies[item].kind == "join"
            ), None)
            if (
                policy is None
                or policy.config.get("mode") not in {"any", "n_of_m"}
            ):
                continue
            if not _join_is_ready(self.ir, join, values, (), policies):
                continue
            state = self.graph.invoke(
                Command(goto=Send(join.id, values)), config=self._config(config),
            )
            self.graph.update_state(
                self._config(config),
                self._pending_join_writes(
                    join.id, values, state, config, deadline=False,
                ),
                as_node=join.id,
            )
            if any(edge.source_node == join.id for edge in self.ir.edges):
                state = self.graph.invoke(None, config=self._config(config))
            return self._result(state)
        return None

    def _pending_join_writes(
        self,
        node_id: str,
        values: Mapping[str, Any],
        state: Mapping[str, Any],
        config: Mapping[str, Any],
        *,
        deadline: bool,
    ) -> dict[str, Any]:
        """The join's own writes, plus the ones the checkpoint is missing.

        `values` is what the graph reports, which includes the writes pending
        from the interrupted superstep; the checkpoint holds only what was
        committed before it. The difference is what would be lost, and it is
        replayed here rather than recomputed — every channel merges, and
        `execution_order` merges by appending, so this must carry each entry
        exactly once or a handler's attempt numbering shifts under it.
        """

        committed = self._committed_values(config)
        order = tuple(values.get("execution_order") or ())
        # Appended to and never rewritten, so what is committed is a prefix.
        missing = order[len(tuple(committed.get("execution_order") or ())):]
        outputs = values.get("node_outputs") or {}
        routes = values.get("node_routes") or {}
        writes = {
            "node_outputs": {
                **{name: outputs[name] for name in missing if name in outputs},
                node_id: state.get("node_outputs", {})[node_id],
            },
            "node_routes": {
                **{name: routes[name] for name in missing if name in routes},
                node_id: state.get("node_routes", {}).get(node_id, "success"),
            },
            "execution_order": (*missing, node_id),
        }
        if deadline:
            writes["join_deadlines"] = {node_id: True}
        return writes

    def _committed_values(self, config: Mapping[str, Any]) -> Mapping[str, Any]:
        """What the checkpoint holds, without the writes still pending on it.

        `get_state` deliberately shows both, which is the right answer for
        every other caller and the wrong one for deciding what a write has to
        restore.
        """

        checkpointer = getattr(self.graph, "checkpointer", None)
        if checkpointer is None:
            return {}
        stored = checkpointer.get_tuple(self._config(config))
        if stored is None:
            return {}
        return stored.checkpoint.get("channel_values") or {}

    def _result(self, state: Mapping[str, Any]) -> Mapping[str, Any]:
        if not state.get("__interrupt__") and not self.completion_satisfied(state):
            required_terminals = self._required_terminal_count()
            reached_terminals = self._reached_terminal_count(state)
            raise LangGraphCompletionUnsatisfied(
                f"completion requires {required_terminals} successful terminals; "
                f"reached {reached_terminals}"
            )
        result = None
        if self.ir.result is not None:
            producer = state.get("node_outputs", {}).get(self.ir.result.node_id)
            if producer is not None:
                result = producer.get(self.ir.result.output_port_id)
        return {
            "result": to_primitive(result),
            "node_outputs": to_primitive(state.get("node_outputs", {})),
            "node_routes": dict(state.get("node_routes", {})),
            "execution_order": list(state.get("execution_order", ())),
        }

    def _required_terminal_count(self) -> int:
        completion = next((
            policy for policy in self.ir.policies
            if policy.kind == "completion"
        ), None)
        return int(
            completion.config.get("required_terminal_count", 1)
            if completion is not None else 1
        )

    def _reached_terminal_count(self, state: Mapping[str, Any]) -> int:
        # Distinct terminals, not terminal executions. Counting repeats let a
        # workflow with one terminal satisfy `required_terminal_count: 2` by
        # reaching that terminal twice — which a rework or a loop back edge
        # upstream makes reachable — without a second terminal ever existing.
        return len({
            node_id for node_id in state.get("execution_order", ())
            if node_id in self.ir.terminals
        })

    def completion_satisfied(self, state: Mapping[str, Any]) -> bool:
        """Whether the declared terminal quorum has already been reached."""

        return self._reached_terminal_count(state) >= self._required_terminal_count()


def outgoing_edges(ir: WorkflowIR, node: IRNode) -> tuple[IREdge, ...]:
    """This node's edges in the order the engine considers them.

    Priority then id, except that in exclusive mode an unconditional edge
    sorts last however it was numbered: a default is what is left when no
    condition matched, so letting it win on priority would make every
    condition beside it unreachable.

    Kept in one place because two things read it. The engine picks the first
    match; the run's edge report says which edges never matched, and a report
    that ordered them differently would name the wrong edge as shadowed.
    """

    exclusive = (node.route_mode or "exclusive") == "exclusive"
    return tuple(sorted(
        (edge for edge in ir.edges if edge.source_node == node.id),
        key=lambda edge: (
            exclusive and edge.condition == {"op": "literal", "value": True},
            edge.priority,
            edge.id,
        ),
    ))


def edge_is_selected(
    edge: IREdge, state: Mapping[str, Any],
) -> bool | None:
    """Whether this edge fired, or `None` while its source has not run.

    Three answers, not two. An edge whose source has produced nothing is
    undecided; one whose source ran and routed elsewhere is decided *against*
    and will never fire. A join that cannot tell those apart either waits for
    a branch that is never coming or merges before one that is.
    """

    outputs = state.get("node_outputs") or {}
    if edge.source_node not in outputs:
        return None
    if edge.route != (state.get("node_routes") or {}).get(
        edge.source_node, "success"
    ):
        return False
    return bool(evaluate_condition(
        edge.condition,
        outputs[edge.source_node],
        workflow_inputs=state.get("workflow_inputs", {}),
    ))


def _still_possible(
    ir: WorkflowIR, state: Mapping[str, Any], frontier: Sequence[str],
) -> set[str]:
    """Nodes that may still execute, given what has run and what is scheduled.

    Reached from `frontier` — the nodes about to be scheduled — over every
    edge that has not already been decided against. A node not in this set can
    never produce anything, which is what lets a join stop waiting for it.
    """

    dead = {
        edge.id for edge in ir.edges if edge_is_selected(edge, state) is False
    }
    live: set[str] = set(frontier)
    queue = list(live)
    while queue:
        current = queue.pop()
        for edge in ir.edges:
            if edge.source_node != current or edge.id in dead:
                continue
            if edge.target_node not in live:
                live.add(edge.target_node)
                queue.append(edge.target_node)
    return live


def _join_is_ready(
    ir: WorkflowIR, join: IRNode, state: Mapping[str, Any],
    frontier: Sequence[str], policies: Mapping[str, IRPolicy],
) -> bool:
    """Whether this join may run now, or must wait for a branch still coming.

    A join used to be an ordinary node: whoever reached it scheduled it, and
    it merged whatever had arrived. Branches that finished in different
    supersteps therefore ran it once each — merging partial data the first
    time, and re-running everything downstream the second. A workflow whose
    merge step was an Agent action performed that merge twice.

    `all` waits for every branch that can still arrive. `any` and `n_of_m`
    need their count and no more, so they run as soon as it is met. A
    `deadline` join is the one that may legitimately never be satisfied, and
    its timer — not this — decides when waiting ends.
    """

    incoming = [edge for edge in ir.edges if edge.target_node == join.id]
    if not incoming:
        return True
    policy = next(
        (
            policies[item] for item in join.policies
            if item in policies and policies[item].kind == "join"
        ),
        None,
    )
    mode = "all" if policy is None else policy.config.get("mode", "all")
    arrived = sum(1 for edge in incoming if edge_is_selected(edge, state) is True)
    if mode == "any":
        return arrived >= 1
    if mode == "n_of_m":
        return arrived >= int(policy.config["threshold"])
    if mode == "deadline":
        return arrived >= int(policy.config["min_successful"])
    # `all` and `all_successful`: nothing that could still arrive may be
    # outstanding. A source that can no longer run is not worth waiting for.
    possible = _still_possible(ir, state, frontier)
    return not any(
        edge_is_selected(edge, state) is None and edge.source_node in possible
        for edge in incoming
    )


def compile_workflow(
    ir: WorkflowIR,
    registry: LangGraphHandlerRegistry,
    *,
    checkpointer: Any = None,
) -> CompiledLangGraphWorkflow:
    """Bind a validated WorkflowIR to trusted handlers and compile StateGraph."""

    if not isinstance(ir, WorkflowIR):
        raise TypeError("compile_workflow requires WorkflowIR")
    if ir.extensions:
        identifiers = ", ".join(
            sorted(extension.extension_id for extension in ir.extensions)
        )
        raise LangGraphCompileError(
            "LangGraph workflows do not support extensions: " + identifiers
        )
    unsupported_nodes = tuple(
        sorted(
            (
                node for node in ir.nodes
                if node.kind not in {
                    "action", "human", "decision", "join", "terminal",
                }
            ),
            key=lambda node: node.id,
        )
    )
    if unsupported_nodes:
        summary = ", ".join(
            f"{node.id}:{node.kind}" for node in unsupported_nodes
        )
        raise LangGraphCompileError(
            "LangGraph node kinds are not supported: " + summary
        )
    unsupported_policies = tuple(
        sorted(
            (
                policy for policy in ir.policies
                if policy.kind not in {
                    "route", "retry", "join", "loop", "rework", "completion",
                    "workspace_access",
                }
                or (
                    policy.kind == "join"
                    and policy.config.get("mode") not in {
                        "all", "all_successful", "any", "n_of_m", "deadline",
                    }
                )
            ),
            key=lambda policy: policy.id,
        )
    )
    if unsupported_policies:
        summary = ", ".join(
            f"{policy.id}:{policy.kind}" for policy in unsupported_policies
        )
        raise LangGraphCompileError(
            "LangGraph policy semantics are not yet supported: " + summary
        )
    for policy in (item for item in ir.policies if item.kind == "retry"):
        maximum = policy.config.get("max_attempts")
        backoff = policy.config.get("backoff_seconds", ())
        if (
            isinstance(maximum, bool) or not isinstance(maximum, int)
            or maximum < 1 or not isinstance(backoff, (list, tuple))
            or len(backoff) > maximum - 1
            or any(
                isinstance(delay, bool) or not isinstance(delay, int) or delay < 0
                for delay in backoff
            )
        ):
            raise LangGraphCompileError(
                f"retry policy {policy.id!r} has invalid attempts or backoff"
            )
    for policy in (item for item in ir.policies if item.kind == "join"):
        try:
            JoinMergeMode(policy.config.get("merge_mode", "array_by_edge"))
        except ValueError:
            raise LangGraphCompileError(
                f"join policy {policy.id!r} has invalid merge_mode"
            ) from None
        mode = policy.config.get("mode")
        threshold = policy.config.get("threshold")
        if mode == "n_of_m" and (
            isinstance(threshold, bool) or not isinstance(threshold, int)
            or threshold < 1
        ):
            raise LangGraphCompileError(
                f"join policy {policy.id!r} requires a positive threshold"
            )
        if mode == "deadline":
            deadline = policy.config.get("deadline_seconds")
            minimum = policy.config.get("min_successful")
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 1
                for value in (deadline, minimum)
            ):
                raise LangGraphCompileError(
                    f"join policy {policy.id!r} requires positive deadline_seconds"
                    " and min_successful"
                )
    for policy in (item for item in ir.policies if item.kind == "loop"):
        maximum = policy.config.get("max_iterations")
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
            raise LangGraphCompileError(
                f"loop policy {policy.id!r} requires positive max_iterations"
            )
        if policy.config.get("exhaustion", "fail") not in {"fail", "error_route"}:
            raise LangGraphCompileError(
                f"loop policy {policy.id!r} has invalid exhaustion"
            )
    for policy in (item for item in ir.policies if item.kind == "rework"):
        maximum = policy.config.get("max_generations")
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
            raise LangGraphCompileError(
                f"rework policy {policy.id!r} requires positive max_generations"
            )
        if policy.config.get("exhaustion", "fail") not in {"fail", "error_route"}:
            raise LangGraphCompileError(
                f"rework policy {policy.id!r} has invalid exhaustion"
            )
    completion_policies = tuple(
        item for item in ir.policies if item.kind == "completion"
    )
    if len(completion_policies) > 1:
        raise LangGraphCompileError(
            "workflow declares multiple completion policies"
        )
    for policy in completion_policies:
        unknown = set(policy.config) - {"required_terminal_count"}
        required = policy.config.get("required_terminal_count", 1)
        if (
            unknown or isinstance(required, bool)
            or not isinstance(required, int) or required < 1
        ):
            raise LangGraphCompileError(
                f"completion policy {policy.id!r} requires positive "
                "required_terminal_count"
            )
    policies_by_id = {policy.id: policy for policy in ir.policies}
    nodes_by_id = {node.id: node for node in ir.nodes}
    for edge in (item for item in ir.edges if item.back_edge):
        policy = policies_by_id.get(edge.policy_ref or "")
        if policy is None or policy.kind not in {"loop", "rework"}:
            raise LangGraphCompileError(
                f"back edge {edge.id!r} requires a loop or rework policy"
            )
    bound = {
        node.id: registry.resolve(node)
        for node in ir.nodes
        if node.handler is not None
    }
    for node in ir.nodes:
        # A decision belongs here too, and its absence made every workflow
        # containing one impossible to run: the DSL layer refuses a handler on
        # a decision node — it routes, it does not execute — while this
        # required one of every kind not listed. Two rules that cannot both be
        # satisfied, and no test compiled a decision through this engine, so
        # nothing said so until an Agent wrote a workflow that branched.
        if node.handler is None and node.kind not in {
            "terminal", "join", "human", "decision",
        }:
            raise HandlerBindingError(
                f"executable node {node.id!r} has no Handler binding"
            )
        # Only a node that has a Handler is asked what its Handler accepts. A
        # terminal, join, decision or human node has none — the value is
        # carried through, not handed to anything — and defaulting them to
        # `inline` refused the very shape the authoring rules demand:
        # MARKDOWN_ARTIFACT_REQUIRED tells an author to declare the Goal
        # result an `artifact_ref` and to "carry the same policy to the
        # terminal input", and this then refused the terminal for carrying it.
        supported = (
            bound[node.id].supported_transports if node.id in bound else None
        )
        retry_policies = tuple(
            policy for policy in ir.policies
            if policy.id in node.policies and policy.kind == "retry"
        )
        if len(retry_policies) > 1:
            raise LangGraphCompileError(
                f"node {node.id!r} declares multiple retry policies"
            )
        if retry_policies and (
            node.id not in bound or not bound[node.id].retry_safe
        ):
            raise LangGraphCompileError(
                f"node {node.id!r} retry policy requires a retry-safe Handler"
            )
        join_policies = tuple(
            policy for policy in ir.policies
            if policy.id in node.policies and policy.kind == "join"
        )
        if node.kind == "join" and len(join_policies) != 1:
            raise LangGraphCompileError(
                f"join node {node.id!r} requires exactly one join policy"
            )
        workspace_policies = tuple(
            policy for policy in ir.policies
            if policy.id in node.policies and policy.kind == "workspace_access"
        )
        if len(workspace_policies) > 1:
            raise LangGraphCompileError(
                f"node {node.id!r} declares multiple workspace_access policies"
            )
        if workspace_policies:
            # Which of the two capability strings the operator's flag granted
            # says which shape of workspace this deployment hands out — a
            # whole worktree (git) needs no file list, a per-file copy
            # (anything else) cannot be carried out without one. Reading it
            # off the same `bound` binding `retry_safe`/`supported_transports`
            # already come from avoids threading a second, deployment-kind
            # parameter through every `compile_workflow` call site.
            granted = bound[node.id].capabilities if node.id in bound else frozenset()
            config = workspace_policies[0].config or {}
            if config.get("isolation") == "none":
                # The real project directory, not a copy of it. A separate
                # grant from the disposable-copy ones below, and separate
                # again for writing: reading a developer's actual files and
                # editing them are different things to consent to, and the
                # switch that allows one must not quietly allow the other.
                if "workspace.project.read" not in granted:
                    raise LangGraphCompileError(
                        f"node {node.id!r} asks for the project directory "
                        "itself, which this Runtime was not started to grant"
                    )
                if (
                    config.get("mode") == "read_write"
                    and "workspace.project.write" not in granted
                ):
                    raise LangGraphCompileError(
                        f"node {node.id!r} asks to write the project "
                        "directory, which this Runtime was not started to grant"
                    )
            elif "workspace.read" in granted:
                pass
            elif "workspace.read.files" in granted:
                if not config.get("files"):
                    raise LangGraphCompileError(
                        f"node {node.id!r} workspace_access policy requires "
                        "config.files on this deployment"
                    )
            else:
                raise LangGraphCompileError(
                    f"node {node.id!r} requires a Handler granted workspace access"
                )
        for direction, ports in (
            (("input", node.inputs), ("output", node.outputs))
            if supported is not None else ()
        ):
            for port in ports:
                transport = port.data_policy.transport.value
                if transport not in supported:
                    raise LangGraphCompileError(
                        f"node {node.id!r} {direction} port {port.id!r} uses "
                        f"{transport!r}, unsupported by its LangGraph Handler"
                    )
    graph_transports = frozenset().union(
        *(handler.supported_transports for handler in bound.values())
    ) if bound else frozenset({"inline"})
    for owner, ports in (("workflow input", ir.inputs), ("workflow output", ir.outputs)):
        for port in ports:
            transport = port.data_policy.transport.value
            if transport not in graph_transports:
                raise LangGraphCompileError(
                    f"{owner} port {port.id!r} uses {transport!r}, unsupported "
                    "by the LangGraph Handlers in this workflow"
                )

    builder = StateGraph(_GraphState)

    # Winner joins are deterministic, not timing lotteries.  Keep each join's
    # incoming edges in priority order so the registry can stop the suffix only
    # after the prefix through the Nth selected edge is fully resolved.  This
    # works for every n-of-m threshold and still prevents a quick low-priority
    # branch from displacing a slower high-priority one.
    early_joins_by_source: dict[
        str, list[tuple[str, tuple[tuple[str, str], ...], int]]
    ] = {}
    for join in (item for item in ir.nodes if item.kind == "join"):
        policy = next((
            policies_by_id[item] for item in join.policies
            if item in policies_by_id and policies_by_id[item].kind == "join"
        ), None)
        mode = None if policy is None else policy.config.get("mode")
        if mode not in {"any", "n_of_m"}:
            continue
        incoming = sorted(
            (edge for edge in ir.edges if edge.target_node == join.id),
            key=lambda edge: (edge.priority, edge.id),
        )
        ordered = tuple((edge.id, edge.source_node) for edge in incoming)
        threshold = (
            1 if mode == "any" else int(policy.config["threshold"])
        )
        for source_node in dict.fromkeys(edge.source_node for edge in incoming):
            early_joins_by_source.setdefault(source_node, []).append(
                (join.id, ordered, threshold)
            )

    for node in ir.nodes:
        handler = bound.get(node.id)

        def execute(
            state: _GraphState,
            config: RunnableConfig,
            *,
            current=node,
            implementation=handler,
        ):
            inputs = _assemble_inputs(ir, current, state)
            retry_policy = next((
                policy for policy in ir.policies
                if policy.id in current.policies and policy.kind == "retry"
            ), None)
            workspace_policy = next((
                policy for policy in ir.policies
                if policy.id in current.policies and policy.kind == "workspace_access"
            ), None)
            if implementation is None:
                if current.kind == "human":
                    resumed = interrupt({
                        "workflow_id": ir.workflow_id,
                        "node_id": current.id,
                        "input": to_primitive(inputs),
                        "config": to_primitive(current.config),
                        # The shape of the answer, in the question. Without it
                        # a caller had to fetch the definition to learn which
                        # port to reply on, and a reply on the wrong one was
                        # refused as a "handler" that returned undeclared
                        # outputs — for a node that has no handler.
                        "output_ports": [
                            {
                                "id": port.id,
                                "schema_id": port.schema_id,
                                "required": port.required,
                            }
                            for port in current.outputs
                        ],
                    })
                    output = validate_human_response(current, resumed)
                else:
                    output = _handlerless_outputs(current, inputs)
            else:
                execution_context = LangGraphExecutionContext(
                        ir.workflow_id,
                        current.id,
                        str(config.get("configurable", {}).get("thread_id", "")),
                        (
                            "langgraph_attempt:"
                            + str(config.get("configurable", {}).get("thread_id", ""))
                            + f":{current.id}:"
                            + str(state.get("execution_order", ()).count(current.id) + 1)
                        ),
                        tuple(to_primitive(port) for port in current.inputs),
                        tuple(to_primitive(port) for port in current.outputs),
                        str(config.get("configurable", {}).get(
                            "actor", "system:langgraph"
                        )),
                        workspace_access=(
                            to_primitive(workspace_policy.config)
                            if workspace_policy is not None else None
                        ),
                    )
                try:
                    raw = implementation.invoke(
                        inputs, current.config, execution_context,
                    )
                except LangGraphRetryableError as exc:
                    if retry_policy is None:
                        raise
                    raise LangGraphRetryRequested(
                        current.id, execution_context.attempt_id,
                        to_primitive(retry_policy.config), exc,
                        state.get("execution_order", ()).count(current.id) + 1,
                    ) from exc
                outcome = raw if isinstance(raw, HandlerOutcome) else HandlerOutcome(raw)
                if not isinstance(outcome.output, Mapping):
                    raise TypeError(f"handler for {current.id!r} must return a mapping")
                output = _normalize_outputs(current, outcome.output)
            route_name = "success" if implementation is None else outcome.route
            if route_name == "success":
                outgoing = outgoing_edges(ir, current)
                selected = [
                    edge for edge in outgoing
                    if edge.route == route_name and evaluate_condition(
                        edge.condition,
                        output,
                        workflow_inputs=state["workflow_inputs"],
                    )
                ]
                if (current.route_mode or "exclusive") != "parallel":
                    selected = selected[:1]
                exhausted = next((
                    policies_by_id[edge.policy_ref]
                    for edge in selected if edge.back_edge
                    and policies_by_id[edge.policy_ref].config.get(
                        "exhaustion", "fail",
                    ) == "error_route"
                    and state.get("execution_order", ()).count(current.id) >= int(
                        policies_by_id[edge.policy_ref].config[
                            "max_iterations"
                            if policies_by_id[edge.policy_ref].kind == "loop"
                            else "max_generations"
                        ]
                    )
                ), None)
                if exhausted is not None:
                    error_edges = [
                        edge for edge in outgoing
                        if edge.route == "error" and evaluate_condition(
                            edge.condition,
                            output,
                            workflow_inputs=state["workflow_inputs"],
                        )
                    ]
                    if not error_edges:
                        raise ValueError(
                            f"{exhausted.kind} policy {exhausted.id!r} exhausted "
                            "without a selected error route"
                        )
                    route_name = "error"
            run_id = str(config.get("configurable", {}).get("thread_id", ""))
            if run_id:
                outgoing = outgoing_edges(ir, current)
                selected_for_route = [
                    edge for edge in outgoing
                    if edge.route == route_name and evaluate_condition(
                        edge.condition,
                        output,
                        workflow_inputs=state["workflow_inputs"],
                    )
                ]
                if (current.route_mode or "exclusive") != "parallel":
                    selected_for_route = selected_for_route[:1]
                for join_id, incoming, threshold in early_joins_by_source.get(
                    current.id, ()
                ):
                    sources = dict.fromkeys(source for _edge, source in incoming)
                    registry.settle_join_source(
                        run_id,
                        join_id,
                        state.get("execution_order", ()).count(current.id) + 1,
                        incoming,
                        current.id,
                        frozenset(edge.id for edge in selected_for_route),
                        threshold,
                        {
                            node_id: (
                                f"langgraph_attempt:{run_id}:{node_id}:"
                                + str(
                                    state.get("execution_order", ()).count(node_id)
                                    + 1
                                )
                            )
                            for node_id in sources
                            if nodes_by_id[node_id].handler is not None
                        },
                    )
            return {
                "node_outputs": {current.id: output},
                "node_routes": {current.id: route_name},
                "execution_order": (current.id,),
            }

        builder.add_node(node.id, execute)

    # A conditional router runs once per source branch and sees that branch's
    # pending write, not the writes of its siblings in the same superstep.
    # That is enough for `any` (and n=1), but every branch of an n>1 join used
    # to observe an arrival count of one and hold the join forever.  Route
    # those joins through a tiny barrier node: LangGraph coalesces the barrier
    # scheduled by parallel siblings, and its following router sees the
    # superstep's merged state.  If arrivals span supersteps the barrier may
    # run again, which is exactly when readiness needs to be reconsidered.
    join_gates: dict[str, str] = {}
    used_node_ids = set(nodes_by_id)
    for join in (item for item in ir.nodes if item.kind == "join"):
        policy = next((
            policies_by_id[item] for item in join.policies
            if item in policies_by_id and policies_by_id[item].kind == "join"
        ), None)
        if (
            policy is None or policy.config.get("mode") != "n_of_m"
            or int(policy.config["threshold"]) <= 1
        ):
            continue
        gate = f"__orbit_n_of_m_gate__{join.id}"
        while gate in used_node_ids:
            gate += "_"
        used_node_ids.add(gate)
        join_gates[join.id] = gate
        builder.add_node(gate, lambda _state: {})

        def gate_route(state: _GraphState, *, target=join):
            return [target.id] if _join_is_ready(
                ir, target, state, (), policies_by_id,
            ) else []

        builder.add_conditional_edges(gate, gate_route, [join.id])

    for entry in ir.entry:
        builder.add_edge(START, entry)

    for node in ir.nodes:
        outgoing = outgoing_edges(ir, node)
        if not outgoing:
            builder.add_edge(node.id, END)
            continue

        def route(state: _GraphState, *, current=node, edges=outgoing):
            source = state["node_outputs"][current.id]
            selected_edges = [
                edge
                for edge in edges
                if edge.route == state["node_routes"].get(current.id, "success")
                and evaluate_condition(
                    edge.condition,
                    source,
                    workflow_inputs=state["workflow_inputs"],
                )
            ]
            mode = current.route_mode or "exclusive"
            selected_edges = (
                selected_edges if mode == "parallel" else selected_edges[:1]
            )
            for edge in selected_edges:
                if not edge.back_edge:
                    continue
                policy = policies_by_id.get(edge.policy_ref or "")
                if policy is None:
                    raise LangGraphCompileError(
                        f"back edge {edge.id!r} requires a loop policy"
                    )
                limit_field = (
                    "max_iterations" if policy.kind == "loop"
                    else "max_generations"
                )
                if state.get("execution_order", ()).count(current.id) > int(
                    policy.config[limit_field]
                ):
                    raise ValueError(
                        f"{policy.kind} policy {policy.id!r} exceeded {limit_field}"
                    )
            targets = [edge.target_node for edge in selected_edges]
            executed = state.get("node_outputs") or {}

            def ready(node_id: str, frontier: Sequence[str]) -> bool:
                if node_id in join_gates:
                    return True
                target = nodes_by_id.get(node_id)
                if target is None or target.kind != "join":
                    return True
                return _join_is_ready(ir, target, state, frontier, policies_by_id)

            # A join this node reaches but that is still waiting is left for
            # whichever branch completes last. Dropping it is only half the
            # rule: if that last branch routes elsewhere, nobody would ever
            # schedule the join, so a join that has *become* ready is added
            # even when this node does not point at it.
            held = [node_id for node_id in targets if not ready(node_id, targets)]
            scheduled = [node_id for node_id in targets if node_id not in held]
            for candidate in ir.nodes:
                if candidate.kind != "join" or candidate.id in scheduled:
                    continue
                if candidate.id in executed or candidate.id in held:
                    continue
                if not any(
                    edge.target_node == candidate.id
                    and edge_is_selected(edge, state) is True
                    for edge in ir.edges
                ):
                    continue
                if ready(candidate.id, scheduled):
                    scheduled.append(candidate.id)
            return list(dict.fromkeys(
                join_gates.get(node_id, node_id) for node_id in scheduled
            ))

        # Every join is a possible target, not only the ones this node points
        # at: a router schedules a join that has become ready so that a branch
        # routing elsewhere cannot strand it.
        builder.add_conditional_edges(
            node.id,
            route,
            sorted(
                {edge.target_node for edge in outgoing}
                | {item.id for item in ir.nodes if item.kind == "join"}
                | set(join_gates.values())
            ),
        )

    return CompiledLangGraphWorkflow(
        ir, builder.compile(checkpointer=checkpointer)
    )


def compile_generated_workflow(
    source: str,
    handler_catalog: Any,
    schema_catalog: Any,
    runtime_registry: LangGraphHandlerRegistry,
    *,
    source_name: str = "<agent-generated>",
    source_format: str = "json",
    extension_registry: Any = None,
    checkpointer: Any = None,
) -> CompiledLangGraphWorkflow:
    """Validate Agent-authored DSL and compile only its trusted Handler refs.

    This is the intended public entry point for the generation pipeline.  A
    caller cannot skip Orbit's structural/semantic compiler and hand arbitrary
    Python to LangGraph.
    """

    compiled = compile_source(
        source,
        handler_catalog,
        schema_catalog,
        source_name=source_name,
        source_format=source_format,
        extensions=extension_registry,
    )
    return compile_workflow(
        compiled.ir, runtime_registry, checkpointer=checkpointer
    )
