"""Trusted discovery of locally installed Agent CLIs.

Discovery answers exactly one question: *which* of a fixed, code-owned set of
Agent CLIs is installed on this machine. It never answers "what command should
we run" — that lives in the spec, in this file, under review.

The rule the whole design hangs on: a workflow author, the UI and the Planner
can select an Agent by name, and nothing else. They cannot supply an
executable, an argument, a path or an environment variable. So a compromised
plan or a prompt-injected Planner can at worst pick a different trusted CLI;
it can never turn a node into arbitrary shell execution.

Detection scope follows main: a CLI counts as installed when it resolves on
PATH, and each Hermes profile is its own agent. The version probe pins the
CLI's version when it succeeds; a CLI whose version cannot be established is
still detected, but only a version-pinned agent may be registered — an
unpinned version would make the manifest fingerprint a lie.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Callable, Iterable, Mapping, Sequence

from ..cli_environment import trusted_cli_environment
from ..domain.durable_execution import ExecutionSafety
from ..handlers.agent import AGENT_RESULT_PORT
from ..domain.handlers import ResourceProfile
from .handlers import HandlerManifest


VERSION_PROBE_TIMEOUT_SECONDS = 10
_VERSION_PATTERN = re.compile(r"(\d+\.\d+(?:\.\d+)?)")
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class AgentDiscoveryError(ValueError):
    """A spec or a probe result that must not become a registered handler."""


_SAFE_ARG = re.compile(r"^-{0,2}[A-Za-z0-9][A-Za-z0-9._:@/=-]*$")


@dataclass(frozen=True)
class AgentInvocation:
    """How one CLI is asked a question, decided here and nowhere else.

    `args` is the fixed argv tail — subcommand and flags — that this file
    commits to. The prompt is data and rides in exactly one of three ways:
    stdin (preferred, invisible to the process list), the value of a flag, or
    a positional after `--`. Whichever it is, argv is built as a list without a
    shell, so the prompt can never become a command.
    """

    args: tuple[str, ...] = ()
    prompt_flag: str | None = None
    prompt_positional: bool = False

    def __post_init__(self) -> None:
        for argument in self.args:
            if not _SAFE_ARG.match(argument):
                raise AgentDiscoveryError(f"unsafe agent argument: {argument!r}")
        if self.prompt_flag is not None and self.prompt_positional:
            raise AgentDiscoveryError("a prompt is passed one way: flag or positional")
        if self.prompt_flag is not None and not self.prompt_flag.startswith("-"):
            raise AgentDiscoveryError(
                f"prompt flag must be a flag, got {self.prompt_flag!r}"
            )


@dataclass(frozen=True)
class AgentCliSpec:
    """One trusted Agent CLI. Only this file may construct the allowlist.

    `executable` is a bare program name on purpose: it is resolved through
    PATH, and a spec carrying a directory would be a way to smuggle in a
    location the reviewer of this file never saw.
    """

    name: str
    executable: str
    version_args: tuple[str, ...] = ("--version",)
    node_kinds: tuple[str, ...] = ("action",)
    capabilities: tuple[str, ...] = ("agent.invoke",)
    required_secrets: tuple[str, ...] = ()
    max_duration_seconds: int = 1800
    cost_class: str = "agent-cli"
    # How to invoke it. None means the CLI is detected but has no reviewed
    # invocation yet, so it can be listed and never registered — detection and
    # execution compatibility are separate facts.
    invocation: AgentInvocation | None = None

    @property
    def runtime_compatible(self) -> bool:
        return self.invocation is not None

    def __post_init__(self) -> None:
        if not _SAFE_NAME.match(self.name):
            raise AgentDiscoveryError(f"unsafe agent name: {self.name!r}")
        if not _SAFE_NAME.match(self.executable):
            raise AgentDiscoveryError(
                f"executable must be a bare program name, got {self.executable!r}"
            )
        for argument in self.version_args:
            if not argument.startswith("-"):
                raise AgentDiscoveryError(
                    f"version probe takes flags only, got {argument!r}"
                )


# The allowlist. Adding an entry is a code change and a code review; there is
# deliberately no config file, environment variable or API that extends it.
# Every invocation below was probed against the installed CLI rather than read
# off its help text: the argv is what actually produced a reply on stdout.
# `--skip-git-repo-check` and `--skip-trust` waive the CLI's *directory* trust
# check only; neither waives its tool-permission prompts.
TRUSTED_AGENT_CLIS: tuple[AgentCliSpec, ...] = (
    AgentCliSpec("claude", "claude", invocation=AgentInvocation(prompt_flag="-p")),
    AgentCliSpec("codex", "codex", invocation=AgentInvocation(
        args=("exec", "--skip-git-repo-check"), prompt_positional=True,
    )),
    AgentCliSpec("gemini", "gemini", invocation=AgentInvocation(
        args=("--skip-trust",), prompt_flag="-p",
    )),
    AgentCliSpec("antigravity", "agy", invocation=AgentInvocation(prompt_flag="-p")),
    AgentCliSpec("hermes", "hermes", invocation=AgentInvocation(
        # -Q is quiet mode: the final response only, no banner or spinner.
        args=("chat", "-Q"), prompt_flag="-q",
    )),
    # The only one that reads the prompt from stdin, so the only one whose
    # prompt never appears in the process list.
    AgentCliSpec("opencode", "opencode", invocation=AgentInvocation(args=("run",))),
)


@dataclass(frozen=True)
class DiscoveredAgent:
    """A trusted CLI that is actually installed.

    `version` is the pinned CLI version, or None when the version probe could
    not establish one. Detection reports it either way (main's rule); only a
    version-pinned agent may become a registered handler.
    """

    spec: AgentCliSpec
    executable_path: str
    version: str | None

    @property
    def name(self) -> str:
        return self.spec.name


_PROFILE_SLUG = re.compile(r"[^a-z0-9_-]+")


def _profile_slug(name: str) -> str:
    slug = _PROFILE_SLUG.sub("-", name.strip().lower()).strip("-")
    return slug or "profile"


def _hermes_profile_specs(
    spec: AgentCliSpec, profile_root: Path
) -> tuple[AgentCliSpec, ...]:
    """Each Hermes profile as its own agent, same as main's detection.

    Two different names are at work. The *agent* name is slugged into the safe
    space the spec constructor enforces, because it is an identifier workflows
    bind to. The *profile* is the directory name exactly as Hermes knows it,
    because that is what `--profile` has to receive — a slug would name a
    profile that does not exist. It is a top-level flag, so it goes ahead of
    the subcommand, and argv is a list with no shell anywhere.

    Choosing an agent must actually choose that agent: without this the four
    Hermes profiles were four names for one CLI run against whichever profile
    happened to be the sticky default.

    A directory whose name cannot be passed as an argument is detected and
    left unregistrable, the same answer this file already gives for a CLI it
    has no reviewed invocation for.
    """

    try:
        children = sorted(profile_root.iterdir(), key=lambda path: path.name.lower())
    except OSError:
        return ()
    specs: list[AgentCliSpec] = []
    used = {spec.name}
    for child in children:
        if not child.is_dir() or child.name.startswith("."):
            continue
        base = f"hermes-{_profile_slug(child.name)}"[:32]
        name = base
        counter = 2
        while name in used:
            suffix = f"-{counter}"
            name = f"{base[:32 - len(suffix)]}{suffix}"
            counter += 1
        used.add(name)
        invocation = spec.invocation
        if invocation is not None:
            try:
                invocation = replace(
                    invocation, args=("--profile", child.name, *invocation.args),
                )
            except AgentDiscoveryError:
                invocation = None
        specs.append(replace(spec, name=name, invocation=invocation))
    return tuple(specs)


def _probe_version(
    executable_path: str, spec: AgentCliSpec, runner
) -> str | None:
    try:
        completed = runner(
            [executable_path, *spec.version_args],
            capture_output=True, text=True,
            timeout=VERSION_PROBE_TIMEOUT_SECONDS,
            # A version probe has no business reading the project or inheriting
            # credentials, so it runs from a neutral cwd with a bare env.
            cwd=os.path.expanduser("~"),
            env=trusted_cli_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    match = _VERSION_PATTERN.search(f"{completed.stdout}\n{completed.stderr}")
    return match.group(1) if match else None


def discover_agent_clis(
    specs: Sequence[AgentCliSpec] = TRUSTED_AGENT_CLIS,
    *,
    which: Callable[[str], str | None] = shutil.which,
    runner=subprocess.run,
    profile_root: Path | None = None,
) -> tuple[DiscoveredAgent, ...]:
    """Which trusted CLIs are installed here. Silent about the ones that aren't.

    Detection follows main: a CLI on PATH counts as installed even when its
    version cannot be established (the probe still runs — a pinned version is
    what makes an agent registrable). An installed Hermes additionally yields
    one agent per profile under ``~/.hermes/profiles``.
    """

    hermes_profiles = profile_root or (Path.home() / ".hermes" / "profiles")
    found: list[DiscoveredAgent] = []
    for spec in specs:
        resolved = which(spec.executable)
        if not resolved:
            continue
        version = _probe_version(resolved, spec, runner)
        path = str(Path(resolved))
        found.append(DiscoveredAgent(spec, path, version))
        if spec.name == "hermes":
            for profile_spec in _hermes_profile_specs(spec, hermes_profiles):
                found.append(DiscoveredAgent(profile_spec, path, version))
    return tuple(found)


def agent_manifest(
    agent: DiscoveredAgent,
    *,
    input_schema_id: str = "schema://object/1.0",
    result_schema_id: str = "schema://object/1.0",
) -> HandlerManifest:
    """The immutable manifest a discovered Agent is registered under.

    UNKNOWN_ON_LEASE_LOSS, not REPLAY_SAFE: an Agent CLI has already talked to
    the outside world by the time we lose its lease, so re-running it is a
    second real invocation and the runtime must treat the first result as
    unknown rather than assume it never happened.
    """

    if agent.version is None:
        raise AgentDiscoveryError(
            f"agent {agent.name!r} has no pinned version; a manifest fingerprint "
            "built on an unknown version would be a lie"
        )
    return HandlerManifest(
        f"agent.{agent.name}",
        # The CLI's own version is the handler version: upgrading the CLI
        # produces a different fingerprint, which is what makes a published
        # plan refuse to silently run on a different agent build.
        agent.version if agent.version.count(".") == 2 else f"{agent.version}.0",
        agent.spec.node_kinds,
        {"prompt": input_schema_id},
        # The port the prompt client fills. Naming it in one place keeps the
        # manifest a workflow binds to and the answer a client returns from
        # drifting apart.
        {AGENT_RESULT_PORT: result_schema_id},
        {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "timeout_seconds": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": False,
        },
        ExecutionSafety.UNKNOWN_ON_LEASE_LOSS,
        ResourceProfile(0, 0, 0, agent.spec.max_duration_seconds, 0, agent.spec.cost_class),
        result_schema_id,
        agent.spec.capabilities,
        agent.spec.required_secrets,
        True,
        False,
    )


def registrable_agents(
    agents: Iterable[DiscoveredAgent],
    *,
    allowed_capabilities: Sequence[str] | None = None,
) -> tuple[tuple[DiscoveredAgent, HandlerManifest], ...]:
    """Discovery result filtered through capability policy, ready to register.

    Policy runs here rather than at execution time so that a capability the
    deployment has not granted never reaches the sealed registry at all. An
    agent whose version could not be pinned is detected but stops here too:
    the manifest fingerprint covers the CLI version, so registering it would
    make the fingerprint a lie.
    """

    permitted = None if allowed_capabilities is None else set(allowed_capabilities)
    pairs = []
    for agent in agents:
        if agent.version is None:
            continue
        if not agent.spec.runtime_compatible:
            continue
        if permitted is not None and not permitted.issuperset(agent.spec.capabilities):
            continue
        pairs.append((agent, agent_manifest(agent)))
    return tuple(pairs)


def catalog_entries(agents: Iterable[DiscoveredAgent]) -> tuple[Mapping[str, object], ...]:
    """What `/api/v1/handler-catalog` may say about a discovered Agent.

    Name, version and capabilities only. The resolved executable path stays
    server-side: exposing it would hand a caller the one piece of information
    the "no arbitrary command" rule is built to withhold.
    """

    return tuple(
        {
            "name": f"agent.{agent.name}",
            "agent": agent.name,
            "version": agent.version,
            "capabilities": list(agent.spec.capabilities),
        }
        for agent in agents
    )
