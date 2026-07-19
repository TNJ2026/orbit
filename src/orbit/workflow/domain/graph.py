"""Stable contracts for deterministic static Graph execution 1.2.

This module contains facts and policies only.  It deliberately has no
repository, clock, evaluator, or runtime dependency so the same values can be
used by the compiler, Kernel, replay, and diagnostics layers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
from types import MappingProxyType
from typing import Any, Mapping

from .errors import ErrorCategory
from .ids import EntityId
from .serialization import canonical_json, freeze_json
from .versions import DefinitionHash, Revision, SchemaVersion


GRAPH_SCHEMA_VERSION = SchemaVersion("1.2")


class GraphNodeKind(str, Enum):
    ACTION = "action"
    HUMAN = "human"
    DECISION = "decision"
    JOIN = "join"
    TERMINAL = "terminal"


class EdgeRoute(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCEL = "cancel"


class RouteMode(str, Enum):
    EXCLUSIVE = "exclusive"
    PARALLEL = "parallel"


class JoinMode(str, Enum):
    ALL = "all"
    ANY = "any"
    N_OF_M = "n_of_m"
    ALL_SUCCESSFUL = "all_successful"
    DEADLINE = "deadline"


class JoinMergeMode(str, Enum):
    SINGLE = "single"
    ARRAY_BY_EDGE = "array_by_edge"
    OBJECT_BY_EDGE = "object_by_edge"
    FIRST_BY_PRIORITY = "first_by_priority"


class JoinDisposition(str, Enum):
    WAIT = "wait"
    OPEN = "open"
    FAIL = "fail"
    TIMED_OUT = "timed_out"


class CompletionDisposition(str, Enum):
    CONTINUE = "continue"
    WAIT = "wait"
    SUCCEED = "succeed"
    FAIL = "fail"


class ExhaustionAction(str, Enum):
    FAIL = "fail"
    ERROR_ROUTE = "error_route"


class FailureResolution(str, Enum):
    UNKNOWN_WAIT = "unknown_wait"
    RETRY = "retry"
    ROUTE = "route"
    TERMINATE = "terminate"


# This order is contractual.  UNKNOWN is intentionally outside all automatic
# recovery paths; a later HumanTask may resolve it by creating a new Attempt.
FAILURE_RESOLUTION_PRECEDENCE = (
    FailureResolution.UNKNOWN_WAIT,
    FailureResolution.RETRY,
    FailureResolution.ROUTE,
    FailureResolution.TERMINATE,
)


def _required_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")


def _positive(value: int, field: str, *, allow_zero: bool = False) -> None:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{field} must be a {qualifier} integer")


def _unique(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    normalized = tuple(values)
    for value in normalized:
        _required_text(value, field)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field} must not contain duplicates")
    return normalized


def _expect(identifier: EntityId, kind: str, field: str) -> None:
    if not isinstance(identifier, EntityId) or identifier.kind != kind:
        raise ValueError(f"{field} must be a {kind} id")


def _expect_type(value: Any, expected: type, field: str) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"{field} must be {expected.__name__}")


def _graph_id(kind: str, payload: Mapping[str, Any]) -> EntityId:
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return EntityId(kind, digest)


def derive_branch_token_id(
    run_id: EntityId,
    plan_version: Revision,
    edge_id: str,
    source_generation: int,
    activation_key: str,
) -> EntityId:
    _expect(run_id, "run", "run_id")
    _expect_type(plan_version, Revision, "plan_version")
    _required_text(edge_id, "edge_id")
    _positive(source_generation, "source_generation")
    _required_text(activation_key, "activation_key")
    return _graph_id(
        "branch_token",
        {
            "run_id": str(run_id),
            "plan_version": plan_version.value,
            "edge_id": edge_id,
            "source_generation": source_generation,
            "activation_key": activation_key,
        },
    )


def derive_join_group_id(
    run_id: EntityId, plan_version: Revision, node_id: str, generation: int,
) -> EntityId:
    _expect(run_id, "run", "run_id")
    _expect_type(plan_version, Revision, "plan_version")
    _required_text(node_id, "node_id")
    _positive(generation, "generation")
    return _graph_id(
        "join_group",
        {
            "run_id": str(run_id),
            "plan_version": plan_version.value,
            "node_id": node_id,
            "generation": generation,
        },
    )


def derive_graph_node_run_id(
    run_id: EntityId,
    plan_version: Revision,
    node_id: str,
    generation: int,
    activation_key: str,
) -> EntityId:
    _expect(run_id, "run", "run_id")
    _expect_type(plan_version, Revision, "plan_version")
    _required_text(node_id, "node_id")
    _positive(generation, "generation")
    _required_text(activation_key, "activation_key")
    return _graph_id(
        "node_run",
        {
            "run_id": str(run_id),
            "plan_version": plan_version.value,
            "node_id": node_id,
            "generation": generation,
            "activation_key": activation_key,
        },
    )


@dataclass(frozen=True)
class TokenScope:
    plan_version: Revision
    edge_id: str
    target_node_id: str
    target_generation: int
    branch_group: str | None = None

    def __post_init__(self) -> None:
        _expect_type(self.plan_version, Revision, "plan_version")
        _required_text(self.edge_id, "edge_id")
        _required_text(self.target_node_id, "target_node_id")
        _positive(self.target_generation, "target_generation")
        if self.branch_group is not None:
            _required_text(self.branch_group, "branch_group")


@dataclass(frozen=True)
class PlanEdge:
    edge_id: str
    source_node_id: str
    target_node_id: str
    route: EdgeRoute = EdgeRoute.SUCCESS
    priority: int = 0
    source_port: str | None = None
    target_port: str | None = None
    condition: Any = None
    mapping: Any = None
    back_edge: bool = False
    policy_ref: str | None = None

    def __post_init__(self) -> None:
        _expect_type(self.route, EdgeRoute, "route")
        _required_text(self.edge_id, "edge_id")
        _required_text(self.source_node_id, "source_node_id")
        _required_text(self.target_node_id, "target_node_id")
        if self.source_node_id == self.target_node_id and not self.back_edge:
            raise ValueError("self edge must be an explicit back edge")
        _positive(self.priority, "priority", allow_zero=True)
        for field, value in (
            ("source_port", self.source_port),
            ("target_port", self.target_port),
            ("policy_ref", self.policy_ref),
        ):
            if value is not None:
                _required_text(value, field)
        object.__setattr__(self, "condition", freeze_json(self.condition))
        object.__setattr__(self, "mapping", freeze_json(self.mapping))


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    backoff_seconds: tuple[int, ...] = ()
    categories: tuple[ErrorCategory, ...] = (
        ErrorCategory.TRANSIENT_ERROR,
        ErrorCategory.TIMEOUT,
        ErrorCategory.LOST,
    )

    def __post_init__(self) -> None:
        _positive(self.max_attempts, "max_attempts")
        backoff = tuple(self.backoff_seconds)
        if len(backoff) > max(0, self.max_attempts - 1):
            raise ValueError("backoff_seconds cannot exceed available retries")
        for delay in backoff:
            _positive(delay, "backoff delay", allow_zero=True)
        categories = tuple(self.categories)
        for category in categories:
            _expect_type(category, ErrorCategory, "retry category")
        if ErrorCategory.UNKNOWN_EXTERNAL_RESULT in categories:
            raise ValueError("unknown external result cannot be retried automatically")
        if len(set(categories)) != len(categories):
            raise ValueError("retry categories must be unique")
        object.__setattr__(self, "backoff_seconds", backoff)
        object.__setattr__(self, "categories", categories)


@dataclass(frozen=True)
class ReworkPolicy:
    max_generations: int
    exhaustion: ExhaustionAction = ExhaustionAction.FAIL

    def __post_init__(self) -> None:
        _expect_type(self.exhaustion, ExhaustionAction, "exhaustion")
        _positive(self.max_generations, "max_generations")


@dataclass(frozen=True)
class LoopPolicy:
    max_iterations: int
    exhaustion: ExhaustionAction = ExhaustionAction.FAIL

    def __post_init__(self) -> None:
        _expect_type(self.exhaustion, ExhaustionAction, "exhaustion")
        _positive(self.max_iterations, "max_iterations")


@dataclass(frozen=True)
class JoinPolicy:
    mode: JoinMode
    merge_mode: JoinMergeMode
    threshold: int | None = None
    deadline_seconds: int | None = None
    min_successful: int | None = None

    def __post_init__(self) -> None:
        _expect_type(self.mode, JoinMode, "mode")
        _expect_type(self.merge_mode, JoinMergeMode, "merge_mode")
        if self.mode is JoinMode.N_OF_M:
            if self.threshold is None:
                raise ValueError("n_of_m join requires threshold")
            _positive(self.threshold, "threshold")
        elif self.threshold is not None:
            raise ValueError("threshold is only valid for n_of_m join")
        if self.mode is JoinMode.DEADLINE:
            if self.deadline_seconds is None or self.min_successful is None:
                raise ValueError("deadline join requires deadline_seconds and min_successful")
            _positive(self.deadline_seconds, "deadline_seconds")
            _positive(self.min_successful, "min_successful")
        elif self.deadline_seconds is not None or self.min_successful is not None:
            raise ValueError("deadline fields are only valid for deadline join")


@dataclass(frozen=True)
class RouteDecision:
    node_run_id: EntityId
    route: EdgeRoute
    mode: RouteMode
    evaluated_edge_ids: tuple[str, ...]
    selected_edge_ids: tuple[str, ...]
    not_selected_edge_ids: tuple[str, ...]
    context_hash: DefinitionHash

    def __post_init__(self) -> None:
        _expect(self.node_run_id, "node_run", "node_run_id")
        _expect_type(self.route, EdgeRoute, "route")
        _expect_type(self.mode, RouteMode, "mode")
        evaluated = _unique(self.evaluated_edge_ids, "evaluated_edge_ids")
        selected = _unique(self.selected_edge_ids, "selected_edge_ids")
        rejected = _unique(self.not_selected_edge_ids, "not_selected_edge_ids")
        if set(selected) & set(rejected):
            raise ValueError("selected and not-selected edges must be disjoint")
        if set(selected) | set(rejected) != set(evaluated):
            raise ValueError("route decision must partition evaluated edges")
        if self.mode is RouteMode.EXCLUSIVE and len(selected) > 1:
            raise ValueError("exclusive route selects at most one edge")
        object.__setattr__(self, "evaluated_edge_ids", evaluated)
        object.__setattr__(self, "selected_edge_ids", selected)
        object.__setattr__(self, "not_selected_edge_ids", rejected)


@dataclass(frozen=True)
class JoinDecision:
    join_group_id: EntityId
    disposition: JoinDisposition
    participant_edge_ids: tuple[str, ...]
    settled_edge_ids: tuple[str, ...]
    winner_edge_ids: tuple[str, ...]
    ignored_edge_ids: tuple[str, ...]
    merged_input_hash: DefinitionHash | None = None

    def __post_init__(self) -> None:
        _expect(self.join_group_id, "join_group", "join_group_id")
        _expect_type(self.disposition, JoinDisposition, "disposition")
        participants = _unique(self.participant_edge_ids, "participant_edge_ids")
        settled = _unique(self.settled_edge_ids, "settled_edge_ids")
        winners = _unique(self.winner_edge_ids, "winner_edge_ids")
        ignored = _unique(self.ignored_edge_ids, "ignored_edge_ids")
        participant_set = set(participants)
        if not set(settled).issubset(participant_set):
            raise ValueError("settled edges must be join participants")
        if not set(winners).issubset(set(settled)):
            raise ValueError("winner edges must be settled")
        if not set(ignored).issubset(participant_set) or set(winners) & set(ignored):
            raise ValueError("ignored edges must be disjoint join participants")
        if self.disposition is JoinDisposition.WAIT and winners:
            raise ValueError("waiting join cannot have winners")
        if self.disposition is JoinDisposition.OPEN and not winners:
            raise ValueError("open join requires at least one winner")
        if self.merged_input_hash is not None and self.disposition is not JoinDisposition.OPEN:
            raise ValueError("only an open join may have merged input")
        object.__setattr__(self, "participant_edge_ids", participants)
        object.__setattr__(self, "settled_edge_ids", settled)
        object.__setattr__(self, "winner_edge_ids", winners)
        object.__setattr__(self, "ignored_edge_ids", ignored)


@dataclass(frozen=True)
class CompletionDecision:
    disposition: CompletionDisposition
    reason: str
    terminal_node_run_ids: tuple[EntityId, ...] = ()
    active_responsibility_ids: tuple[EntityId, ...] = ()
    waiting_reason: str | None = None

    def __post_init__(self) -> None:
        _expect_type(self.disposition, CompletionDisposition, "disposition")
        _required_text(self.reason, "reason")
        terminals = tuple(self.terminal_node_run_ids)
        responsibilities = tuple(self.active_responsibility_ids)
        for identifier in terminals:
            _expect(identifier, "node_run", "terminal_node_run_ids")
        for identifier in responsibilities:
            if identifier.kind not in {"branch_token", "job", "timer", "attempt", "human_task"}:
                raise ValueError("unsupported active responsibility id")
        if self.disposition is CompletionDisposition.WAIT:
            if self.waiting_reason is None:
                raise ValueError("waiting completion requires waiting_reason")
            _required_text(self.waiting_reason, "waiting_reason")
        elif self.waiting_reason is not None:
            raise ValueError("waiting_reason is only valid for waiting completion")
        object.__setattr__(self, "terminal_node_run_ids", terminals)
        object.__setattr__(self, "active_responsibility_ids", responsibilities)


GRAPH_CONTRACT_TYPES = MappingProxyType(
    {
        "token_scope": TokenScope,
        "plan_edge": PlanEdge,
        "retry_policy": RetryPolicy,
        "rework_policy": ReworkPolicy,
        "loop_policy": LoopPolicy,
        "join_policy": JoinPolicy,
        "route_decision": RouteDecision,
        "join_decision": JoinDecision,
        "completion_decision": CompletionDecision,
    }
)
