"""Bind every Agent node in a graph to one Agent, for single-Agent mode.

Single-Agent mode is a runtime binding policy, not a second catalog. The same
published Workflow runs in both modes; in single-Agent mode every node that
names an Agent is rebound to the one Agent this Runtime speaks for, and the
Workflow's own choice of Agent is ignored.

The definition is never rewritten. Rebinding produces a per-Run graph that is
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
    """A graph rebound to one Agent, and which nodes that moved."""

    ir: WorkflowIR
    handler: IRHandlerRef
    rebound: tuple[str, ...]

    @property
    def identity(self) -> str:
        """What the run was bound to, short enough to put in a hash or a label."""

        return f"{self.handler.name}@{self.handler.version}"


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
    case single-Agent mode exists to rescue — so there is no manifest to read
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


def rebind_agents(
    ir: WorkflowIR, manifest: HandlerManifest,
) -> AgentRebinding | None:
    """Point every Agent node at `manifest`, or None when none of them move.

    Ports are checked rather than assumed. Every Agent Handler this Runtime
    mints today has the same one-in-one-out shape, so the check never fires —
    which is exactly why it has to be here: the day an Agent arrives with a
    different port, the answer must be a refusal to start, not a graph wired
    to a port that does not exist.
    """

    nodes = tuple(node for node in ir.nodes if is_agent_node(node))
    if not nodes:
        return None
    expected_inputs = tuple(manifest.inputs.items())
    expected_outputs = tuple(manifest.outputs.items())
    for node in nodes:
        ports = tuple((port.id, port.schema_id) for port in node.inputs)
        if ports != expected_inputs:
            raise AgentRebindError(
                f"node {node.id!r} has input ports {list(ports)}, which "
                f"{manifest.name} does not offer"
            )
        ports = tuple((port.id, port.schema_id) for port in node.outputs)
        if ports != expected_outputs:
            raise AgentRebindError(
                f"node {node.id!r} has output ports {list(ports)}, which "
                f"{manifest.name} does not produce"
            )
    reference = IRHandlerRef(manifest.name, manifest.version, manifest.fingerprint)
    moved = tuple(
        node.id for node in nodes
        if node.handler != reference
        or _clamped_config(node.config, manifest) != node.config
    )
    if not moved:
        return None
    identifiers = frozenset(node.id for node in nodes)
    return AgentRebinding(
        replace(ir, nodes=tuple(
            replace(
                node, handler=reference,
                config=_clamped_config(node.config, manifest),
            )
            if node.id in identifiers else node
            for node in ir.nodes
        )),
        reference,
        moved,
    )


class SingleAgentBinder:
    """The single-Agent start policy, as one callable the engine can hold.

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
        """The Agent every Agent node would be rebound to, if there is one."""

        return preferred_agent(tuple(self._manifests()), tuple(self._clients()))

    def __call__(self, ir: WorkflowIR) -> AgentRebinding | None:
        if not any(is_agent_node(node) for node in ir.nodes):
            # A Workflow with no Agent step runs identically in both modes, so
            # it must not need an Agent to be named before it can start. Asked
            # before the Agent is resolved, not after: a Runtime with no Agent
            # installed could otherwise not run a graph that never wanted one.
            return None
        manifest = self.current()
        if manifest is None:
            agents = agent_manifests(tuple(self._manifests()))
            if not agents:
                raise AgentRebindError(
                    "single-Agent mode has no Agent to bind to: no Agent "
                    "Handler is registered"
                )
            raise AgentRebindError(
                "single-Agent mode cannot tell which Agent to bind to: "
                + ", ".join(item.name for item in agents)
                + " are registered and no Agent App has introduced itself"
            )
        return rebind_agents(ir, manifest)
