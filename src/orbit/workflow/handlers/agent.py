"""Structured Agent Handler over an injected trusted AgentClientPort."""

from __future__ import annotations

from dataclasses import dataclass
import json
import subprocess
import shutil
from threading import Event, Lock, Thread
import hashlib
from typing import Any, Mapping, Protocol, runtime_checkable

from ..cli_environment import trusted_cli_environment
from ..domain.accounting import UsageSnapshot
from ..domain.durable_execution import ExecutionSafety
from ..domain.handlers import (
    CancelAck, CancelDisposition, ExternalEffect, HandlerResult,
    HandlerResultStatus, HandlerValidationError, HandlerValidationIssue,
    HandlerValidationResult, PreparedExecution, RawHandlerResult,
    RecoveryDisposition, RecoveryResult, UnknownExternalResultError,
)
from ..domain.serialization import to_primitive


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
        process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=self.environment,
        )
        execution_ref = f"agent:{context.request.attempt_id}"
        with self._lock:
            if execution_ref in self._executions:
                process.kill()
                raise RuntimeError("duplicate concurrent Agent execution reference")
            self._executions[execution_ref] = {"process": process, "cancelled": False}
        try:
            stdout, stderr, overflow = self._communicate_bounded(
                process,
                json.dumps(to_primitive({
                    "input": request.input, "config": request.config
                })).encode("utf-8"),
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
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
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

    def _communicate_bounded(self, process, payload):
        stdout_chunks, stderr_chunks = [], []
        stdout_size = 0
        overflow = Event()
        errors = []

        def write_input():
            try:
                process.stdin.write(payload); process.stdin.close()
            except (BrokenPipeError, OSError) as exc:
                errors.append(exc)

        def read_output(pipe, chunks, limit, enforce):
            nonlocal stdout_size
            while True:
                chunk = pipe.read(65_536)
                if not chunk: break
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
            Thread(target=read_output, args=(process.stdout, stdout_chunks, self.max_output_bytes, True), daemon=True),
            Thread(target=read_output, args=(process.stderr, stderr_chunks, 65_536, False), daemon=True),
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
            ExternalEffect.KNOWN_APPLIED,
        )

    def normalize_result(self, raw, context):
        if not isinstance(raw.output, Mapping):
            raise HandlerValidationError("Agent output must be an object")
        return HandlerResult(
            HandlerResultStatus.SUCCEEDED, raw.output, None, raw.usage,
            raw.usage is None, raw.external_effect, raw.provider_request_id,
        )

    def cancel(self, execution_ref, context): return self.client.cancel(execution_ref)
    def recover(self, recovery_ref, context): return self.client.recover(recovery_ref)
