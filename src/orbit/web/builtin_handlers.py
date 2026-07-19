"""The trusted first-party handler set, defined exactly once.

`orbit serve`, the tests, and anything that publishes a workflow against the
production registry all read the manifests from here. That matters more than it
looks: a manifest's fingerprint is part of the compiled workflow, so a second
copy of these definitions that drifts by one field produces workflows the
running registry refuses with "handler manifest mismatch".

Only deterministic, in-process handlers belong here. Agent CLI and git tooling
arrive in M5 behind an explicit catalog; the composition root never registers
arbitrary shell or network execution.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from ..workflow.catalogs import HandlerManifest
from ..workflow.domain.durable_execution import ExecutionSafety
from ..workflow.domain.handlers import ResourceProfile
from ..workflow.catalogs.agent_discovery import registrable_agents
from ..workflow.handlers import TransformHandler
from ..workflow.handlers.agent import AgentHandler, TrustedCliAgentClient
from ..workflow.handlers.dev_tools import (
    CAPABILITY_PROCESS_RUN, CAPABILITY_WORKSPACE_READ, CAPABILITY_WORKSPACE_WRITE,
    DEFAULT_TIMEOUT_SECONDS, VerifyProfile, WorkspaceRunner, register_dev_tools,
)
from ..workflow.handlers.tools import ToolHandler, ToolRegistry
from ..workspace.git import GitWorkspaceProvider
from .app import HandlerRegistration


TRANSFORM_MANIFEST = HandlerManifest(
    "transform", "1.0.0", ("action",),
    {"value": "example://integer/1.0"}, {"value": "example://integer/1.0"},
    {"type": "object"}, ExecutionSafety.REPLAY_SAFE,
    ResourceProfile(100_000, 100_000, 0, 300, 0, "builtin"),
    "schema://object/1.0", (), (), True, True,
)

BUILTIN_SCHEMAS: Mapping[str, Any] = {
    "schema://object/1.0": {"type": "object"},
    "example://integer/1.0": {"type": "integer"},
}


def builtin_handlers() -> Sequence[HandlerRegistration]:
    return (
        HandlerRegistration(TRANSFORM_MANIFEST, TransformHandler(), "transform@1.0.0"),
    )


# -- discovered agent CLIs ---------------------------------------------------


def agent_handlers(
    agents: Sequence[Any],
    *,
    allowed_capabilities: Sequence[str] | None = None,
    timeout_seconds: int = 1800,
) -> tuple[Sequence[HandlerRegistration], tuple[str, ...]]:
    """Turn discovered agent CLIs into registrations, or nothing.

    Discovery on its own only fills a catalog: it tells the UI which CLIs
    exist. Until these registrations reach the registry *before it seals*, a
    workflow can see an installed agent and be unable to call it — which is
    the state the migration plan's M3 task 17 exists to prevent.

    The command is owned by TrustedCliAgentClient's constructor, built here
    from the executable discovery resolved. No workflow, plan or planner can
    contribute an argument to it.
    """

    registrations: list[HandlerRegistration] = []
    names: list[str] = []
    for agent, manifest in registrable_agents(
        agents, allowed_capabilities=allowed_capabilities
    ):
        registrations.append(
            HandlerRegistration(
                manifest,
                AgentHandler(
                    TrustedCliAgentClient(
                        (agent.executable_path,), timeout_seconds=timeout_seconds
                    )
                ),
                f"{manifest.name}@{manifest.version}",
            )
        )
        names.append(manifest.name)
    return tuple(registrations), tuple(names)


# -- optional development tooling -------------------------------------------
#
# Registered only when the operator asks for it. A non-development workflow
# runs on the same kernel with none of this loaded, which is the property
# Gate M5 exists to protect.

DEV_TOOL_CAPABILITIES: tuple[str, ...] = (
    CAPABILITY_WORKSPACE_READ, CAPABILITY_WORKSPACE_WRITE, CAPABILITY_PROCESS_RUN,
)

# The read-only tools and the writing one cannot share a handler manifest:
# ToolHandler refuses to run a tool whose execution safety differs from the
# handler it was invoked through, and that check is the reason a lost lease on
# `git.integrate` is treated as an unknown result rather than a free retry.
DEV_TOOL_MANIFEST = HandlerManifest(
    "dev_tool", "1.0.0", ("action",),
    {"workspace_ref": "schema://object/1.0"}, {"result": "schema://object/1.0"},
    {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string"},
            "tool_version": {"type": "string"},
        },
        "required": ["tool_name", "tool_version"],
    },
    ExecutionSafety.REPLAY_SAFE,
    ResourceProfile(0, 0, 0, DEFAULT_TIMEOUT_SECONDS, 0, "dev-tool"),
    "schema://object/1.0", DEV_TOOL_CAPABILITIES, (), True, True,
)

DEV_TOOL_WRITE_MANIFEST = HandlerManifest(
    "dev_tool_write", "1.0.0", ("action",),
    {"workspace_ref": "schema://object/1.0"}, {"result": "schema://object/1.0"},
    DEV_TOOL_MANIFEST.config_schema,
    ExecutionSafety.UNKNOWN_ON_LEASE_LOSS,
    ResourceProfile(0, 0, 0, DEFAULT_TIMEOUT_SECONDS, 0, "dev-tool"),
    "schema://object/1.0", DEV_TOOL_CAPABILITIES, (), True, True,
)


def dev_tool_handlers(
    project_root: Path | str,
    state_dir: Path | str,
    *,
    verify_profiles: Sequence[VerifyProfile],
    allowed_capabilities: Sequence[str] = DEV_TOOL_CAPABILITIES,
    environment: Mapping[str, str] | None = None,
) -> tuple[Sequence[HandlerRegistration], tuple[str, ...]]:
    """Build the dev ToolHandlers, or nothing if no capability was granted.

    Returns the registrations plus the tool names that survived policy, so the
    caller can tell an operator what is actually available instead of leaving
    them to discover it from a failing run.
    """

    registry = ToolRegistry()
    runner = WorkspaceRunner(
        GitWorkspaceProvider(project_root, state_dir), environment=environment
    )
    names = register_dev_tools(
        registry, runner, verify_profiles=verify_profiles,
        allowed_capabilities=allowed_capabilities,
    )
    registry.seal()
    if not names:
        return (), ()

    registrations = [
        HandlerRegistration(DEV_TOOL_MANIFEST, ToolHandler(registry), "dev_tool@1.0.0")
    ]
    if "git.integrate" in names:
        registrations.append(
            HandlerRegistration(
                DEV_TOOL_WRITE_MANIFEST, ToolHandler(registry), "dev_tool_write@1.0.0"
            )
        )
    return tuple(registrations), names
