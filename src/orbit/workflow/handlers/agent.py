"""Structured Agent Handler over an injected trusted AgentClientPort."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import hashlib
from threading import Lock
from typing import Any, Mapping, Protocol, runtime_checkable

from ...platform.process import (
    ProcessHandle, process_identity, stop_pid_tree_if_identity,
)
from ..cli_environment import trusted_cli_environment
from ..domain.deadlines import AGENT_KILL_GRACE_SECONDS
from ..domain.ids import EntityId
from ..domain.accounting import UsageSnapshot
from ..domain.durable_execution import ExecutionSafety
from ..domain.handlers import (
    CancelAck, CancelDisposition, ExternalEffect, HandlerResult,
    HandlerResultStatus, HandlerValidationError, HandlerValidationIssue,
    HandlerValidationResult, PreparedExecution, RawHandlerResult,
    RecoveryDisposition, RecoveryResult, UnknownExternalResultError,
)
from ..domain.serialization import to_primitive


# The single output port every discovered Agent's manifest declares. The
# manifest is what a workflow binds to, so this name is a contract between
# `agent_discovery.agent_manifest` and the client that fills it.
AGENT_RESULT_PORT = "result"
# The reply is prose, but the port it fills is typed as an object — that is how
# every discovered Agent's manifest declares it, and how one Agent's output can
# satisfy the next Agent's object-typed input. So the text is carried under a
# key rather than returned bare: a bare string reaching a downstream object port
# is rejected as "not of type object", which is where an Agent chain used to die
# one node after the Agent that actually answered.
AGENT_RESULT_TEXT_KEY = "text"
AGENT_COMPLETION_MARKER = "ORBIT_RESULT_COMPLETE"

AGENT_RUNTIME_COMPLETION_PROTOCOL = """RUNTIME EXECUTION CONSTRAINTS
Work within the available time and stop starting new operations when time is short.
Prefer tests targeted at the changes; do not run the full test suite by default.
When asked to wrap up, a test fails, or progress is blocked, return immediately with the current result.
Always report completed changes, tests run, errors, and remaining work, even when the task is only partially complete.
After the complete final response, print ORBIT_RESULT_COMPLETE on a line by itself. Orbit treats that line as completion; do not print anything after it."""


@dataclass(frozen=True)
class AgentRequest:
    input: Mapping[str, Any]
    config: Mapping[str, Any]
    idempotency_key: str


@dataclass(frozen=True)
class AgentResponse:
    output: Mapping[str, Any]
    usage: UsageSnapshot | None
    provider_request_id: str | None
    finish_reason: str = "completed"
    # Artifacts the client staged for the result port, when the port is
    # artifact_ref. Empty on the inline path.
    artifact_refs: tuple[Any, ...] = ()


@runtime_checkable
class AgentClientPort(Protocol):
    def execute(self, request: AgentRequest, context: object) -> AgentResponse: ...
    def cancel(self, execution_ref: str) -> CancelAck: ...
    def recover(self, recovery_ref: str) -> RecoveryResult: ...


class FakeAgentClient:
    def __init__(self, response=None, error=None) -> None:
        self.response = response
        self.error = error
        self.requests = []

    def execute(self, request, context):
        self.requests.append(request)
        if self.error is not None: raise self.error
        return self.response

    def cancel(self, execution_ref): return CancelAck(CancelDisposition.CONFIRMED_STOPPED)
    def recover(self, recovery_ref): return RecoveryResult(RecoveryDisposition.NOT_FOUND)


def _safe_name(value: str) -> str:
    """A run id as one path segment, with nothing that can leave the root."""

    cleaned = "".join(
        character if character.isalnum() or character in "-_." else "_"
        for character in value
    )
    return cleaned.strip(".") or "shared"


class TrustedCliAgentClient:
    """Local first-party CLI adapter; command is constructor-owned, never DSL-owned."""

    def __init__(
        self, command: tuple[str, ...], *, timeout_seconds=3600,
        kill_grace_seconds=AGENT_KILL_GRACE_SECONDS, max_output_bytes=1_048_576,
        environment: Mapping[str, str] | None = None,
        workspace_root: Path | str | None = None,
    ) -> None:
        if not command or any(not item for item in command):
            raise ValueError("trusted CLI command is required")
        if timeout_seconds <= 0 or kill_grace_seconds <= 0 or max_output_bytes < 1:
            raise ValueError("CLI timeout, kill grace and output limit must be positive")
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds
        self.kill_grace_seconds = kill_grace_seconds
        self.max_output_bytes = max_output_bytes
        self.environment = dict(
            environment if environment is not None else trusted_cli_environment()
        )
        # Where a run's Agents are put to work. Without one they inherited the
        # Runtime's own working directory, which on a developer's machine is
        # whatever repository they happened to start `orbit serve` in — and an
        # Agent asked to merge a pull request merged that repository.
        #
        # This is isolation, not confinement. Nothing stops a CLI from writing
        # an absolute path or changing directory; what it stops is the far more
        # likely accident, an Agent doing exactly what it was asked to do in
        # whatever directory it woke up in. A real boundary is the operating
        # system's to draw, and calling this a sandbox would be claiming one
        # that is not here.
        self.workspace_root = (
            None if workspace_root is None
            else Path(workspace_root).expanduser().absolute()
        )
        # Reader threads this client could not get back. Never reset: it is a
        # cumulative account of leaked capacity, which is what makes the leak
        # visible to whoever reads the metric.
        self.leaked_reader_threads = 0
        self._lock = Lock()
        self._executions = {}

    def execute(self, request, context):
        stdout = self._run(
            (),
            json.dumps(to_primitive({
                "input": request.input, "config": request.config
            })).encode("utf-8"),
            context,
        )
        try:
            value = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HandlerValidationError("agent CLI returned invalid JSON") from None
        if not isinstance(value, dict) or not isinstance(value.get("output"), dict):
            raise HandlerValidationError("agent CLI result must contain object output")
        return AgentResponse(
            value["output"], None, value.get("provider_request_id"),
            value.get("finish_reason", "completed"),
        )

    def _run(
        self, extra_args, payload, context, *, max_output_bytes=None,
        completion_marker: str | None = None,
    ):
        """Spawn, feed, bound and reap. Subclasses decide argv tail and parsing.

        Everything here is about surviving the process, not about what it says:
        a timeout or a non-zero exit after submission is an *unknown* external
        result, never a failure, because the Agent may already have acted.

        `max_output_bytes` overrides the read ceiling for this call — an
        artifact result port raises it to the port's size limit, so a reply too
        big to go inline is still fully read on its way to the blob store.
        """

        sink = getattr(context, "output", None)

        def publish(name):
            def emit(chunk):
                if sink is None:
                    return
                try:
                    sink.emit(name, chunk)
                except Exception:  # noqa: BLE001 - output is observational
                    pass
            return emit

        def record_process(handle: ProcessHandle) -> None:
            recorder = getattr(context, "record_execution", None)
            if recorder is None:
                return
            identity = process_identity(handle.pid)
            if identity is None:
                raise UnknownExternalResultError(
                    "cannot record a safe Agent process identity"
                )
            try:
                recorder(json.dumps(
                    {
                        "kind": "local_process_v1", "pid": handle.pid,
                        "identity": identity,
                    },
                    sort_keys=True, separators=(",", ":"),
                ))
            except UnknownExternalResultError:
                raise
            except Exception as exc:
                raise UnknownExternalResultError(
                    "cannot persist the Agent process identity"
                ) from exc

        handle = ProcessHandle(
            (*self.command, *extra_args),
            cwd=self._workspace(context),
            env=self.environment,
            stdin_text=payload.decode("utf-8"),
            max_output_bytes=max_output_bytes or self.max_output_bytes,
            on_stdout=publish("stdout"),
            on_stderr=publish("stderr"),
            on_start=record_process,
        )
        execution_ref = f"agent:{context.request.attempt_id}"
        with self._lock:
            if execution_ref in self._executions:
                handle.cancel(
                    grace_seconds=self.kill_grace_seconds,
                    reason="duplicate_execution",
                )
                raise RuntimeError("duplicate concurrent Agent execution reference")
            self._executions[execution_ref] = handle
        try:
            outcome = handle.wait(
                timeout=self._attempt_timeout(context),
                kill_grace_seconds=self.kill_grace_seconds,
                completion_predicate=(
                    None if completion_marker is None
                    else lambda stdout: _has_terminal_marker(
                        stdout, completion_marker
                    )
                ),
            )
        finally:
            with self._lock:
                self._executions.pop(execution_ref, None)
        with self._lock:
            # Under the lock because one client serves every concurrent attempt
            # and `+=` is a read then a write. A dropped increment is the one
            # failure this counter cannot afford: being exact about leaked
            # capacity is the whole reason it exists.
            self.leaked_reader_threads += outcome.leaked_drain_threads
        stdout = outcome.stdout
        completed = bool(
            completion_marker
            and _has_terminal_marker(stdout, completion_marker)
            and not outcome.stdout_truncated
        )
        if outcome.timed_out and not completed:
            raise UnknownExternalResultError("agent CLI timed out after request submission")
        if outcome.cancelled:
            raise UnknownExternalResultError("agent CLI cancellation outcome is unknown")
        if outcome.stdout_truncated:
            raise HandlerValidationError("agent CLI output exceeds size limit")
        if outcome.returncode != 0 and not completed:
            stderr = outcome.stderr.encode("utf-8")
            digest = hashlib.sha256(stderr).hexdigest()[:16]
            raise UnknownExternalResultError(
                f"agent CLI exited with code {outcome.returncode} after request submission "
                f"(stderr_bytes={len(stderr)}, stderr_sha256={digest})"
            )
        if completed:
            stdout = _strip_terminal_marker(stdout, completion_marker)
        return stdout.encode("utf-8")

    def _workspace(self, context) -> Path | None:
        """This run's own directory, made on first use.

        Per run rather than per attempt: nodes in one workflow hand work to
        each other through files, and a fresh directory for every attempt
        would lose what the step before it wrote. A retry of one node is meant
        to see what its predecessors left.
        """

        if self.workspace_root is None:
            return None
        run_id = str(getattr(context.request, "run_id", "") or "shared")
        workspace = self.workspace_root / _safe_name(run_id)
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def _attempt_timeout(self, context) -> float:
        """How long *this* attempt may run, not how long any attempt may run.

        `timeout_seconds` is set once when the registry is built, so every node
        shares it. The Runtime, meanwhile, gives each attempt its own schedule
        from the node's budget. Waiting the constructor's value on a node
        configured for ten minutes means the CLI is still running when the
        Runtime has already written the attempt off — every timeout becomes a
        late result. The schedule wins; the constructor value stays as the
        ceiling for callers that have no request to read.

        The point aimed at is `process_deadline`, not the node's deadline:
        stopping the tree and settling the attempt has to happen *inside* the
        budget, so the CLI has to be finished before the budget is. Callers
        that predate the schedule fall back to the node deadline, which is
        what they always meant by it.
        """

        request = getattr(context, "request", None)
        deadline = (
            getattr(request, "process_deadline", None)
            or getattr(request, "deadline", None)
        )
        if deadline is None:
            return self.timeout_seconds
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        # A deadline already in the past still gets one positive tick: the
        # process is spawned by now, and killing it through the timeout path
        # keeps one exit route instead of two.
        return max(0.001, min(float(self.timeout_seconds), remaining))

    def preflight(self) -> None:
        if shutil.which(self.command[0]) is None:
            raise RuntimeError(f"trusted agent CLI is unavailable: {self.command[0]}")

    def cancel(self, execution_ref):
        with self._lock:
            handle = self._executions.get(execution_ref)
        if handle is None: return CancelAck(CancelDisposition.CONFIRMED_STOPPED)
        handle.cancel(
            grace_seconds=self.kill_grace_seconds,
            reason="cancelled",
        )
        return CancelAck(CancelDisposition.UNKNOWN, "termination requested")

    def recover(self, recovery_ref):
        try:
            value = json.loads(recovery_ref)
        except (TypeError, json.JSONDecodeError):
            value = None
        if (
            isinstance(value, dict)
            and value.get("kind") == "local_process_v1"
            and isinstance(value.get("pid"), int)
            and not isinstance(value.get("pid"), bool)
            and isinstance(value.get("identity"), str)
        ):
            stop_pid_tree_if_identity(
                value["pid"], value["identity"],
                grace_seconds=self.kill_grace_seconds,
            )
        return RecoveryResult(RecoveryDisposition.UNKNOWN, provider_request_id=recovery_ref)


class TrustedPromptCliAgentClient(TrustedCliAgentClient):
    """Adapter for CLIs that take a prompt and print prose.

    No installed Agent CLI speaks Orbit's `{"input": ...}` → `{"output": ...}`
    protocol; they take a prompt and answer in text. This client renders the
    node's input into one prompt string, hands it to the CLI the way that CLI
    accepts it, and returns the reply on the port every discovered Agent
    declares: `AGENT_RESULT_PORT`.

    The port name is not decoration. A workflow binds to the manifest, the
    kernel refuses a completion whose output does not fill the node's declared
    ports, and the worker's report is then all there is — so a client that
    answered on a port of its own invention produced Agents that could run
    perfectly and never complete.

    The argv prefix is still constructor-owned and comes from the reviewed
    allowlist. The prompt is *data*: it is passed either on stdin, or as the
    value of a code-owned flag, or as a positional after `--`. argv is a list
    and no shell is involved, so a prompt cannot become a command — but on the
    flag/positional paths it is visible in the process list to other users on
    this machine, which is why stdin is preferred wherever the CLI allows it.
    """

    def __init__(
        self, command, *, prompt_flag: str | None = None,
        prompt_positional: bool = False, max_prompt_bytes: int = 131_072,
        process_timeout_flag: str | None = None, **kwargs,
    ) -> None:
        super().__init__(command, **kwargs)
        if prompt_flag is not None and prompt_positional:
            raise ValueError("a prompt is passed one way: flag or positional")
        if prompt_flag is not None and not prompt_flag.startswith("-"):
            raise ValueError(f"prompt flag must be a flag, got {prompt_flag!r}")
        if max_prompt_bytes < 1:
            raise ValueError("prompt limit must be positive")
        if process_timeout_flag is not None and not process_timeout_flag.startswith("--"):
            raise ValueError("process timeout flag must be a long flag")
        self.prompt_flag = prompt_flag
        self.prompt_positional = prompt_positional
        self.max_prompt_bytes = max_prompt_bytes
        self.process_timeout_flag = process_timeout_flag

    def execute(self, request, context):
        # An upstream Agent may have handed this one its answer as an artifact
        # reference (a large output that never went inline). Resolve it back to
        # text before rendering, so the prompt is the prose, not a reference.
        resolved_input = _resolve_artifact_inputs(request.input, context)
        prompt = render_agent_prompt(resolved_input, request.config)
        encoded = prompt.encode("utf-8")
        # A prompt fed by an artifact input may be far larger than the inline
        # cap — the point of carrying it as an artifact. Only a stdin CLI can
        # receive it: a flag or positional value goes in argv, which the OS caps
        # at ARG_MAX, so those keep the small inline limit and say so plainly.
        via_stdin = self.prompt_flag is None and not self.prompt_positional
        prompt_limit = (
            max(self.max_prompt_bytes, _artifact_input_budget(context))
            if via_stdin else self.max_prompt_bytes
        )
        if len(encoded) > prompt_limit:
            hint = "" if via_stdin else (
                " — this CLI takes the prompt as an argument, which the OS "
                "caps; a stdin-based Agent can receive a large artifact input"
            )
            raise HandlerValidationError(
                f"prompt exceeds {prompt_limit} bytes for this Agent CLI{hint}"
            )
        timeout_args = ()
        if self.process_timeout_flag is not None:
            # The CLI must give control back before Orbit's own process
            # deadline. That leaves the adapter its reserved grace to stop any
            # descendants, drain output, and settle under the lease.
            internal = max(
                1, int(self._attempt_timeout(context) - self.kill_grace_seconds)
            )
            timeout_args = (f"{self.process_timeout_flag}={internal}s",)
        if self.prompt_flag is not None:
            extra, payload = (*timeout_args, self.prompt_flag, prompt), b""
        elif self.prompt_positional:
            # `--` first: a prompt that happens to start with a dash stays an
            # argument to read, not a flag to obey.
            extra, payload = (*timeout_args, "--", prompt), b""
        else:
            extra, payload = timeout_args, encoded
        # An artifact result port lifts the read ceiling to its size limit, so a
        # reply too large for the inline path is still fully captured.
        result_port = _artifact_port(
            getattr(getattr(context, "request", None), "output_ports", ()),
            AGENT_RESULT_PORT,
        )
        max_output_bytes = None
        if result_port is not None:
            max_output_bytes = result_port.get("data_policy", {}).get("max_size_bytes")
        stdout = self._run(
            extra, payload, context, max_output_bytes=max_output_bytes,
            completion_marker=AGENT_COMPLETION_MARKER,
        )
        text = stdout.decode("utf-8", errors="replace").strip()
        return _agent_result(text, context)


def _has_terminal_marker(stdout: str, marker: str) -> bool:
    """True once any complete output line is the exact protocol marker."""

    return any(line.strip() == marker for line in stdout.splitlines())


def _strip_terminal_marker(stdout: str, marker: str) -> str:
    lines = stdout.rstrip().splitlines()
    for index, line in enumerate(lines):
        if line.strip() == marker:
            # The marker commits everything before it as the result. Output
            # racing in after that point belongs to a process Orbit is already
            # stopping and must not leak into the committed response.
            lines = lines[:index]
            break
    return "\n".join(lines)


def _artifact_port(ports, port_id: str):
    """The plan port dict for `port_id` when it is artifact_ref, else None."""

    for port in ports or ():
        if port.get("id") == port_id and (
            port.get("data_policy", {}).get("transport") == "artifact_ref"
        ):
            return port
    return None


def _artifact_input_budget(context) -> int:
    """The largest artifact input the plan lets this node receive.

    Zero when no input port is artifact_ref, so an ordinary Agent keeps its
    small inline prompt cap.
    """

    ports = getattr(getattr(context, "request", None), "input_ports", ())
    budget = 0
    for port in ports or ():
        policy = port.get("data_policy", {})
        if policy.get("transport") == "artifact_ref":
            budget = max(budget, int(policy.get("max_size_bytes") or 0))
    return budget


def _resolve_artifact_inputs(node_input, context):
    """Replace artifact-reference input values with their text.

    Only ports the plan declares artifact_ref are touched, and only their
    committed, authorised blobs are read — an inline value that merely happens
    to carry an `artifact_id` key is left exactly as it is.
    """

    ports = getattr(getattr(context, "request", None), "input_ports", ())
    artifacts = getattr(context, "artifacts", None)
    if not ports or artifacts is None or not isinstance(node_input, Mapping):
        return node_input
    resolved = dict(node_input)
    for port in ports:
        if port.get("data_policy", {}).get("transport") != "artifact_ref":
            continue
        value = resolved.get(port["id"])
        if not isinstance(value, Mapping) or "artifact_id" not in value:
            continue
        blob = artifacts.read(EntityId.parse(str(value["artifact_id"])))
        resolved[port["id"]] = blob.decode("utf-8", errors="replace")
    return resolved


def _agent_result(text: str, context) -> "AgentResponse":
    """The reply, inline for a small port or staged for an artifact one.

    The port the workflow declared decides. An artifact_ref result port sends
    the text to the blob store — no 1 MiB inline ceiling — and the output
    carries only the reference; an inline port keeps the object envelope.
    """

    ports = getattr(getattr(context, "request", None), "output_ports", ())
    port = _artifact_port(ports, AGENT_RESULT_PORT)
    if port is None:
        return AgentResponse(
            {AGENT_RESULT_PORT: {AGENT_RESULT_TEXT_KEY: text}}, None, None, "completed",
        )
    content_types = port.get("data_policy", {}).get("content_types") or ("text/plain",)
    artifact_id = context.artifacts.write(
        name=AGENT_RESULT_PORT, content=text.encode("utf-8"),
        content_type=content_types[0],
    )
    return AgentResponse(
        {AGENT_RESULT_PORT: {"artifact_id": str(artifact_id)}},
        None, None, "completed", artifact_refs=(artifact_id,),
    )


def render_agent_prompt(
    node_input: Mapping[str, Any], config: Mapping[str, Any]
) -> str:
    """One prompt string from the node's authored config and its runtime input.

    The authored preamble comes first and the runtime value follows inside
    delimiters. The delimiters are for the reader's benefit, not a security
    boundary: the CLI has no command surface to protect here, because argv is
    fixed before the prompt is known.
    """

    parts = []
    preamble = config.get("prompt")
    if isinstance(preamble, str) and preamble.strip():
        parts.append(preamble.strip())
    value = node_input.get("prompt", node_input)
    rendered = value if isinstance(value, str) else json.dumps(
        to_primitive(value), ensure_ascii=False, sort_keys=True
    )
    if parts:
        parts.append(f"INPUT-BEGIN\n{rendered}\nINPUT-END")
    else:
        parts.append(rendered)
    parts.append(AGENT_RUNTIME_COMPLETION_PROTOCOL)
    return "\n\n".join(parts)


class AgentHandler:
    def __init__(self, client: AgentClientPort) -> None:
        if not isinstance(client, AgentClientPort): raise TypeError("invalid AgentClientPort")
        self.client = client

    def preflight(self) -> None:
        check = getattr(self.client, "preflight", None)
        if check is not None: check()

    def validate(self, manifest, config):
        issues = []
        if manifest.execution_safety is not ExecutionSafety.UNKNOWN_ON_LEASE_LOSS:
            issues.append(HandlerValidationIssue(
                ("execution_safety",), "AgentHandler requires unknown_on_lease_loss"
            ))
        if "model" in config and not isinstance(config["model"], str):
            issues.append(HandlerValidationIssue(("model",), "model must be a string"))
        return HandlerValidationResult(tuple(issues))

    def prepare(self, request, context):
        return PreparedExecution(
            {"input": request.input, "config": request.config, "idempotency_key": request.idempotency_key},
            f"agent:{request.attempt_id}",
        )

    def execute(self, prepared, context):
        response = self.client.execute(
            AgentRequest(
                prepared.payload["input"], prepared.payload["config"],
                prepared.payload["idempotency_key"],
            ), context,
        )
        return RawHandlerResult(
            response.output, response.usage, response.provider_request_id,
            ExternalEffect.KNOWN_APPLIED, artifact_refs=response.artifact_refs,
        )

    def normalize_result(self, raw, context):
        if not isinstance(raw.output, Mapping):
            raise HandlerValidationError("Agent output must be an object")
        return HandlerResult(
            HandlerResultStatus.SUCCEEDED, raw.output, None, raw.usage,
            raw.usage is None, raw.external_effect, raw.provider_request_id,
            artifact_refs=raw.artifact_refs,
        )

    def cancel(self, execution_ref, context): return self.client.cancel(execution_ref)
    def recover(self, recovery_ref, context): return self.client.recover(recovery_ref)
