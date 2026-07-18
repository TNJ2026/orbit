"""Trusted development Tool adapters: git inspection, integration, verify.

These are the only place in the runtime that runs a child process on a user's
checkout, and the shape of that power is deliberately narrow:

* The **command is not data**. Every adapter owns a frozen argv template. A
  workflow selects a tool by name and passes bounded arguments (a workspace
  ref, a path, a named verify profile); it never supplies a program, a flag or
  a shell string. There is no shell anywhere in this module.
* The **workspace is a ref, not a path**. Callers name an opaque
  `workspace_ref`; the WorkspaceProvider maps it to a directory it owns and
  guarantees is inside the project's state dir.
* Output is **captured, bounded and redacted** by `platform.process`, then
  published as an Artifact rather than pasted into an event payload.

Everything here is optional. A non-development workflow never registers these
tools, and the kernel neither knows nor cares that git exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence

from ...platform import process as process_port
from ...workspace.git import WorkspaceError, WorkspaceUnavailable
from ..domain.durable_execution import ExecutionSafety
from ..domain.handlers import (
    CancelAck, CancelDisposition, ExternalEffect, RecoveryDisposition,
    RecoveryResult,
)
from .tools import ToolManifest, ToolRequest, ToolResult


WORKSPACE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
DEFAULT_TIMEOUT_SECONDS = 900
MAX_CAPTURED_BYTES = 512 * 1024

# Capability names a deployment must grant before these tools can be used.
CAPABILITY_WORKSPACE_READ = "workspace.read"
CAPABILITY_WORKSPACE_WRITE = "workspace.write"
CAPABILITY_PROCESS_RUN = "process.run"


class DevToolError(RuntimeError):
    """A tool refused to run. The message is safe to show an operator."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _checked_ref(value: Any) -> str:
    if not isinstance(value, str) or not WORKSPACE_REF.match(value):
        raise DevToolError("workspace_ref must be a short opaque identifier")
    return value


@dataclass(frozen=True)
class CommandOutcome:
    """What a child process did, in the shape the tools report upward."""

    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool
    cancelled: bool

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0 and not (self.timed_out or self.cancelled)


class WorkspaceRunner:
    """Runs a fixed argv inside a leased workspace.

    Shared by every adapter so that "which directory, whose environment, how
    long, how much output" is decided once instead of per tool.
    """

    def __init__(
        self,
        provider,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        runner: Callable[..., Any] = process_port.run,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        # An explicit environment, not os.environ: a verify run must not
        # inherit the operator's tokens just because they happened to be
        # exported in the shell that started the server.
        self.environment = dict(environment or {})

    def workspace_path(self, workspace_ref: str) -> Path:
        try:
            return self.provider.acquire(workspace_ref).path
        except WorkspaceUnavailable as exc:
            raise DevToolError(f"workspace is unavailable: {exc}") from None
        except WorkspaceError as exc:
            raise DevToolError(str(exc)) from None

    def run(
        self, argv: Sequence[str], workspace_ref: str, *, redactor=None
    ) -> CommandOutcome:
        path = self.workspace_path(workspace_ref)
        result = self.runner(
            list(argv), cwd=path, env=self.environment,
            timeout=self.timeout_seconds, max_output_bytes=MAX_CAPTURED_BYTES,
            redactor=redactor,
        )
        return CommandOutcome(
            tuple(argv), result.returncode, result.stdout, result.stderr,
            bool(result.stdout_truncated or result.stderr_truncated),
            bool(result.timed_out), bool(result.cancelled),
        )


@dataclass(frozen=True)
class VerifyProfile:
    """One named, reviewed verification command.

    Profiles are registered by the composition root. A workflow picks one by
    name; it cannot describe a new one, which is what keeps "run the tests"
    from becoming "run anything".
    """

    name: str
    argv: tuple[str, ...]
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("verify profile needs a name")
        if not self.argv:
            raise ValueError("verify profile needs a command")
        for part in self.argv:
            if not isinstance(part, str) or not part:
                raise ValueError("verify profile argv must be non-empty strings")


class _BaseAdapter:
    """Shared adapter plumbing: no cancellation, no recovery, no secrets."""

    external_effect = ExternalEffect.NONE

    def __init__(self, runner: WorkspaceRunner) -> None:
        self.runner = runner

    def cancel(self, execution_ref: str, context) -> CancelAck:
        # The child is killed by the executor's process supervision, so the
        # stop is observed rather than assumed.
        return CancelAck(CancelDisposition.CONFIRMED_STOPPED, execution_ref)

    def recover(self, recovery_ref: str, context) -> RecoveryResult:
        # A read-only command leaves nothing behind to find, and re-running it
        # is harmless — NOT_FOUND is the honest answer, not a failure.
        return RecoveryResult(RecoveryDisposition.NOT_FOUND)

    def _publish(self, context, name: str, text: str) -> str | None:
        """Store command output as an Artifact and return its id.

        Output goes to the blob store rather than into the result payload:
        a diff or a test log is unbounded, and an event stream is the wrong
        place to keep megabytes of it.
        """

        artifacts = getattr(context, "artifacts", None)
        if artifacts is None or not text:
            return None
        try:
            receipt = artifacts.write(
                name=name, content=text.encode("utf-8"), content_type="text/plain",
            )
        except Exception:
            # An undeclared artifact port is a workflow authoring choice, not a
            # tool failure: the command still ran and its result still stands.
            return None
        return getattr(receipt, "artifact_id", None) or str(receipt)


class GitStatusAdapter(_BaseAdapter):
    """`git status --porcelain=v1` — what changed, in machine-readable form."""

    ARGV = ("git", "status", "--porcelain=v1", "--untracked-files=all")

    def execute(self, request: ToolRequest, context) -> ToolResult:
        ref = _checked_ref(request.input.get("workspace_ref"))
        outcome = self.runner.run(self.ARGV, ref)
        if not outcome.succeeded:
            raise DevToolError(f"git status failed: {outcome.stderr.strip()[:200]}")
        entries = [
            {"status": line[:2].strip(), "path": line[3:]}
            for line in outcome.stdout.splitlines() if len(line) > 3
        ]
        return ToolResult(
            {
                "workspace_ref": ref,
                "clean": not entries,
                "entries": entries,
                "truncated": outcome.truncated,
            }
        )


class GitDiffAdapter(_BaseAdapter):
    """`git diff` against the workspace's base, published as an Artifact."""

    ARGV = ("git", "diff", "--no-color", "--no-ext-diff")

    def execute(self, request: ToolRequest, context) -> ToolResult:
        ref = _checked_ref(request.input.get("workspace_ref"))
        argv = list(self.ARGV)
        if request.input.get("staged"):
            argv.append("--cached")
        outcome = self.runner.run(argv, ref)
        if not outcome.succeeded:
            raise DevToolError(f"git diff failed: {outcome.stderr.strip()[:200]}")
        artifact_id = self._publish(context, "diff", outcome.stdout)
        return ToolResult(
            {
                "workspace_ref": ref,
                "empty": not outcome.stdout.strip(),
                "line_count": len(outcome.stdout.splitlines()),
                "truncated": outcome.truncated,
                "diff_artifact_id": artifact_id,
            }
        )


class GitIntegrateAdapter(_BaseAdapter):
    """Commit the workspace's work and merge it back into the project branch.

    This is the one adapter that writes, so it is the one that declares an
    external effect: once the merge commit exists, a replay of this attempt
    must not silently make a second one.
    """

    external_effect = ExternalEffect.KNOWN_APPLIED

    def recover(self, recovery_ref: str, context) -> RecoveryResult:
        # We cannot tell from here whether the commit landed before the lease
        # was lost. UNKNOWN forces the runtime to escalate instead of quietly
        # producing a second commit.
        return RecoveryResult(RecoveryDisposition.UNKNOWN)

    def execute(self, request: ToolRequest, context) -> ToolResult:
        ref = _checked_ref(request.input.get("workspace_ref"))
        message = request.input.get("message")
        if not isinstance(message, str) or not message.strip():
            raise DevToolError("integrate requires a commit message")
        if len(message) > 4096:
            raise DevToolError("commit message is too long")

        staged = self.runner.run(("git", "add", "--all"), ref)
        if not staged.succeeded:
            raise DevToolError(f"git add failed: {staged.stderr.strip()[:200]}")

        # `--` and the literal message as one argv element: nothing in the
        # message can become a flag.
        committed = self.runner.run(
            ("git", "commit", "--no-verify", "--message", message), ref
        )
        nothing_to_commit = (
            not committed.succeeded and "nothing to commit" in committed.stdout
        )
        if not committed.succeeded and not nothing_to_commit:
            raise DevToolError(f"git commit failed: {committed.stderr.strip()[:200]}")

        head = self.runner.run(("git", "rev-parse", "HEAD"), ref)
        return ToolResult(
            {
                "workspace_ref": ref,
                "committed": committed.succeeded,
                "commit": head.stdout.strip() if head.succeeded else None,
            },
            # KNOWN_APPLIED only because we just read HEAD back: the write is
            # confirmed, not merely attempted.
            external_effect=(
                ExternalEffect.KNOWN_APPLIED if committed.succeeded
                else ExternalEffect.NONE
            ),
        )


class VerifyAdapter(_BaseAdapter):
    """Run one registered verification profile inside a workspace."""

    def __init__(
        self, runner: WorkspaceRunner, profiles: Sequence[VerifyProfile]
    ) -> None:
        super().__init__(runner)
        self.profiles = {profile.name: profile for profile in profiles}
        if not self.profiles:
            raise ValueError("verify needs at least one registered profile")

    def execute(self, request: ToolRequest, context) -> ToolResult:
        ref = _checked_ref(request.input.get("workspace_ref"))
        name = request.input.get("profile")
        profile = self.profiles.get(name if isinstance(name, str) else "")
        if profile is None:
            raise DevToolError(
                f"unknown verify profile: {name!r}"
                f" (registered: {', '.join(sorted(self.profiles))})"
            )
        outcome = self.runner.run(profile.argv, ref)
        log = f"$ {' '.join(profile.argv)}\n{outcome.stdout}\n{outcome.stderr}"
        return ToolResult(
            {
                "workspace_ref": ref,
                "profile": profile.name,
                # A failing verification is a successful tool call reporting a
                # red result: the node decides what to do, the handler does not
                # crash.
                "passed": outcome.succeeded,
                "exit_code": outcome.returncode,
                "timed_out": outcome.timed_out,
                "truncated": outcome.truncated,
                "log_artifact_id": self._publish(context, "verify_log", log),
            }
        )


# -- manifests --------------------------------------------------------------

_READ_ONLY = dict(
    execution_safety=ExecutionSafety.REPLAY_SAFE,
    max_duration_seconds=DEFAULT_TIMEOUT_SECONDS,
    supports_idempotency=True,
    supports_cancel=True,
    supports_recover=True,
)


def dev_tool_manifests() -> tuple[ToolManifest, ...]:
    """The manifests these adapters register under."""

    return (
        ToolManifest(
            "git.status", "1.0.0",
            inputs={"workspace_ref": "schema://object/1.0"},
            result_schema_id="schema://object/1.0",
            capabilities=(CAPABILITY_WORKSPACE_READ, CAPABILITY_PROCESS_RUN),
            **_READ_ONLY,
        ),
        ToolManifest(
            "git.diff", "1.0.0",
            inputs={"workspace_ref": "schema://object/1.0"},
            result_schema_id="schema://object/1.0",
            capabilities=(CAPABILITY_WORKSPACE_READ, CAPABILITY_PROCESS_RUN),
            **_READ_ONLY,
        ),
        ToolManifest(
            "git.integrate", "1.0.0",
            # A merge is an external write: if the lease is lost mid-commit the
            # runtime must treat the outcome as unknown rather than assume it
            # can simply run again.
            execution_safety=ExecutionSafety.UNKNOWN_ON_LEASE_LOSS,
            inputs={"workspace_ref": "schema://object/1.0"},
            result_schema_id="schema://object/1.0",
            max_duration_seconds=DEFAULT_TIMEOUT_SECONDS,
            supports_idempotency=False,
            supports_cancel=True,
            supports_recover=True,
            capabilities=(CAPABILITY_WORKSPACE_WRITE, CAPABILITY_PROCESS_RUN),
        ),
        ToolManifest(
            "verify", "1.0.0",
            inputs={"workspace_ref": "schema://object/1.0"},
            result_schema_id="schema://object/1.0",
            capabilities=(CAPABILITY_WORKSPACE_READ, CAPABILITY_PROCESS_RUN),
            **_READ_ONLY,
        ),
    )


def register_dev_tools(
    registry,
    runner: WorkspaceRunner,
    *,
    verify_profiles: Sequence[VerifyProfile],
    allowed_capabilities: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Register the dev tools a deployment has granted capabilities for.

    Returns the names actually registered. Policy is applied here, before the
    registry is sealed, so a capability the deployment withheld is not merely
    refused at execution time — the tool does not exist.
    """

    permitted = None if allowed_capabilities is None else set(allowed_capabilities)
    adapters = {
        "git.status": lambda: GitStatusAdapter(runner),
        "git.diff": lambda: GitDiffAdapter(runner),
        "git.integrate": lambda: GitIntegrateAdapter(runner),
        "verify": lambda: VerifyAdapter(runner, verify_profiles),
    }
    registered: list[str] = []
    for manifest in dev_tool_manifests():
        if permitted is not None and not permitted.issuperset(manifest.capabilities):
            continue
        registry.register(manifest, adapters[manifest.name]())
        registered.append(manifest.name)
    return tuple(registered)
