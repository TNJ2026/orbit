"""A Planner provider backed by a trusted, locally installed agent CLI.

The Runtime never depends on a model SDK: it talks to a `PlannerProvider`, and
this is the production implementation for people whose "model" is an agent CLI
already on their machine.

The rules are the same ones the development tools follow, for the same reasons:

* The **command is constructor-owned.** It comes from Agent CLI discovery,
  which only resolves names from an in-code allowlist. A planning context is
  data written to the child's stdin; it never contributes an argument.
* **No shell, explicit environment.** A planner call must not inherit whatever
  credentials happened to be exported in the shell that started the server.
* **Output is bounded.** A planner that prints forever is an output bomb, and
  the raw response is persisted before it is parsed.

The error mapping carries the most weight. A timeout becomes
`PlannerUnknownResultError`, not a failure: the CLI may well have called a
model and been charged for it, so claiming the attempt failed would license a
silent second call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from ...platform import process as process_port
from ..cli_environment import trusted_cli_environment
from ..domain.planner import PlannerUsage, PlanningContext
from ..domain.serialization import canonical_json, to_primitive
from .provider import (
    PlannerPermanentError, PlannerProviderResponse, PlannerTransientError,
    PlannerUnknownResultError,
)


DEFAULT_TIMEOUT_SECONDS = 300
MAX_RESPONSE_BYTES = 256 * 1024


@dataclass(frozen=True)
class CliPlannerRequest:
    """Exactly what the child process is told, and nothing more."""

    model_id: str
    request_fingerprint: str
    context: Mapping[str, Any]

    def to_json(self) -> str:
        return canonical_json(
            {
                "schema_version": "1.0",
                "model_id": self.model_id,
                "request_fingerprint": self.request_fingerprint,
                "context": self.context,
            }
        )


class TrustedCliPlannerProvider:
    """Runs a discovered agent CLI as the planner.

    `command` is a tuple owned by whoever constructs this — in production, the
    executable path that Agent CLI discovery resolved from the allowlist.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        environment: Mapping[str, str] | None = None,
        runner: Callable[..., Any] = process_port.run,
        redactor=None,
    ) -> None:
        if not command or any(not str(part).strip() for part in command):
            raise ValueError("a trusted planner CLI command is required")
        if timeout_seconds <= 0 or max_response_bytes < 1:
            raise ValueError("planner timeout and output limit must be positive")
        self.command = tuple(str(part) for part in command)
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.environment = dict(
            environment if environment is not None else trusted_cli_environment()
        )
        self.runner = runner
        self.redactor = redactor

    # -- provider port ----------------------------------------------------

    def generate(
        self, context: PlanningContext, *, model_id: str, request_fingerprint: str
    ) -> PlannerProviderResponse:
        payload = CliPlannerRequest(
            model_id, request_fingerprint, to_primitive(context)
        ).to_json()

        try:
            outcome = self.runner(
                list(self.command),
                env=self.environment,
                stdin_text=payload,
                timeout=self.timeout_seconds,
                max_output_bytes=self.max_response_bytes,
                redactor=self.redactor,
            )
        except (FileNotFoundError, PermissionError) as exc:
            # A missing or non-executable binary will not heal on retry;
            # calling this transient would spin the retry budget on nothing.
            raise PlannerPermanentError(f"planner CLI cannot run: {exc}") from None
        except OSError as exc:
            # The CLI could not be started at all, so nothing was called and
            # nothing was charged: safe to retry.
            raise PlannerTransientError(f"planner CLI could not start: {exc}") from None

        if getattr(outcome, "timed_out", False):
            # Unknown, not failed. The child may have reached the model and
            # been billed; calling this a failure would licence a second call.
            raise PlannerUnknownResultError(
                f"planner CLI exceeded {self.timeout_seconds}s with an unknown result"
            )
        if getattr(outcome, "cancelled", False):
            raise PlannerUnknownResultError("planner CLI was cancelled mid-call")

        if outcome.returncode != 0:
            detail = (outcome.stderr or outcome.stdout or "").strip()[:500]
            raise PlannerTransientError(
                f"planner CLI exited {outcome.returncode}: {detail}"
            )
        if getattr(outcome, "stdout_truncated", False):
            # A clipped response would be parsed as malformed and blamed on the
            # model; saying why is more useful than a schema error.
            raise PlannerPermanentError(
                f"planner CLI response exceeded {self.max_response_bytes} bytes"
            )
        if not outcome.stdout.strip():
            raise PlannerTransientError("planner CLI produced no response")

        # The raw text is returned as-is. Parsing and schema enforcement belong
        # to the Planner service, which persists the raw response first so a
        # malformed answer can still be inspected after the fact.
        return PlannerProviderResponse(
            outcome.stdout,
            provider_request_id=None,
            usage=PlannerUsage(incomplete=True),
        )

    def cancel(self, request_fingerprint: str) -> bool:
        """Nothing to cancel out of band.

        Each call is one child process owned by `generate`; the process port
        kills it and its descendants on timeout. Reporting False rather than a
        hopeful True keeps the caller from recording a cancellation that never
        happened.
        """

        return False
