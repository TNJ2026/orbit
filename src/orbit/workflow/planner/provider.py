"""Planner provider port; Runtime and replay never depend on a concrete model SDK."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..domain.planner import PlannerUsage, PlanningContext


class PlannerTransientError(RuntimeError): pass
class PlannerPermanentError(RuntimeError): pass
class PlannerUnknownResultError(TimeoutError): pass


@dataclass(frozen=True)
class PlannerProviderResponse:
    raw_response: str
    provider_request_id: str | None = None
    usage: PlannerUsage = PlannerUsage()

    def __post_init__(self) -> None:
        if not isinstance(self.raw_response, str):
            raise TypeError("Planner provider raw response must be text")
        if self.provider_request_id is not None and not self.provider_request_id.strip():
            raise ValueError("provider_request_id cannot be empty")


@runtime_checkable
class PlannerProvider(Protocol):
    def generate(self, context: PlanningContext, *, model_id: str, request_fingerprint: str) -> PlannerProviderResponse: ...
    def cancel(self, request_fingerprint: str) -> bool: ...


class FakePlannerProvider:
    """Deterministic queue-backed provider used by fault tests and offline Eval."""

    def __init__(self, responses=()) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []
        self.cancelled: set[str] = set()

    def generate(self, context, *, model_id, request_fingerprint):
        self.calls.append(request_fingerprint)
        if not self.responses:
            raise RuntimeError("FakePlannerProvider has no response")
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def cancel(self, request_fingerprint):
        self.cancelled.add(request_fingerprint)
        return True


class CallablePlannerProvider:
    """Adapter for an explicitly trusted callable with bounded protocol output."""

    def __init__(self, generate, cancel=None) -> None:
        if not callable(generate): raise TypeError("Planner generate adapter must be callable")
        self._generate, self._cancel = generate, cancel

    def generate(self, context, *, model_id, request_fingerprint):
        value = self._generate(context, model_id=model_id, request_fingerprint=request_fingerprint)
        if not isinstance(value, PlannerProviderResponse):
            raise PlannerPermanentError("Planner adapter returned an invalid response type")
        return value

    def cancel(self, request_fingerprint):
        return False if self._cancel is None else bool(self._cancel(request_fingerprint))
