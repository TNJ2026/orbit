"""Whether a run holds the real project directory, and who says so.

`workspace_access` with `isolation: none` is a property of the *run*, not of
the node that happened to declare it: every Agent in that run works in the
same real directory, so that they can hand each other files the way they do
in a scratch directory today. Deriving it from the whole workflow before the
first node executes — rather than switching directories when execution
reaches the node that asked — is what keeps a run from starting in one place
and finishing in another. Design: docs/project-file-access-design.md §2, §4.

This module answers two questions and holds nothing else:

    project_access_need(ir)   what the workflow asks for, read from the IR
    ProjectAccessCoordinator  who holds the directory, and until when
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ...platform.project_occupancy import (
    ProjectClaim, ProjectOccupancyRegistry,
)


# Terminal for the purposes of the project: nothing more will run, so the
# directory can go back. `unknown` is deliberately absent — it means nobody
# can say whether the Handler acted, and by extension whether the Agent
# subprocess it started is still writing. Releasing on that would hand the
# project to the next run while a process may still be editing its files;
# the claim stays, and §4's recovery path is what clears it.
RELEASING_STATUSES = frozenset({"completed", "failed", "cancelled"})


class ProjectAccessUnavailable(RuntimeError):
    """The run asked for the project directory and cannot be given it."""


@dataclass(frozen=True)
class ProjectAccessNeed:
    """What a workflow asks of the real project directory."""

    required: bool = False
    write: bool = False
    # The Agent nodes that will work in the directory. Under `isolation:
    # none` that is *every* Agent node in the workflow, not only the ones
    # carrying the policy — see §2.
    agent_nodes: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.required


def project_access_need(ir: Any) -> ProjectAccessNeed:
    """Read the run's project access off the workflow, once, before it starts.

    A node reference is what asks, but the answer is run-wide: if any node
    asks for the project directory, every Agent node in the run works there.
    """

    direct = {
        policy.id: policy for policy in getattr(ir, "policies", ())
        if policy.kind == "workspace_access"
        and (policy.config or {}).get("isolation") == "none"
    }
    if not direct:
        return ProjectAccessNeed()
    asked = [
        direct[policy_id] for node in ir.nodes
        for policy_id in node.policies if policy_id in direct
    ]
    if not asked:
        return ProjectAccessNeed()
    return ProjectAccessNeed(
        required=True,
        write=any(
            (policy.config or {}).get("mode") == "read_write" for policy in asked
        ),
        agent_nodes=tuple(
            node.id for node in ir.nodes
            if node.handler is not None and node.handler.name.startswith("agent.")
        ),
    )


class ProjectAccessCoordinator:
    """Holds the project directory for the runs that need it.

    One per Runtime. A claim is taken before the run's first node executes and
    kept for the whole run — including while it waits for a person, which is
    a deliberate choice (§4): a project stays occupied until its run settles,
    and how long that takes is the answering human's to decide.
    """

    def __init__(
        self,
        project_root: Path | str,
        *,
        registry: ProjectOccupancyRegistry | None = None,
        write_granted: bool = False,
        recovery_points: Any = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.registry = registry or ProjectOccupancyRegistry()
        self.write_granted = write_granted
        # Where this run can be put back from, established between taking the
        # project and running anything in it. `None` disables it, which is for
        # tests: a real Runtime builds one, because §6.2 refuses to run a
        # workflow whose recovery point cannot be established rather than
        # running it unprotected.
        if recovery_points is None:
            from ...workspace.recovery import GitRecoveryPoints

            recovery_points = GitRecoveryPoints(self.project_root)
        self.recovery_points = recovery_points
        self._claims: dict[str, ProjectClaim] = {}

    def held_by(self, run_id: str) -> bool:
        return run_id in self._claims

    def acquire(self, run_id: str, need: ProjectAccessNeed) -> None:
        """Take the project for this run, or refuse the run outright.

        Idempotent for one run: a resumed or recovered run re-enters through
        here, and the registry treats a claim by the same run id as the claim
        it already has rather than as a competitor.
        """

        if not need.required or run_id in self._claims:
            return
        if need.write and not self.write_granted:
            raise ProjectAccessUnavailable(
                "workflow asks to write the project directory but this "
                "Runtime was not started with --agent-project-write"
            )
        claim = self.registry.claim(self.project_root, run_id=run_id)
        # Between holding the project and letting anything run in it — §6.
        # Before the claim is handed out, so a run that cannot be undone is
        # never started: on failure the project goes straight back rather than
        # being held by a run nobody can rescue.
        try:
            point = self.recovery_points.create(run_id)
        except BaseException:
            claim.release()
            raise
        claim.record_recovery(point.to_primitive())
        self._claims[run_id] = claim

    def release(self, run_id: str, status: str) -> None:
        """Give the project back once the run can no longer touch it."""

        if status not in RELEASING_STATUSES:
            return
        claim = self._claims.pop(run_id, None)
        if claim is not None:
            claim.release()

    def abandon(self, run_id: str) -> None:
        """Drop this process's hold without settling the run.

        For shutdown: the record stays, so a Runtime that stops with runs in
        flight leaves exactly what §4 says it should — a claim the next
        Runtime must resolve rather than step over.
        """

        claim = self._claims.pop(run_id, None)
        if claim is not None:
            claim._lock.release()  # noqa: SLF001 - record stays on purpose

    def summarize(self, run_id: str) -> Mapping[str, Any] | None:
        """What this run has changed so far, or None if it holds no project.

        On demand rather than per node, which is §5's rule and not an
        optimisation: the comparison scans the working tree and writes a tree
        object, so a summary after every write node is a full scan per node
        on a large repository.

        Answers for a settled run too, by rebuilding the point from its ref —
        "what did that run change" is asked most often once it is over.
        """

        point = self.recovery_points.load(run_id)
        if point is None:
            return None
        return self.recovery_points.summarize(point).to_primitive()

    def status(self, run_id: str) -> Mapping[str, Any] | None:
        claim = self._claims.get(run_id)
        if claim is None:
            return None
        return {
            "run_id": run_id,
            "project_root": str(claim.path),
            "write_granted": self.write_granted,
            # §7 wants the coverage and the gaps on the page, not only the
            # fact that a recovery point exists.
            "recovery": claim.occupancy.recovery,
        }
