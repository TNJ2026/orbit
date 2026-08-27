"""Who may read what a Runtime has done: one rule, for both transports."""

from __future__ import annotations


def reading_actor(_caller: str | None = None) -> None:
    """The owner a read filters by — nobody, always.

    A Runtime serves exactly one Workspace: `serve --project-root` fixes it
    before the first request, and every Run, Artifact and step it holds
    happened there. So the Workspace is the boundary, and it is enforced by
    which Runtime a caller reached rather than by anything inside this one.

    Filtering reads by actor as well put a second, weaker boundary inside that
    one, and the two transports drew it differently: `/mcp` let a caller ask
    for the Workspace's work and the panel did, while `/api/v1` had no way to
    ask and every loopback caller is `local`. One database, thirty-five Runs,
    and Orbit's own UI showing twenty-five of them with no sign that the rest
    existed. Neither surface was wrong about the rule; there were two rules.

    Deliberately not a policy this returns *sometimes*. An actor is still
    recorded on everything, and still scopes every **write** — who cancelled a
    Run is the point of recording who cancelled it, and the single-goal slot is
    per-actor so one caller cannot block another. What it no longer does is
    decide who may look.

    The parameter is accepted and ignored so call sites read as the question
    they are answering rather than as a bare `None` argument.
    """

    return None
