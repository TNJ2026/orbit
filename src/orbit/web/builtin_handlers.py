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

from typing import Any, Mapping, Sequence

from ..workflow.catalogs import HandlerManifest
from ..workflow.domain.durable_execution import ExecutionSafety
from ..workflow.domain.handlers import ResourceProfile
from ..workflow.handlers import TransformHandler
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
