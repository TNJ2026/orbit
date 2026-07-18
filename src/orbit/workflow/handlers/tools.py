"""Exact-bound trusted Tool adapters and the generic ToolHandler."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable

from ..catalogs.handlers import _version_tuple
from ..domain.accounting import UsageSnapshot
from ..domain.durable_execution import ExecutionSafety
from ..domain.handlers import (
    CancelAck, CancelDisposition, ExternalEffect, HandlerResult,
    HandlerResultStatus, HandlerValidationIssue, HandlerValidationResult,
    PreparedExecution, RawHandlerResult, RecoveryDisposition, RecoveryResult,
)


@dataclass(frozen=True)
class ToolManifest:
    name: str
    version: str
    execution_safety: ExecutionSafety
    inputs: Mapping[str, str]
    result_schema_id: str
    max_duration_seconds: int
    supports_idempotency: bool
    supports_cancel: bool
    supports_recover: bool
    capabilities: tuple[str, ...] = ()
    required_secrets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool name is required")
        _version_tuple(self.version)
        if not self.result_schema_id.strip():
            raise ValueError("tool result_schema_id is required")
        if self.max_duration_seconds < 1:
            raise ValueError("tool max_duration_seconds must be positive")
        object.__setattr__(self, "inputs", MappingProxyType(dict(sorted(self.inputs.items()))))
        object.__setattr__(self, "capabilities", tuple(sorted(set(self.capabilities))))
        object.__setattr__(self, "required_secrets", tuple(sorted(set(self.required_secrets))))


@dataclass(frozen=True)
class RegisteredTool:
    manifest: ToolManifest
    adapter: "ToolAdapter"


@dataclass(frozen=True)
class ToolRequest:
    input: Mapping[str, Any]
    idempotency_key: str
    config: Mapping[str, Any]


@dataclass(frozen=True)
class ToolResult:
    output: Mapping[str, Any]
    usage: UsageSnapshot | None = None
    provider_request_id: str | None = None
    external_effect: ExternalEffect = ExternalEffect.NONE


@runtime_checkable
class ToolAdapter(Protocol):
    def execute(self, request: ToolRequest, context: object) -> ToolResult: ...
    def cancel(self, execution_ref: str, context: object) -> CancelAck: ...
    def recover(self, recovery_ref: str, context: object) -> RecoveryResult: ...


class ToolRegistry:
    def __init__(self) -> None:
        self._tools = {}
        self._sealed = False

    def register(self, manifest: ToolManifest, adapter: ToolAdapter) -> None:
        if self._sealed: raise RuntimeError("ToolRegistry is sealed")
        if not isinstance(adapter, ToolAdapter): raise TypeError("invalid ToolAdapter")
        key = (manifest.name, manifest.version)
        if key in self._tools: raise ValueError("duplicate tool")
        self._tools[key] = RegisteredTool(manifest, adapter)

    def seal(self) -> None: self._sealed = True

    def resolve(self, name: str, version: str) -> RegisteredTool:
        if not self._sealed: raise RuntimeError("ToolRegistry is not sealed")
        try: return self._tools[(name, version)]
        except KeyError: raise LookupError(f"tool not available: {name}@{version}") from None


class ToolHandler:
    def __init__(self, tools: ToolRegistry) -> None:
        self.tools = tools

    def validate(self, manifest, config):
        issues = []
        for key in ("tool_name", "tool_version"):
            if not isinstance(config.get(key), str) or not config[key].strip():
                issues.append(HandlerValidationIssue((key,), f"{key} is required"))
        if not issues:
            try:
                registered = self.tools.resolve(config["tool_name"], config["tool_version"])
            except LookupError as error:
                issues.append(HandlerValidationIssue(("tool_name",), str(error)))
            else:
                if registered.manifest.execution_safety is not manifest.execution_safety:
                    issues.append(HandlerValidationIssue(
                        ("tool_name",), "tool execution safety must match the handler manifest"
                    ))
                if registered.manifest.inputs != manifest.inputs:
                    issues.append(HandlerValidationIssue(
                        ("tool_name",), "tool input schemas must match the handler manifest"
                    ))
                if registered.manifest.result_schema_id != manifest.result_schema_id:
                    issues.append(HandlerValidationIssue(
                        ("tool_name",), "tool result schema must match the handler manifest"
                    ))
                if registered.manifest.max_duration_seconds > manifest.resource_profile.max_duration_seconds:
                    issues.append(HandlerValidationIssue(
                        ("tool_name",), "tool duration exceeds the handler resource profile"
                    ))
                if registered.manifest.supports_cancel and not manifest.supports_cancel:
                    issues.append(HandlerValidationIssue(
                        ("tool_name",), "tool cancellation is undeclared by the handler manifest"
                    ))
                if registered.manifest.supports_recover and not manifest.supports_recover:
                    issues.append(HandlerValidationIssue(
                        ("tool_name",), "tool recovery is undeclared by the handler manifest"
                    ))
                if set(registered.manifest.capabilities) - set(manifest.capabilities):
                    issues.append(HandlerValidationIssue(
                        ("tool_name",), "tool requires undeclared capabilities"
                    ))
                if set(registered.manifest.required_secrets) - set(manifest.required_secrets):
                    issues.append(HandlerValidationIssue(
                        ("tool_name",), "tool requires undeclared secrets"
                    ))
        return HandlerValidationResult(tuple(issues))

    def prepare(self, request, context):
        return PreparedExecution(
            {
                "input": request.input, "config": request.config,
                "idempotency_key": request.idempotency_key,
            },
            f"tool:{request.attempt_id}",
        )

    def execute(self, prepared, context):
        config = prepared.payload["config"]
        registered = self.tools.resolve(config["tool_name"], config["tool_version"])
        result = registered.adapter.execute(
            ToolRequest(
                prepared.payload["input"], prepared.payload["idempotency_key"], config
            ),
            context,
        )
        return RawHandlerResult(
            result.output, result.usage, result.provider_request_id,
            result.external_effect,
        )

    def normalize_result(self, raw, context):
        return HandlerResult(
            HandlerResultStatus.SUCCEEDED, raw.output, None, raw.usage,
            raw.usage is None, raw.external_effect, raw.provider_request_id,
        )

    def cancel(self, execution_ref, context):
        config = context.request.config
        return self.tools.resolve(config["tool_name"], config["tool_version"]).adapter.cancel(execution_ref, context)

    def recover(self, recovery_ref, context):
        config = context.request.config
        return self.tools.resolve(config["tool_name"], config["tool_version"]).adapter.recover(recovery_ref, context)
