"""Versioned read models for the HTTP and MCP adapters.

Every payload leaves through :func:`envelope`, so a caller always gets the
same shape: a schema version it can pin, the projection version the data was
read at, and an opaque cursor when there is more to fetch.

Two rules the UI contract depends on:

* cursors are opaque — the client must not parse or synthesise them;
* actions come from ``allowed_commands`` — the client must not infer a button
  from a status, a kind or a role.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "1.0"

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


class CursorError(ValueError):
    """The supplied cursor is not one this server issued."""


def encode_cursor(payload: Mapping[str, Any]) -> str:
    """Opaque forward cursor. Base64 keeps clients from reading the contents."""

    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> dict[str, Any]:
    if not cursor:
        return {}
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding).decode("utf-8")
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise CursorError("cursor is not valid") from None
    if not isinstance(value, dict):
        raise CursorError("cursor is not valid")
    return value


def page_size(raw: Any, *, default: int = DEFAULT_PAGE_SIZE) -> int:
    if raw is None or raw == "":
        return default
    try:
        size = int(raw)
    except (TypeError, ValueError):
        raise ValueError("limit must be an integer") from None
    if size < 1 or size > MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    return size


def envelope(
    data: Any,
    *,
    projection_version: int | None = None,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "projection_version": projection_version,
        "data": data,
        "next_cursor": next_cursor,
    }


@dataclass(frozen=True)
class AllowedCommand:
    """One action the current actor may take on one responsibility.

    ``expected_version`` belongs to ``target_aggregate_id``, which is not
    necessarily the aggregate that owns the responsibility: a single waiting
    item can offer commands against a HumanTask, the Run and the BudgetAccount.
    """

    command: str
    label: str
    method: str
    href: str
    target_aggregate_id: str
    expected_version: int
    payload_schema: str
    confirmation: str = "explicit"

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "label": self.label,
            "method": self.method,
            "href": self.href,
            "target_aggregate_id": self.target_aggregate_id,
            "expected_version": self.expected_version,
            "payload_schema": self.payload_schema,
            "confirmation": self.confirmation,
        }


@dataclass(frozen=True)
class Responsibility:
    """Something the run is waiting on, plus what may be done about it."""

    responsibility_id: str
    kind: str
    label: str
    status: str
    expected_version: int
    allowed_commands: tuple[AllowedCommand, ...] = ()
    detail: str | None = None
    # Present when the responsibility is a NodeRun the Runtime could not
    # settle: its command acts on that NodeRun, not on this row's own id.
    node_run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "responsibility_id": self.responsibility_id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "expected_version": self.expected_version,
            "node_run_id": self.node_run_id,
            "allowed_commands": [item.to_dict() for item in self.allowed_commands],
        }


def budget_summary(budget: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Budget with its unit attached.

    The unit travels with the numbers so a client can never guess a currency;
    microunits formatted as dollars is exactly the mistake this prevents.
    """

    if budget is None:
        return None
    total = int(budget.get("total", 0))
    reserved = int(budget.get("reserved", 0))
    consumed = int(budget.get("consumed", 0))
    return {
        "total_microunits": total,
        "reserved_microunits": reserved,
        "consumed_microunits": consumed,
        "remaining_microunits": total - reserved - consumed,
        "overrun": consumed > total,
        "unit": budget.get("unit", "microunits"),
    }


def run_summary(
    row: Mapping[str, Any],
    responsibilities: Sequence[Mapping[str, Any]],
    budget: Mapping[str, Any] | None,
    *,
    can_act: bool = False,
) -> dict[str, Any]:
    """List-row shape.

    The primary responsibility is embedded so a run list never has to call the
    diagnostics service once per row — that N+1 is the difference between a
    list that loads and one that does not.
    """

    primary = responsibilities[0] if responsibilities else None
    raw_status = row["status"]
    status = {
        "created": "pending",
        "budget_exhausted": "waiting",
        "waiting_for_budget": "waiting",
    }.get(raw_status, raw_status)
    wait_reason = None
    if raw_status in {"budget_exhausted", "waiting_for_budget"}:
        wait_reason = "budget_wait"
    elif primary is not None:
        if primary["kind"] == "human":
            wait_reason = "human_wait"
        elif primary["kind"] == "timer":
            wait_reason = "timer_wait"
        elif primary["kind"] == "job" and primary.get("status") == "retry_wait":
            wait_reason = "retry_wait"
        elif primary.get("status") == "unknown":
            wait_reason = "unknown_wait"
    return {
        "run_id": row["run_id"],
        "display_name": row.get("display_name") or row["run_id"],
        "workflow_id": row["workflow_id"],
        "workflow_version": row["workflow_version"],
        "status": status,
        "wait_reason": wait_reason,
        "goal": row.get("goal"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "projection_version": row["aggregate_version"],
        "responsibility_count": len(responsibilities),
        "primary_responsibility": (
            None if primary is None
            else {
                "kind": primary["kind"],
                "label": primary.get("label") or primary.get("detail") or primary["kind"],
            }
        ),
        "requires_actor_action": can_act and any(
            item["kind"] in {"human", "budget"} for item in responsibilities
        ),
        "budget_summary": budget_summary(budget),
    }
