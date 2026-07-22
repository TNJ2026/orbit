"""Composition and read-only diagnostics for the trusted Handler runtime."""

from __future__ import annotations

from dataclasses import dataclass

from ..handlers.executor import HandlerExecutor
from ..handlers.registry import ExecutionRegistry


@dataclass(frozen=True)
class HandlerDetail:
    name: str
    version: str
    manifest_fingerprint: str
    implementation_id: str
    implementation_fingerprint: str
    execution_safety: str
    capabilities: tuple[str, ...]
    required_secrets: tuple[str, ...]
    max_duration_seconds: int


@dataclass(frozen=True)
class HandlerRegistrySummary:
    fingerprint: str
    handler_count: int
    handlers: tuple[HandlerDetail, ...]


class HandlerRuntimeBuilder:
    def __init__(
        self, schema_catalog, *, secret_values=None, output_sink_factory=None,
    ) -> None:
        self.schemas = schema_catalog
        self.secret_values = dict(secret_values or {})
        # Optional: where Handler console output is kept for the operator. A
        # Runtime without one still executes every Handler.
        self.output_sink_factory = output_sink_factory
        self.registry = ExecutionRegistry()

    def register(self, manifest, implementation, *, implementation_id):
        self.registry.register(
            manifest, implementation, implementation_id=implementation_id
        )
        return self

    def build(self) -> HandlerExecutor:
        self.registry.seal()
        self.preflight()
        return HandlerExecutor(
            self.registry, self.schemas, secret_values=self.secret_values,
            output_sink_factory=self.output_sink_factory,
        )

    def preflight(self) -> None:
        missing = []
        for entry in self.registry.entries():
            for name in entry.manifest.required_secrets:
                if name not in self.secret_values:
                    missing.append(f"{entry.manifest.name}@{entry.manifest.version}:{name}")
            schema_ids = {
                entry.manifest.result_schema_id,
                *entry.manifest.inputs.values(), *entry.manifest.outputs.values(),
            }
            for schema_id in schema_ids:
                if self.schemas.get(schema_id) is None:
                    missing.append(
                        f"{entry.manifest.name}@{entry.manifest.version}:schema:{schema_id}"
                    )
            preflight = getattr(entry.implementation, "preflight", None)
            if preflight is not None:
                try: preflight()
                except Exception as exc:
                    missing.append(
                        f"{entry.manifest.name}@{entry.manifest.version}:implementation:{type(exc).__name__}"
                    )
        if missing:
            raise RuntimeError("Handler runtime preflight failed: " + ", ".join(sorted(missing)))

    def summary(self) -> HandlerRegistrySummary:
        entries = self.registry.entries()
        details = tuple(
            HandlerDetail(
                item.manifest.name, item.manifest.version,
                item.manifest.fingerprint, item.implementation_id,
                item.implementation_fingerprint,
                item.manifest.execution_safety.value,
                item.manifest.capabilities, item.manifest.required_secrets,
                item.manifest.resource_profile.max_duration_seconds,
            )
            for item in entries
        )
        return HandlerRegistrySummary(self.registry.fingerprint, len(details), details)
