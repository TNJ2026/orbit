"""When an attempt stops working, stops running, and must be settled.

One attempt has four moments, not one. The Runtime used to know only the
last of them — ``start + max_duration_seconds`` — and every other component
picked its own interpretation: the supervisor cancelled there, the adapter
waited a constant set once for the whole registry, and settlement happened
whenever the pieces happened to finish. Nothing reserved time for actually
stopping a process, so a node that ran to its budget had none left to be
written off in, and the lease reaper got there first.

The four points, from one place:

* ``node_deadline`` — the node's whole budget. Nothing may run past it.
* ``soft_deadline`` — stop starting new work and summarise. Only meaningful
  for budgets long enough to have a wind-down phase.
* ``process_deadline`` — begin killing the process group.
* ``settlement_deadline`` — the attempt must be in a definite state.

The reserve for killing and settling is taken *out of* the budget rather
than added after it. Appending it would put settlement past the node's own
deadline, which is the same mistake as having no reserve at all: the moment
a caller is told the node's budget is 300s, everything the Runtime does about
that node has to fit inside 300s.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


# A budget below this cannot express "work, then wind down, then be stopped"
# — the reserve would eat most of it. Applied to what an author may configure,
# not to a manifest the Runtime ships.
MIN_AGENT_DURATION_SECONDS = 60
# How long a killed Agent tree gets to die. The platform default of two
# seconds suits a quick command; an Agent CLI is usually mid-write to a
# workspace when it is killed, and its descendants need room to finish the
# write they started rather than leave a half-written file. It lives here
# rather than beside the adapter because it is also subtracted from every
# node's budget — stopping is part of what the budget has to pay for.
AGENT_KILL_GRACE_SECONDS = 10.0
# How many renewals in a row may fail before the lease is treated as lost.
# It is an input to the lease inequality below, so it lives beside it rather
# than only where the supervisor happens to count.
MAX_CONSECUTIVE_RENEWAL_FAILURES = 3
# How long after the worker's own settlement deadline the durable timer waits
# before stepping in. Firing at the same instant would put the timer in a race
# with the settlement it is supposed to be a backstop for: both would write the
# attempt's terminal state, one would lose on the fencing token, and the timer's
# fire count — the metric that is supposed to mean "the worker failed" — would
# be dominated by cases where it did not.
TIMER_GRACE_SECONDS = 10.0
# How long settlement itself may take once the process is gone: writing the
# outcome, the workspace snapshot and the lease release.
SETTLEMENT_MARGIN_SECONDS = 30.0
# Below this there is no wind-down phase worth announcing; the budget is
# spent entirely on the work and then on stopping it.
SOFT_DEADLINE_MIN_DURATION_SECONDS = 600
# Wind-down starts at the later of these: 70% of the budget, or five minutes
# before the end. The proportion is what makes short budgets workable; the
# constant is what stops a very long budget from winding down for hours.
SOFT_DEADLINE_FRACTION = 0.7
SOFT_DEADLINE_LEAD_SECONDS = 300
PROCESS_DEADLINE_FRACTION = 0.85
PROCESS_DEADLINE_LEAD_SECONDS = 120


class UnsafeDeadlineConfiguration(ValueError):
    """A timing configuration that cannot be made to hold, stated plainly."""


@dataclass(frozen=True)
class AttemptDeadlines:
    """The four moments of one attempt, in order."""

    node_deadline: datetime
    process_deadline: datetime
    settlement_deadline: datetime
    # None when the budget is too short for a wind-down phase to mean
    # anything. A caller must not synthesise one: announcing "wrapping up" on
    # a node that was never told to wrap up is a status nobody can act on.
    soft_deadline: datetime | None

    def __post_init__(self) -> None:
        if not (self.process_deadline < self.settlement_deadline <= self.node_deadline):
            raise UnsafeDeadlineConfiguration(
                "attempt deadlines must be ordered "
                "process < settlement <= node"
            )
        if self.soft_deadline is not None and self.soft_deadline >= self.process_deadline:
            raise UnsafeDeadlineConfiguration(
                "the soft deadline must fall before the process deadline"
            )


def cleanup_reserve_seconds(kill_grace_seconds: float) -> float:
    """Time reserved out of the budget for stopping and settling."""

    return float(kill_grace_seconds) + SETTLEMENT_MARGIN_SECONDS


def attempt_deadlines(
    start: datetime, duration_seconds: float, *, kill_grace_seconds: float,
) -> AttemptDeadlines:
    """The four moments for a budget of ``duration_seconds`` starting at ``start``.

    Proportions rather than fixed subtraction. ``max_duration_seconds`` spans
    three orders of magnitude across the shipped handlers — 300s for a builtin,
    900s for a dev tool, 1800s for an agent — so "five minutes before the end"
    lands before the start for the shortest of them. A proportion with a
    constant floor stays ordered for every budget the schema admits.
    """

    reserve = cleanup_reserve_seconds(kill_grace_seconds)
    if duration_seconds <= reserve:
        raise UnsafeDeadlineConfiguration(
            f"a {duration_seconds:g}s budget leaves nothing after the "
            f"{reserve:g}s needed to stop the process and settle the attempt"
        )
    process_offset = min(
        max(
            PROCESS_DEADLINE_FRACTION * duration_seconds,
            duration_seconds - PROCESS_DEADLINE_LEAD_SECONDS,
        ),
        duration_seconds - reserve,
    )
    soft_offset = None
    if duration_seconds >= SOFT_DEADLINE_MIN_DURATION_SECONDS:
        soft_offset = max(
            SOFT_DEADLINE_FRACTION * duration_seconds,
            duration_seconds - SOFT_DEADLINE_LEAD_SECONDS,
        )
    return AttemptDeadlines(
        node_deadline=start + timedelta(seconds=duration_seconds),
        process_deadline=start + timedelta(seconds=process_offset),
        settlement_deadline=start + timedelta(seconds=process_offset + reserve),
        soft_deadline=(
            None if soft_offset is None else start + timedelta(seconds=soft_offset)
        ),
    )


def validate_lease_budget(
    *,
    lease_ttl_seconds: float,
    renew_interval_seconds: float,
    max_consecutive_renewal_failures: int,
    kill_grace_seconds: float,
) -> None:
    """Check that forced settlement fits inside the lease it is settling.

    The lease is a separate axis from the deadlines above: it is not a window
    covering the node's execution but a short one rolled forward every
    ``renew_interval_seconds``, and the kernel caps it absolutely. What has to
    hold is that the whole path from "renewals start failing" to "the attempt
    is settled" completes before the lease expires — otherwise the reaper and
    the worker reach the same attempt from two directions and race for its
    terminal state.
    """

    detection = renew_interval_seconds * max_consecutive_renewal_failures
    needed = detection + cleanup_reserve_seconds(kill_grace_seconds)
    if needed >= lease_ttl_seconds:
        raise UnsafeDeadlineConfiguration(
            f"detecting lost renewals and settling takes up to {needed:g}s, "
            f"which does not fit in a {lease_ttl_seconds:g}s lease; the reaper "
            "would race the worker for the attempt's terminal state"
        )
