"""Trusted discovery of locally installed Agent CLIs.

Discovery answers exactly one question: *which* of a fixed, code-owned set of
Agent CLIs is installed on this machine, and at what version. It never answers
"what command should we run" — that lives in the spec, in this file, under
review.

The rule the whole design hangs on: a workflow author, the UI and the Planner
can select an Agent by name, and nothing else. They cannot supply an
executable, an argument, a path or an environment variable. So a compromised
plan or a prompt-injected Planner can at worst pick a different trusted CLI;
it can never turn a node into arbitrary shell execution.

Discovery output is an immutable HandlerManifest whose fingerprint covers the
resolved CLI version, so a plan compiled against `claude 2.1` refuses to run
against `claude 3.0` instead of silently changing behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Callable, Iterable, Mapping, Sequence

from ..domain.durable_execution import ExecutionSafety
from ..domain.handlers import ResourceProfile
from .handlers import HandlerManifest


VERSION_PROBE_TIMEOUT_SECONDS = 10
_VERSION_PATTERN = re.compile(r"(\d+\.\d+(?:\.\d+)?)")
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class AgentDiscoveryError(ValueError):
    """A spec or a probe result that must not become a registered handler."""


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
TRUSTED_AGENT_CLIS: tuple[AgentCliSpec, ...] = (
    AgentCliSpec("claude", "claude"),
    AgentCliSpec("codex", "codex"),
    AgentCliSpec("gemini", "gemini"),
)


@dataclass(frozen=True)
class DiscoveredAgent:
    """A trusted CLI that is actually installed, at a pinned version."""

    spec: AgentCliSpec
    executable_path: str
    version: str

    @property
    def name(self) -> str:
        return self.spec.name


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
            env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")},
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
) -> tuple[DiscoveredAgent, ...]:
    """Which trusted CLIs are installed here. Silent about the ones that aren't.

    A CLI that is present but whose version cannot be established is skipped:
    an unpinned version would make the manifest fingerprint a lie.
    """

    found: list[DiscoveredAgent] = []
    for spec in specs:
        resolved = which(spec.executable)
        if not resolved:
            continue
        version = _probe_version(resolved, spec, runner)
        if version is None:
            continue
        found.append(DiscoveredAgent(spec, str(Path(resolved)), version))
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

    return HandlerManifest(
        f"agent.{agent.name}",
        # The CLI's own version is the handler version: upgrading the CLI
        # produces a different fingerprint, which is what makes a published
        # plan refuse to silently run on a different agent build.
        agent.version if agent.version.count(".") == 2 else f"{agent.version}.0",
        agent.spec.node_kinds,
        {"prompt": input_schema_id},
        {"result": result_schema_id},
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
    deployment has not granted never reaches the sealed registry at all.
    """

    permitted = None if allowed_capabilities is None else set(allowed_capabilities)
    pairs = []
    for agent in agents:
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
