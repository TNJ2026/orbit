"""Who may act on what a Runtime has done: one rule, for both transports."""

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
    recorded on everything; what it no longer does is decide who may look.

    The parameter is accepted and ignored so call sites read as the question
    they are answering rather than as a bare `None` argument.
    """

    return None


def writing_actor(_caller: str | None = None) -> None:
    """The owner a write filters by — nobody either, and for the same reason.

    Ownership used to gate `resume` and `cancel` while reads had already been
    widened to the Workspace. The panel is a view of the Workspace, so it drew
    Runs from Sessions that had ended and offered their advertised commands;
    pressing one asked a Runtime that answers "not found" for a Run it does not
    own, and a Run whose Session was gone could not be answered by anybody. It
    was reachable in one afternoon of ordinary use, and the only way out was
    editing SQLite.

    So the Workspace is the boundary for acting on a Run as well as for seeing
    it — one rule, the one enforced by which Runtime a caller reached. This is
    a separate name from `reading_actor` because they are separate decisions
    that happen to agree: a later build may well want a write to be refused
    where a read is not, and it should have to change this function to do it.

    What is *not* affected: who a Run belongs to is still recorded at `start`,
    still reported, and every mutation still goes through the same optimistic
    revision check it always did.
    """

    return None
