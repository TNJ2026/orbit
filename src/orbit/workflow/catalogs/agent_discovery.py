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
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Callable, Iterable, Mapping, Sequence

from ..cli_environment import trusted_cli_environment
from ..domain.deadlines import MIN_AGENT_DURATION_SECONDS
from ..domain.durable_execution import ExecutionSafety
from ..handlers.agent import AGENT_RESULT_PORT
from ..domain.handlers import ResourceProfile
from .handlers import HandlerManifest


VERSION_PROBE_TIMEOUT_SECONDS = 10
AGENT_DISCOVERY_CACHE_SECONDS = 300
AGENT_DISCOVERY_FAILURE_CACHE_SECONDS = 30
DEFAULT_AGENT_DISCOVERY_CACHE = Path.home() / ".orbit" / "cache" / "agents.json"
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
    process_timeout_flag: str | None = None

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
        if (
            self.process_timeout_flag is not None
            and not self.process_timeout_flag.startswith("--")
        ):
            raise AgentDiscoveryError("process timeout flag must be a long flag")


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
#
# Tool permissions are waived here on purpose, and this is where that decision
# is reviewed. A CLI's permission prompt asks a person, and a workflow step has
# no person: the prompt is not answered, it is refused, and the CLI then prints
# its refusal as prose and exits 0. The step is recorded a success that wrote
# nothing. So each entry either gets the setting that lets an unattended run
# actually work, or it gets none because it never prompted to begin with —
# pi, hermes and opencode all write without asking.
#
# The two settings are not the same strength, and the weaker one is preferred
# where a CLI offers it. Codex has a real sandbox of its own, so it is confined
# to the directory it was given rather than let out of it; Claude and
# Antigravity have no equivalent lever, so they are trusted outright. Nothing
# here is an OS boundary — see TrustedCliAgentClient.workspace_root.
TRUSTED_AGENT_CLIS: tuple[AgentCliSpec, ...] = (
    AgentCliSpec("claude", "claude", invocation=AgentInvocation(
        args=("--dangerously-skip-permissions",), prompt_flag="-p",
    )),
    AgentCliSpec("codex", "codex", invocation=AgentInvocation(
        # workspace-write, not a full bypass: Codex keeps enforcing its own
        # sandbox and confines the run to the workspace it was handed. Under
        # the default read-only sandbox every write came back "rejected by
        # user approval settings", with nobody to ask.
        args=("exec", "--skip-git-repo-check", "--sandbox", "workspace-write"),
        prompt_positional=True,
    )),
    AgentCliSpec("gemini", "gemini", invocation=AgentInvocation(
        args=("--skip-trust",), prompt_flag="-p",
    )),
    AgentCliSpec("antigravity", "agy", invocation=AgentInvocation(
        args=("--dangerously-skip-permissions",), prompt_flag="-p",
        process_timeout_flag="--print-timeout",
    )),
    # `pi -p "<prompt>"` is non-interactive print mode, text output by default.
    # `-p` is a boolean and the prompt is a positional message, but the argv is
    # identical to the flag form, and pi rejects the `--` fence a positional
    # spec would add ("Unknown option: --"). Probed against pi 0.81.1.
    AgentCliSpec("pi", "pi", invocation=AgentInvocation(prompt_flag="-p")),
    AgentCliSpec("hermes", "hermes", invocation=AgentInvocation(
        # -Q is quiet mode: the final response only, no banner or spinner.
        args=("chat", "-Q"), prompt_flag="-q",
    )),
    # The only one that reads the prompt from stdin, so the only one whose
    # prompt never appears in the process list.
    AgentCliSpec("opencode", "opencode", invocation=AgentInvocation(args=("run",))),
    # Proposed from a system probe: installed here, reporting version
    # 0.37.2.
    AgentCliSpec("kimi", "kimi", invocation=AgentInvocation(prompt_flag="-p")),
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


def _installed_candidates(
    specs: Sequence[AgentCliSpec],
    which: Callable[[str], str | None],
    profile_root: Path | None,
):
    """Every trusted CLI this PATH offers, with its profiles already expanded.

    Shared so that "what is installed here" has one answer. The two discovery
    entry points differ in how they get a version — one always probes, one
    consults a machine-wide cache first — and nothing else; when they each
    walked PATH themselves the two walks drifted apart, and the cached one
    silently started probing once per Hermes profile.

    `identity` is what a cache keys on: a path is not evidence that the file
    behind it is the one that was probed. It is None when the executable
    cannot be stat'd, which the caller reads as "do not trust a cached row".
    """

    hermes_profiles = profile_root or (Path.home() / ".hermes" / "profiles")
    for base_spec in specs:
        resolved = which(base_spec.executable)
        if not resolved:
            continue
        executable = str(Path(resolved))
        try:
            stat = Path(executable).stat()
            identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        except OSError:
            identity = None
        candidates = (base_spec,)
        if base_spec.name == "hermes":
            candidates += _hermes_profile_specs(base_spec, hermes_profiles)
        yield executable, identity, candidates


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

    found: list[DiscoveredAgent] = []
    for executable, _identity, candidates in _installed_candidates(
        specs, which, profile_root,
    ):
        # One probe per executable: the profiles of a CLI differ in name and
        # invocation, never in `version_args`.
        version = _probe_version(executable, candidates[0], runner)
        found.extend(DiscoveredAgent(spec, executable, version) for spec in candidates)
    return tuple(found)


def discover_agent_clis_cached(
    specs: Sequence[AgentCliSpec] = TRUSTED_AGENT_CLIS,
    *,
    cache_path: Path | str = DEFAULT_AGENT_DISCOVERY_CACHE,
    max_age_seconds: int = AGENT_DISCOVERY_CACHE_SECONDS,
    which: Callable[[str], str | None] = shutil.which,
    runner=subprocess.run,
    profile_root: Path | None = None,
    now: Callable[[], float] | None = None,
) -> tuple[DiscoveredAgent, ...]:
    """Reuse machine-wide version probes while revalidating each Runtime's PATH.

    The cache is only evidence that one reviewed executable reported a version
    recently. Every Runtime still resolves the code-owned allowlist on its own
    PATH and reconstructs its own Handler registrations and policy boundary.
    """

    clock = now or time.time
    path = Path(cache_path).expanduser()
    cached: dict[
        tuple[str, str, tuple[int, int, int, int] | None], tuple[str | None, float]
    ] = {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload.get("agents", ()):
            if not isinstance(item, Mapping):
                continue
            name, executable = item.get("name"), item.get("executable_path")
            version = item.get("version")
            probed_at = item.get("probed_at")
            raw_identity = item.get("identity")
            identity = (
                tuple(raw_identity) if isinstance(raw_identity, list)
                and len(raw_identity) == 4
                and all(isinstance(value, int) for value in raw_identity)
                else None
            )
            if (
                isinstance(name, str) and isinstance(executable, str)
                and (version is None or isinstance(version, str))
                and isinstance(probed_at, (int, float))
                and not isinstance(probed_at, bool)
            ):
                cached[(name, executable, identity)] = (version, float(probed_at))
    except (OSError, ValueError, TypeError):
        pass

    found: list[DiscoveredAgent] = []
    cache_rows: list[dict[str, object]] = []
    observed_at = clock()
    for executable, identity, candidates in _installed_candidates(
        specs, which, profile_root,
    ):
        # A probe runs `[executable, *spec.version_args]` and nothing else, and
        # a profile spec rewrites only the name and the invocation — so every
        # profile of one CLI asks the identical question. Twenty Hermes
        # profiles meant twenty-one identical `hermes --version` runs on any
        # start that missed the cache, each able to spend the full probe
        # timeout. Asked once per distinct command, as `discover_agent_clis`
        # has always done.
        probed_versions: dict[tuple[str, ...], str | None] = {}
        for spec in candidates:
            key = (spec.name, executable, identity)
            prior = cached.get(key)
            # A failed probe is cached too — one timeout under load must not be
            # re-paid on every start — but for much less time than a successful
            # one, and never for longer than the caller agreed to tolerate:
            # `max_age_seconds=0` means "ask now", including about an Agent
            # that was missing a moment ago.
            ttl = (
                min(max_age_seconds, AGENT_DISCOVERY_FAILURE_CACHE_SECONDS)
                if prior is not None and prior[0] is None else max_age_seconds
            )
            # The lower bound is not decoration. A row written while the clock
            # was ahead — an NTP correction, a resumed VM, a home directory
            # shared with a host that disagrees — has a negative age, which
            # satisfies any TTL and pins that entry until wall-clock time
            # catches up. For a `version=None` row that leaves an Agent
            # unregistered for as long as the skew lasts, which is the
            # never-expiring failure this cache was rewritten to end.
            age = None if prior is None else observed_at - prior[1]
            if prior is not None and age is not None and 0 <= age <= ttl:
                version, probed_at = prior
            else:
                if spec.version_args not in probed_versions:
                    probed_versions[spec.version_args] = _probe_version(
                        executable, spec, runner,
                    )
                version = probed_versions[spec.version_args]
                probed_at = observed_at
            found.append(DiscoveredAgent(spec, executable, version))
            cache_rows.append({
                "name": spec.name, "executable_path": executable,
                "version": version, "identity": list(identity) if identity else None,
                "probed_at": probed_at,
            })

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps({
            "schema_version": 3,
            "written_at": observed_at,
            "written_at_iso": datetime.now(timezone.utc).isoformat(),
            "agents": cache_rows,
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass
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
                # The ceiling is a rule and the floor is not. A budget above
                # the profile is a request the Runtime never admitted this
                # handler for, so it is refused; a budget below the schedulable
                # minimum is raised to it when the attempt is scheduled.
                #
                # Refusing the small ones here instead would reject documents
                # that were valid when they were published — the field has
                # existed, and gone unread, since before it meant anything —
                # and a workflow that ran yesterday would stop compiling for a
                # value that never had an effect.
                "timeout_seconds": {
                    "type": "integer", "minimum": 1,
                    "maximum": agent.spec.max_duration_seconds,
                    "description": (
                        "How long this step may run. Values below "
                        f"{MIN_AGENT_DURATION_SECONDS}s are raised to it: a "
                        "shorter budget cannot pay for stopping the process "
                        "and settling the attempt."
                    ),
                },
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


@dataclass(frozen=True)
class CandidateProbe:
    """What this machine can be made to say about one proposed program name.

    Evidence, not a decision. It exists so a reviewer reads what the probe
    *saw* rather than what something reported to it, and so the two can never
    be confused for each other.

    The resolved path is deliberately absent, for the same reason
    `catalog_entries` withholds it: a location this file's reviewer never saw
    is the one thing the bare-program-name rule exists to keep out.
    """

    executable: str
    refused: str | None
    on_path: bool
    version: str | None
    already_trusted: str | None


def probe_executable(
    name: str,
    *,
    specs: Sequence[AgentCliSpec] = TRUSTED_AGENT_CLIS,
    which: Callable[[str], str | None] = shutil.which,
    runner=subprocess.run,
) -> CandidateProbe:
    """Look at one proposed program name. Runs its version flag and nothing else.

    Where the suggestion came from does not matter — a person typing, a model
    reading their prompt — because this refuses to act on it in every way that
    would matter. The name is held to the same bare-program-name rule the
    allowlist is, resolution goes through PATH, and the only thing executed is
    the CLI's own version flag, from a neutral cwd with a bare environment.

    Being on PATH is not being trusted, and this function registers nothing.
    Its result feeds a proposal a person reads and merges, which is the only
    way the allowlist has ever grown.
    """

    try:
        candidate = AgentCliSpec(name, name)
    except AgentDiscoveryError as exc:
        return CandidateProbe(name, str(exc), False, None, None)
    covered = next(
        (spec.name for spec in specs if spec.executable == candidate.executable),
        None,
    )
    resolved = which(candidate.executable)
    if not resolved:
        return CandidateProbe(candidate.executable, None, False, None, covered)
    return CandidateProbe(
        candidate.executable, None, True,
        _probe_version(resolved, candidate, runner), covered,
    )


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
