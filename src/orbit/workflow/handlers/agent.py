"""Structured Agent Handler over an injected trusted AgentClientPort."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import subprocess
import shutil
from threading import Event, Lock, Thread
import hashlib
from typing import Any, Mapping, Protocol, runtime_checkable

from ..cli_environment import trusted_cli_environment
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


def _abandon_pipe(stream) -> None:
    """Release a pipe we can no longer drain, without waiting on its reader.

    `close()` on a buffered pipe waits for the thread parked inside `read()`,
    and that thread is waiting for an EOF that will never arrive: the CLI has
    exited, but a process it left behind still holds the write end. Hermes
    keeps an MCP gateway alive exactly this way, and the wait is unbounded —
    a Handler that had already collected its answer would sit there until the
    lease expired and the attempt was written off as unsettled.

    Closing the descriptor ends the blocked read, the reader thread leaves,
    and the buffered object goes with it.
    """

    if stream is None:
        return
    try:
        os.close(stream.fileno())
    except (OSError, ValueError):
        pass


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


class TrustedCliAgentClient:
    """Local first-party CLI adapter; command is constructor-owned, never DSL-owned."""

    def __init__(
        self, command: tuple[str, ...], *, timeout_seconds=3600,
        kill_grace_seconds=2, max_output_bytes=1_048_576,
        environment: Mapping[str, str] | None = None,
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

    def _run(self, extra_args, payload, context, *, max_output_bytes=None):
        """Spawn, feed, bound and reap. Subclasses decide argv tail and parsing.

        Everything here is about surviving the process, not about what it says:
        a timeout or a non-zero exit after submission is an *unknown* external
        result, never a failure, because the Agent may already have acted.

        `max_output_bytes` overrides the read ceiling for this call — an
        artifact result port raises it to the port's size limit, so a reply too
        big to go inline is still fully read on its way to the blob store.
        """

        process = subprocess.Popen(
            (*self.command, *extra_args), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.environment,
        )
        execution_ref = f"agent:{context.request.attempt_id}"
        with self._lock:
            if execution_ref in self._executions:
                process.kill()
                raise RuntimeError("duplicate concurrent Agent execution reference")
            self._executions[execution_ref] = {"process": process, "cancelled": False}
        try:
            stdout, stderr, overflow = self._communicate_bounded(
                process, payload, sink=getattr(context, "output", None),
                max_output_bytes=max_output_bytes,
            )
        except TimeoutError:
            process.terminate()
            try:
                process.wait(timeout=self.kill_grace_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise UnknownExternalResultError("agent CLI timed out after request submission")
        finally:
            with self._lock:
                state = self._executions.pop(execution_ref, None)
                cancelled = bool(state and state["cancelled"])
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            _abandon_pipe(process.stdout)
            _abandon_pipe(process.stderr)
        if cancelled:
            raise UnknownExternalResultError("agent CLI cancellation outcome is unknown")
        if overflow:
            raise HandlerValidationError("agent CLI output exceeds size limit")
        if process.returncode != 0:
            digest = hashlib.sha256(stderr).hexdigest()[:16]
            raise UnknownExternalResultError(
                f"agent CLI exited with code {process.returncode} after request submission "
                f"(stderr_bytes={len(stderr)}, stderr_sha256={digest})"
            )
        return stdout

    def _communicate_bounded(self, process, payload, *, sink=None, max_output_bytes=None):
        stdout_limit = max_output_bytes or self.max_output_bytes
        stdout_chunks, stderr_chunks = [], []
        stdout_size = 0
        overflow = Event()
        errors = []

        def write_input():
            try:
                process.stdin.write(payload); process.stdin.close()
            except (BrokenPipeError, OSError) as exc:
                errors.append(exc)

        def publish(name, chunk):
            # Forwarded as it is read, so a long-running Agent is watchable and
            # a failed one still leaves an account. Never lets a reporting
            # problem interfere with reading the pipe.
            if sink is None:
                return
            try:
                sink.emit(name, chunk.decode("utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                return

        def read_output(pipe, chunks, limit, enforce, name):
            nonlocal stdout_size
            # `read` waits for a full buffer or EOF, which turns a five-minute
            # Agent into one silent block at the end. `read1` returns whatever
            # has arrived, which is what makes following the console possible.
            read = getattr(pipe, "read1", pipe.read)
            while True:
                try:
                    chunk = read(65_536)
                except (OSError, ValueError):
                    # The descriptor was dropped because nothing more could be
                    # drained from it. There is no output left to miss.
                    return
                if not chunk: break
                publish(name, chunk)
                current = stdout_size if enforce else sum(map(len, chunks))
                remaining = max(0, limit - current)
                if remaining: chunks.append(chunk[:remaining])
                if enforce:
                    stdout_size += len(chunk)
                    if stdout_size > limit and not overflow.is_set():
                        overflow.set()
                        try: process.terminate()
                        except OSError: pass

        threads = (
            Thread(target=write_input, daemon=True),
            Thread(target=read_output, args=(process.stdout, stdout_chunks, stdout_limit, True, "stdout"), daemon=True),
            Thread(target=read_output, args=(process.stderr, stderr_chunks, 65_536, False, "stderr"), daemon=True),
        )
        for thread in threads: thread.start()
        try:
            process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            raise TimeoutError from None
        finally:
            for thread in threads: thread.join(timeout=self.kill_grace_seconds)
        return b"".join(stdout_chunks), b"".join(stderr_chunks), overflow.is_set()

    def preflight(self) -> None:
        if shutil.which(self.command[0]) is None:
            raise RuntimeError(f"trusted agent CLI is unavailable: {self.command[0]}")

    def cancel(self, execution_ref):
        with self._lock:
            state = self._executions.get(execution_ref)
            process = None if state is None else state["process"]
            if state is not None:
                state["cancelled"] = True
        if process is None: return CancelAck(CancelDisposition.CONFIRMED_STOPPED)
        process.terminate()
        try:
            process.wait(timeout=self.kill_grace_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return CancelAck(CancelDisposition.UNKNOWN, "termination requested")

    def recover(self, recovery_ref):
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
        prompt_positional: bool = False, max_prompt_bytes: int = 131_072, **kwargs,
    ) -> None:
        super().__init__(command, **kwargs)
        if prompt_flag is not None and prompt_positional:
            raise ValueError("a prompt is passed one way: flag or positional")
        if prompt_flag is not None and not prompt_flag.startswith("-"):
            raise ValueError(f"prompt flag must be a flag, got {prompt_flag!r}")
        if max_prompt_bytes < 1:
            raise ValueError("prompt limit must be positive")
        self.prompt_flag = prompt_flag
        self.prompt_positional = prompt_positional
        self.max_prompt_bytes = max_prompt_bytes

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
        if self.prompt_flag is not None:
            extra, payload = (self.prompt_flag, prompt), b""
        elif self.prompt_positional:
            # `--` first: a prompt that happens to start with a dash stays an
            # argument to read, not a flag to obey.
            extra, payload = ("--", prompt), b""
        else:
            extra, payload = (), encoded
        # An artifact result port lifts the read ceiling to its size limit, so a
        # reply too large for the inline path is still fully captured.
        result_port = _artifact_port(
            getattr(getattr(context, "request", None), "output_ports", ()),
            AGENT_RESULT_PORT,
        )
        max_output_bytes = None
        if result_port is not None:
            max_output_bytes = result_port.get("data_policy", {}).get("max_size_bytes")
        stdout = self._run(extra, payload, context, max_output_bytes=max_output_bytes)
        text = stdout.decode("utf-8", errors="replace").strip()
        return _agent_result(text, context)


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
