"""Where an Agent step goes when the Agent it names is not on this machine.

A published Workflow pins the exact Handler build it was compiled against,
and for an Agent that build is its CLI version — so a Workflow written on
another machine, or one whose CLI has since been upgraded, names something
that is not here. The Agent it names is honoured wherever it exists; only a
step with nowhere to go is carried elsewhere, and as little as possible: to
the same Agent's installed build where there is one, and only failing that to
whichever Agent this Runtime is talking to.

The definition is never rewritten. A substitution produces a per-Run graph
stored beside the run, so the published version keeps saying what its author
published and the run keeps saying what actually executed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Any, Callable, Mapping, Sequence

from .catalogs.handlers import HandlerManifest
from .domain.definitions import IRHandlerRef, IRNode, WorkflowIR

AGENT_CAPABILITY = "agent.invoke"

# An MCP client's name for itself is not always the Agent CLI's name. Only the
# ones that cannot be derived need to be here: `candidate_agent_names` already
# tries each leading stem, so `claude-code` finds `agent.claude` unaided.
CLIENT_AGENT_ALIASES = {"chatgpt": "codex", "claude-desktop": "claude"}

_SEPARATORS = re.compile(r"[^a-z0-9]+")


def candidate_agent_names(client: Any) -> tuple[str, ...]:
    """Handler names a client's own name for itself could mean, best first.

    A client says what it is at `initialize`, in whatever shape it likes:
    `Codex`, `claude-code`, `Claude Desktop`. Matching that against
    `agent.<cli>` by string equality found none of them, and the Runtime then
    fell back to "is there exactly one Agent installed" — so on a machine with
    three CLIs, an Agent that had just introduced itself was answered with
    "cannot tell which Agent to bind to".

    Case and punctuation are folded, the alias table covers the names that do
    not resemble their CLI at all, and each leading stem is tried so a client
    that appends something to its name still resolves.
    """

    token = _SEPARATORS.sub("-", str(client or "").strip().lower()).strip("-")
    if not token:
        return ()
    names: list[str] = []

    def offer(name: str) -> None:
        if name not in names:
            names.append(name)

    aliased = CLIENT_AGENT_ALIASES.get(token)
    if aliased is not None:
        offer(f"agent.{aliased}")
    parts = token.split("-")
    for size in range(len(parts), 0, -1):
        stem = "-".join(parts[:size])
        offer(f"agent.{CLIENT_AGENT_ALIASES.get(stem, stem)}")
    return tuple(names)


def recent_agent_clients(sessions=None, broker=None) -> tuple[str, ...]:
    """Client names this Runtime has heard from, most recent first.

    The MCP session registry leads because it answers the question actually
    being asked — who is talking to this Runtime — and it answers it in order,
    newest first, keyed by actor. On loopback that is one row whose client
    name is whoever handshook last, which is exactly "the Agent connected
    right now" when only one connects at a time.

    The authoring broker follows as a fallback for an App that polls for
    generation work without ever speaking MCP. It is second on purpose: its
    presence window is ten minutes and it returns a *set sorted by name*, so
    an Agent swapped out ten minutes ago could outrank the one at the keyboard
    for as long as the window lasted.

    Nothing is filtered by presence. A client that has gone quiet is still the
    last Agent this process spoke to, and refusing to run a workflow because
    nobody has said anything for a minute is worse than running it on the
    Agent that was there — the sticky answer is the right one where only one
    Agent is ever connected.
    """

    names: list[str] = []
    listing = getattr(sessions, "sessions", None)
    if listing is not None:
        for session in listing():
            info = session.get("client")
            name = (info or {}).get("name") if isinstance(info, Mapping) else None
            if name and name not in names:
                names.append(name)
    if broker is not None:
        for name in broker.clients():
            if name not in names:
                names.append(name)
    return tuple(names)


class AgentRebindError(ValueError):
    """This graph's Agent nodes cannot be rebound to the Agent on offer."""


@dataclass(frozen=True)
class AgentRebinding:
    """A graph with substitutes wired in, and what each moved node now names.

    A mapping rather than one Handler: a substitution is decided per step, so
    a graph with two stranded Agents can land on two different ones.
    """

    ir: WorkflowIR
    rebound: Mapping[str, IRHandlerRef]

    @property
    def identity(self) -> str:
        """What ran it, short enough to put in a hash or a label."""

        return ", ".join(sorted({
            f"{reference.name}@{reference.version}"
            for reference in self.rebound.values()
        }))


def agent_manifests(
    manifests: Sequence[HandlerManifest],
) -> tuple[HandlerManifest, ...]:
    """Registered Handlers that can be an Agent step, by name."""

    return tuple(sorted(
        (item for item in manifests if AGENT_CAPABILITY in item.capabilities),
        key=lambda item: item.name,
    ))


def preferred_agent(
    manifests: Sequence[HandlerManifest], clients: Sequence[str],
) -> HandlerManifest | None:
    """Which single Agent this Runtime speaks for, or None when it is ambiguous.

    `clients` is **most recent first** — see `recent_agent_clients`. The order
    is the policy: a client names itself, and where only one Agent is ever
    connected the one that spoke last is the one at the keyboard. Ranking them
    any other way (by name, say) means an Agent that was swapped out still
    wins for as long as it lingers in a presence window.

    Failing every client, a Runtime with exactly one Agent registered has no
    choice to make. Two Agents and nothing ever heard from is genuinely
    ambiguous, and guessing there would mean a workflow silently ran on
    whichever name sorted first.
    """

    agents = agent_manifests(manifests)
    if not agents:
        return None
    by_name = {item.name: item for item in agents}
    for client in clients:
        for name in candidate_agent_names(client):
            candidate = by_name.get(name)
            if candidate is not None:
                return candidate
    return agents[0] if len(agents) == 1 else None


def is_agent_node(node: IRNode) -> bool:
    """Whether this node names an Agent, judged from the published graph alone.

    The name prefix, not the Handler's declared capability: the Agent a node
    was published against is frequently *not* installed here — that is the
    case this fallback exists to rescue — so there is no manifest to read
    a capability off. `agent_manifest` mints every Agent Handler as
    `agent.<name>`, which makes the prefix the durable signal.
    """

    return node.handler is not None and node.handler.name.startswith("agent.")


def _clamped_config(config: Any, manifest: HandlerManifest) -> Any:
    """The node's config with any budget the new Agent cannot honour lowered.

    Only the ceiling moves. Everything else a node carries — its prompt above
    all — is the author's instruction for that step, and rebinding changes who
    carries the instruction out, not what it says.
    """

    if not isinstance(config, Mapping):
        return config
    budget = config.get("timeout_seconds")
    ceiling = manifest.resource_profile.max_duration_seconds
    if not isinstance(budget, int) or isinstance(budget, bool) or budget <= ceiling:
        return config
    return {**dict(config), "timeout_seconds": ceiling}


def _check_ports(node: IRNode, manifest: HandlerManifest) -> None:
    """Refuse a substitute whose shape is not the one the graph is wired for.

    Checked rather than assumed. Every Agent Handler this Runtime mints today
    has the same one-in-one-out shape, so this never fires — which is exactly
    why it has to be here: the day an Agent arrives with a different port, the
    answer must be a refusal to start, not a graph wired to a port that does
    not exist.
    """

    ports = tuple((port.id, port.schema_id) for port in node.inputs)
    if ports != tuple(manifest.inputs.items()):
        raise AgentRebindError(
            f"node {node.id!r} has input ports {list(ports)}, which "
            f"{manifest.name} does not offer"
        )
    ports = tuple((port.id, port.schema_id) for port in node.outputs)
    if ports != tuple(manifest.outputs.items()):
        raise AgentRebindError(
            f"node {node.id!r} has output ports {list(ports)}, which "
            f"{manifest.name} does not produce"
        )


def _apply(
    ir: WorkflowIR, targets: Mapping[str, HandlerManifest],
) -> AgentRebinding | None:
    """Wire each named node to its target, or None when nothing moves."""

    references = {}
    for node in ir.nodes:
        manifest = targets.get(node.id)
        if manifest is None:
            continue
        _check_ports(node, manifest)
        reference = IRHandlerRef(
            manifest.name, manifest.version, manifest.fingerprint,
        )
        if (
            node.handler != reference
            or _clamped_config(node.config, manifest) != node.config
        ):
            references[node.id] = reference
    if not references:
        return None
    return AgentRebinding(
        replace(ir, nodes=tuple(
            replace(
                node, handler=references[node.id],
                config=_clamped_config(node.config, targets[node.id]),
            )
            if node.id in references else node
            for node in ir.nodes
        )),
        references,
    )


class AgentFallback:
    """A substitute for an Agent step whose Handler is not on this machine.

    Agent CLI releases are operational upgrades, not Workflow contract
    migrations. Availability is therefore decided by the logical Handler name:
    a locally installed ``agent.claude`` satisfies a step naming
    ``agent.claude`` regardless of the CLI version or manifest fingerprint the
    published Workflow recorded.

    So a step whose Handler is *here* runs on it, exactly as published: this
    changes nothing about a Workflow that names Agents this Runtime has, and
    a graph that deliberately uses two Agents keeps using two. Only a step
    that has nowhere to go is moved, and it is moved as little as possible —
    to the same Agent's installed build where there is one, because a CLI
    release is an operational upgrade rather than a change of Agent, and only
    failing that to whichever Agent this Runtime is talking to.

    Late-bound on purpose: which Agent is current depends on who is connected
    right now, and the engine is built once at startup.
    """

    def __init__(
        self,
        manifests: Callable[[], Sequence[HandlerManifest]] | Sequence[HandlerManifest],
        connected_clients: Callable[[], Sequence[str]] | None = None,
    ) -> None:
        self._manifests = (
            manifests if callable(manifests) else (lambda: tuple(manifests))
        )
        self._clients = connected_clients or (lambda: ())

    def current(self) -> HandlerManifest | None:
        """The Agent this Runtime is talking to, for a step with nowhere else."""

        return preferred_agent(tuple(self._manifests()), tuple(self._clients()))

    def ambiguous(self) -> bool:
        """Agents are installed, and nothing says which one this Runtime uses.

        Worth telling a reader apart from "no Agent installed": connecting an
        Agent App resolves this one and cannot resolve the other.
        """

        return (
            bool(agent_manifests(tuple(self._manifests())))
            and self.current() is None
        )

    def target_for(self, name: str) -> HandlerManifest | None:
        """What a step naming `name` would run on, or None for nothing to offer."""

        registered = agent_manifests(tuple(self._manifests()))
        same = next(
            (item for item in registered if item.name == name), None,
        )
        return same if same is not None else self.current()

    def __call__(self, ir: WorkflowIR) -> AgentRebinding | None:
        manifests = tuple(self._manifests())
        # ExecutionRegistry resolves Agent handlers by logical name when the
        # published CLI build is no longer installed. Keep this availability
        # check aligned with it so a routine CLI upgrade neither emits a
        # misleading fallback warning nor rewrites the run's Agent binding.
        installed = {
            item.name for item in agent_manifests(manifests)
        }
        stranded = [
            node for node in ir.nodes
            if is_agent_node(node) and node.handler.name not in installed
        ]
        if not stranded:
            return None
        registered = {item.name: item for item in agent_manifests(manifests)}
        current, asked = None, False
        targets: dict[str, HandlerManifest] = {}
        for node in stranded:
            same = registered.get(node.handler.name)
            if same is not None:
                targets[node.id] = same
                continue
            if not asked:
                current, asked = self.current(), True
            if current is not None:
                targets[node.id] = current
        # Nothing to offer is not a refusal: the published binding stands and
        # the compiler says whether it resolves, the same answer it would give
        # if this fallback did not exist.
        return _apply(ir, targets) if targets else None
