"""Ask a model for a workflow as a typed object instead of prose to salvage.

`TrustedCliDslGenerator` runs an installed Agent CLI and hands back whatever it
printed; the service then goes looking for a JSON object inside it — a fence to
strip, braces to locate, a trailing comma to repair. Each of those exists
because a model answered in prose that happened to contain a document, and each
is a place a correct answer can still be lost.

This generator asks a model that supports structured output to fill
`AuthoredWorkflow` directly. A reply that does not fit the type is a validation
error the client retries against the model, before Orbit sees it at all; a
reply that does fit needs no salvaging, because it was never text.

It satisfies the same `(prompt: str) -> str` contract as the CLI generator and
registers in the same `generators` mapping, so the funnel, the compile-feedback
retry loop, the diagnostics and the publish path are all unchanged. What it
replaces is the extraction step, not the pipeline.

pydantic-ai is an optional dependency. It is imported when a generator is
constructed, never at module import, so a Runtime that never asks for one keeps
Orbit's install as shallow as it was. Absent, construction fails with
`AuthoringUnavailableError` — the same answer the service already gives for a
CLI that will not run.

Unlike the CLI path, this one talks to a model API directly and carries no
tool-execution surface: writing a workflow definition needs a model that can
emit JSON, not an agent that can read the filesystem.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from ..dsl.models import AuthoredWorkflow
from ..dsl.patch import (
    AddNode, GraphPatch, PatchError, RemoveNode, SetNodeConfig, SetNodeLabel,
    SetNodePolicies, SetNodePorts, apply_patch,
)
from .generator import (
    MAX_CHANGE_ENTRIES, MAX_CHANGE_TEXT, AuthoringFailedError,
    AuthoringUnavailableError, AuthoringUnknownResultError, active_scope,
)


# Kept in step with `generator.CHANGE_KINDS`, which the service filters against
# after the fact; a summary entry this type admits and that one drops would be
# silently discarded. The two are pinned together by test.
ChangeKind = Literal["added", "removed", "changed"]

# Operations that change a node in place, for the derived summary.
NODE_EDITS = (SetNodeLabel, SetNodeConfig, SetNodePorts, SetNodePolicies)


class ChangeEntry(BaseModel):
    """One line of "what I changed", in the shape the reviewer UI renders."""

    model_config = ConfigDict(extra="forbid")

    kind: ChangeKind
    node_id: str = Field(min_length=1, max_length=MAX_CHANGE_TEXT)
    label: str = Field(min_length=1, max_length=MAX_CHANGE_TEXT)
    detail: str | None = Field(default=None, max_length=MAX_CHANGE_TEXT)


class GeneratedWorkflow(BaseModel):
    """A workflow, and optionally what changed to arrive at it.

    The service accepts either a bare DSL document or a `{workflow,
    change_summary}` envelope, and distinguishes them by whether `dsl_version`
    sits at the top level. Emitting the envelope only when there is a summary
    to carry keeps a plain generation looking exactly like the CLI path's
    output.
    """

    model_config = ConfigDict(extra="forbid")

    workflow: AuthoredWorkflow
    change_summary: list[ChangeEntry] = Field(
        default_factory=list, max_length=MAX_CHANGE_ENTRIES
    )

    def to_response(self) -> dict[str, Any]:
        document = self.workflow.to_dsl_document()
        if not self.change_summary:
            return document
        return {
            "workflow": document,
            "change_summary": [
                entry.model_dump(mode="json", exclude_none=True)
                for entry in self.change_summary
            ],
        }


def _default_revision_factory(model: Any, instructions: str | None) -> Any:
    """An Agent bound to `WorkflowRevision`, told to emit only operations."""

    return _build_agent(
        model, WorkflowRevision,
        "\n\n".join(filter(None, (instructions, REVISION_INSTRUCTIONS))),
    )


def _default_agent_factory(model: Any, instructions: str | None) -> Any:
    """Build a pydantic-ai Agent bound to `GeneratedWorkflow`.

    Imported here rather than at module scope: pydantic-ai is optional, and a
    Runtime that never configures a structured generator must not pay for it.
    """

    return _build_agent(model, GeneratedWorkflow, instructions)


def _build_agent(model: Any, output_type: Any, instructions: str | None) -> Any:
    try:
        from pydantic_ai import Agent
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise AuthoringUnavailableError(
            "structured generation needs the optional 'pydantic-ai' dependency; "
            "install orbit[structured-authoring] or use a trusted Agent CLI"
        ) from exc
    return Agent(model, output_type=output_type, instructions=instructions)


class StructuredDslGenerator:
    """One structured model call per generation request.

    `agent_factory` is the seam the CLI generator gives to `runner`: tests
    supply a stub with the same `run_sync(prompt).output` shape, so the
    envelope, the error mapping and the funnel integration are all exercised
    without the optional dependency or a network.
    """

    def __init__(
        self,
        model: Any,
        *,
        instructions: str | None = None,
        agent_factory: Callable[[Any, str | None], Any] = _default_agent_factory,
        model_settings: Any = None,
        revision_factory: Callable[[Any, str | None], Any] | None = None,
    ) -> None:
        if model is None or (isinstance(model, str) and not model.strip()):
            raise ValueError("a structured generator model is required")
        self.model = model
        self.instructions = instructions
        self.model_settings = model_settings
        # Built now, not per call: a missing optional dependency or a
        # misspelled model name should be refused when the Runtime is composed,
        # not minutes later when a queued job finally runs.
        self.agent = agent_factory(model, instructions)
        # Built when a revision is first asked for. Constructing the
        # generation agent above has already proved the optional dependency is
        # installed and the model name resolves, so deferring this defers
        # nothing a caller could have been told earlier.
        self._revision_factory = revision_factory or _default_revision_factory
        self._instructions = instructions
        self._reviser: Any = None

    def revise(self, prompt: str, base: Mapping[str, Any]) -> str:
        """A revision of `base`, described by the model as operations.

        Returns the resulting document, because that is what the funnel
        compiles and what the retry loop feeds diagnostics back about. What
        the *model* produced is a patch, and that is where the guarantee is:
        a part of the workflow it did not name is a part it could not change.
        """

        if self._reviser is None:
            self._reviser = self._revision_factory(self.model, self._instructions)
        result = self._run(self._reviser, prompt)
        revision = getattr(result, "output", None)
        if not isinstance(revision, WorkflowRevision):
            raise AuthoringFailedError(
                f"structured revision returned {type(revision).__name__}, not a patch"
            )
        try:
            document = apply_patch(base, revision.patch)
        except PatchError as exc:
            # A refusal the model can act on: it names the operation, so the
            # funnel's next attempt can fix that one rather than start again.
            raise AuthoringFailedError(str(exc)) from None
        summary = _summarise(revision.patch, base)
        return json.dumps(
            {"workflow": document, "change_summary": summary} if summary else document
        )

    def _run(self, agent: Any, prompt: str) -> Any:
        scope = active_scope()
        if scope is not None and scope.cancelled:
            raise AuthoringUnknownResultError(
                "structured generation was stopped before it began"
            )
        try:
            result = (
                agent.run_sync(prompt) if self.model_settings is None
                else agent.run_sync(prompt, model_settings=self.model_settings)
            )
        except Exception as exc:
            raise self._translate(exc) from None
        if scope is not None and scope.cancelled:
            raise AuthoringUnknownResultError(
                "structured generation was stopped mid-call"
            )
        return result

    def __call__(self, prompt: str) -> str:
        result = self._run(self.agent, prompt)
        output = getattr(result, "output", None)
        if not isinstance(output, GeneratedWorkflow):
            raise AuthoringFailedError(
                "structured generation returned "
                f"{type(output).__name__}, not a workflow"
            )
        return json.dumps(output.to_response())

    @staticmethod
    def _translate(exc: Exception) -> Exception:
        """Map a client failure onto the taxonomy the service already routes.

        The distinction that matters is whether a model was reached. Nothing
        sent is `unavailable` and safe to retry; sent-then-lost is
        `unknown`, because the request was charged and may have been served.
        A reply that arrived and was rejected is a plain failure.
        """

        name = type(exc).__name__
        if name in {"UsageLimitExceeded", "UnexpectedModelBehavior"}:
            return AuthoringFailedError(f"{name}: {exc}")
        if name in {"ModelHTTPError", "ModelRetry", "FallbackExceptionGroup"}:
            return AuthoringUnknownResultError(f"{name}: {exc}")
        if isinstance(exc, (TimeoutError, KeyboardInterrupt)):
            return AuthoringUnknownResultError(f"structured generation lost: {exc}")
        if isinstance(exc, (ImportError, ValueError, LookupError)):
            return AuthoringUnavailableError(f"structured generator cannot run: {exc}")
        return AuthoringUnknownResultError(f"{name}: {exc}")


def structured_generators(
    models: Sequence[tuple[str, Any]], *, instructions: str | None = None, **kwargs
) -> dict[str, StructuredDslGenerator]:
    """Named structured generators for the service's `generators` mapping.

    Each entry is `(name, model)`; the name is what a caller selects and what
    the UI shows, exactly as a discovered Agent CLI's name is.
    """

    built: dict[str, StructuredDslGenerator] = {}
    for name, model in models:
        key = str(name).strip()
        if not key:
            raise ValueError("a structured generator name is required")
        if key in built:
            raise ValueError(f"duplicate structured generator name: {key!r}")
        built[key] = StructuredDslGenerator(
            model, instructions=instructions, **kwargs
        )
    return built


REVISION_INSTRUCTIONS = (
    "You are revising an existing workflow. Return the smallest set of "
    "operations that carries out the instruction, and nothing else: an "
    "operation you do not emit is a part of the workflow you cannot change. "
    "Name every node, edge and policy by its id. Do not restate parts of the "
    "workflow that are already correct."
)


class WorkflowRevision(BaseModel):
    """The operations a revision is made of.

    A patch rather than a rewritten document, because a model handed the whole
    definition is free to change parts nobody asked about, and an operation it
    does not emit is a part it cannot reach.
    """

    model_config = ConfigDict(extra="forbid")

    patch: GraphPatch


def _summarise(patch: GraphPatch, base: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What the operations did, read off the operations.

    Derived rather than declared. The CLI path asks a model to say what it
    changed alongside the document it changed, which is a second claim that
    can disagree with the first; operations are the change, so a summary read
    from them cannot describe an edit that did not happen.

    Node-scoped only, because that is the shape the reviewer renders.
    """

    labels = {
        node.get("id"): node.get("label") or node.get("id")
        for node in base.get("nodes", []) or []
        if isinstance(node, Mapping)
    }
    entries: list[dict[str, Any]] = []
    for operation in patch.operations:
        kind, node_id, label = None, None, None
        if isinstance(operation, AddNode):
            kind, node_id = "added", operation.node.id
            label = operation.node.label or operation.node.id
        elif isinstance(operation, RemoveNode):
            kind, node_id = "removed", operation.node_id
            label = labels.get(operation.node_id, operation.node_id)
        elif isinstance(operation, NODE_EDITS):
            kind, node_id = "changed", operation.node_id
            label = labels.get(operation.node_id, operation.node_id)
        if kind and node_id and label:
            entries.append({"kind": kind, "node_id": node_id, "label": label})
    # One node touched twice is one change to read about.
    seen: set[tuple[str, str]] = set()
    unique = []
    for entry in entries:
        key = (entry["kind"], entry["node_id"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique
