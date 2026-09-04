"""Whether a run needs a shared project workspace, and who says so.

``workspace_access`` is a property of the *run*, not only the declaring node.
Every Agent/App/Harness node in that run gets the same directory: a Run-scoped
worktree for Git projects, or the real project root for non-Git projects.
Deriving this once before execution keeps nodes from changing workspace midway
through a run. Design: docs/project-file-access-design.md.

This module answers two questions and holds nothing else:

    project_access_need(ir)   what the workflow asks for, read from the IR
    ProjectAccessCoordinator  who holds the directory, and until when
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

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
# How long a settled run's way back is kept. A run finishing is not the
# moment somebody stops wanting to undo it — the morning after is a very
# common one — so this is days rather than minutes.
DEFAULT_RECOVERY_RETENTION_SECONDS = 7 * 24 * 60 * 60.0
# The frozen observations kept in memory. The durable copy is the Runtime's
# own `langgraph_project_summaries` row, written as the run settles; this is
# only what lets a second `finalize` in the same settle — and a `summarize`
# between settling and the row being read back — answer without asking git
# again. Bounded, because a Runtime settles runs for as long as it is up.
FINAL_SUMMARY_CACHE_SIZE = 256


class ProjectAccessUnavailable(RuntimeError):
    """The run asked for the project directory and cannot be given it."""


@dataclass(frozen=True)
class ProjectAccessNeed:
    """What a workflow asks of the real project directory."""

    required: bool = False
    write: bool = False
    # Retained in the DTO for stored-run compatibility. The current non-git
    # direct mode has no automatic rollback and does not consume this field.
    protect: tuple[str, ...] = ()
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

    policies = {
        policy.id: policy for policy in getattr(ir, "policies", ())
        if policy.kind == "workspace_access"
    }
    if not policies:
        return ProjectAccessNeed()
    asked = [
        policies[policy_id] for node in ir.nodes
        for policy_id in node.policies if policy_id in policies
    ]
    if not asked:
        return ProjectAccessNeed()
    return ProjectAccessNeed(
        required=True,
        write=any(policy.config.get("mode", "read_write") != "read_only"
                  for policy in asked),
        protect=tuple(dict.fromkeys(
            item for policy in asked for item in policy.config.get("protect", ())
        )),
        agent_nodes=tuple(
            node.id for node in ir.nodes
            if node.handler is not None and (
                node.handler.name.startswith("agent.")
                or node.handler.name in {"app.delegate", "harness.subagent"}
            )
        ),
    )


class UnprotectedDirectRecoveryPoints:
    """Honest recovery metadata for non-git direct access.

    No copy is made: large projects must not be duplicated merely to run an
    Agent. Occupancy still serializes runs, while the status explicitly says
    that rollback is unavailable.
    """

    def __init__(self, project_root: Path | str) -> None:
        self.project_root = Path(project_root).resolve()
        self._points: dict[str, Any] = {}
        self._summaries: dict[str, Mapping[str, Any]] = {}

    def preflight(self, *, protect=()) -> None:
        return None

    def create(self, run_id: str, *, protect=()):
        from ...workspace.recovery import RecoveryPoint
        from datetime import datetime, timezone

        point = RecoveryPoint(
            run_id=run_id, project_root=self.project_root,
            kind="unprotected_direct",
            created_at=datetime.now(timezone.utc).isoformat(),
            uncovered=("the entire non-git project has no automatic rollback",),
        )
        self._points[run_id] = point
        return point

    def finalize(self, run_id: str) -> None:
        if run_id in self._points:
            self._summaries[run_id] = {
                "kind": "unprotected_direct", "scope": "run_cumulative",
                "content": [], "staged": [],
                "error": "non-git direct access has no automatic change summary or rollback",
            }

    def final_summary(self, run_id: str):
        return self._summaries.get(run_id)

    def load(self, run_id: str):
        return self._points.get(run_id)

    def summarize(self, point):
        class Summary:
            def to_primitive(self_nonlocal):
                return {
                    "kind": "unprotected_direct", "scope": "run_cumulative",
                    "content": [], "staged": [],
                    "error": "non-git direct access has no automatic change summary or rollback",
                }
        return Summary()

    def sweep(self, live_run_ids, *, older_than_seconds=0):
        return ()


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
            from ...workspace.git import is_git_repo
            from ...workspace.recovery import (
                FileBackupRecoveryPoints, GitRecoveryPoints,
            )

            # git where git can answer, because its baseline covers the whole
            # project without anybody having to enumerate it. Where it cannot,
            # the only honest alternative is the files the workflow named —
            # see `FileBackupRecoveryPoints` for why a whole-directory copy is
            # not on the list.
            recovery_points = (
                GitRecoveryPoints(self.project_root)
                if is_git_repo(self.project_root)
                else FileBackupRecoveryPoints(
                    self.project_root, self.project_root / ".orbit",
                )
            )
        self.recovery_points = recovery_points
        self._claims: dict[str, ProjectClaim] = {}
        self._final_summaries: OrderedDict[str, Mapping[str, Any]] = OrderedDict()

    def held_by(self, run_id: str) -> bool:
        return run_id in self._claims

    def preflight(self, need: ProjectAccessNeed) -> None:
        """Ask whether this run could have a way back, without making one.

        Asked before the durable Run exists, so a workflow that can never run
        here does not leave one behind — and, in single-goal mode, does not
        leave one occupying the slot. `acquire` asks the same questions again
        for real; this only exists so the answer arrives early enough to be
        useful.
        """

        if not need.required:
            return
        if need.write and not self.write_granted:
            raise ProjectAccessUnavailable(
                "workflow asks to write the project directory but this "
                "Runtime was not started with --agent-project-access"
            )
        check = getattr(self.recovery_points, "preflight", None)
        if check is not None:
            check(protect=need.protect)

    def acquire(self, run_id: str, need: ProjectAccessNeed) -> None:
        """Take the project for this run, or refuse the run outright.

        Idempotent only while this coordinator still owns the claim. A
        restarted process must explicitly resolve an abandoned claim first.
        """

        if not need.required or run_id in self._claims:
            return
        if need.write and not self.write_granted:
            raise ProjectAccessUnavailable(
                "workflow asks to write the project directory but this "
                "Runtime was not started with --agent-project-access"
            )
        claim = self.registry.claim(self.project_root, run_id=run_id)
        # Between holding the project and letting anything run in it — §6.
        # Before the claim is handed out, so a run that cannot be undone is
        # never started: on failure the project goes straight back rather than
        # being held by a run nobody can rescue.
        try:
            point = self.recovery_points.create(run_id, protect=need.protect)
            claim.record_recovery(point.to_primitive())
        except BaseException:
            claim.release()
            raise
        self._claims[run_id] = claim

    def finalize(self, run_id: str, status: str) -> Mapping[str, Any] | None:
        if status in RELEASING_STATUSES and run_id in self._claims:
            if run_id not in self._final_summaries:
                try:
                    self.recovery_points.finalize(run_id)
                    summary = self.recovery_points.final_summary(run_id)
                    if summary is None:
                        raise RuntimeError("final summary missing")
                except Exception as exc:
                    summary = {"kind": "unavailable", "scope": "run_cumulative",
                               "content": [], "staged": [],
                               "error": f"{type(exc).__name__}: {exc}"}
                self._final_summaries[run_id] = summary
                while len(self._final_summaries) > FINAL_SUMMARY_CACHE_SIZE:
                    self._final_summaries.popitem(last=False)
            self._final_summaries.move_to_end(run_id)
            return self._final_summaries[run_id]
        return None

    def release(self, run_id: str, status: str) -> None:
        """Give the project back once the run can no longer touch it."""

        if status not in RELEASING_STATUSES:
            return
        self.finalize(run_id, status)
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

    def sweep_recovery_points(
        self, live_run_ids: Iterable[str], *,
        older_than_seconds: float = DEFAULT_RECOVERY_RETENTION_SECONDS,
    ) -> tuple[str, ...]:
        """Reclaim recovery refs for settled runs past the retention period."""

        return self.recovery_points.sweep(
            live_run_ids, older_than_seconds=older_than_seconds,
        )

    def summarize(self, run_id: str) -> Mapping[str, Any] | None:
        """What this run has changed so far, or None if it holds no project.

        On demand rather than per node, which is §5's rule and not an
        optimisation: the comparison scans the working tree and writes a tree
        object, so a summary after every write node is a full scan per node
        on a large repository.

        Answers for a settled run too, by rebuilding the point from its ref —
        "what did that run change" is asked most often once it is over.
        """

        if run_id in self._final_summaries:
            self._final_summaries.move_to_end(run_id)
            return self._final_summaries[run_id]
        summary = self.recovery_points.final_summary(run_id)
        if summary is not None:
            return summary
        point = self.recovery_points.load(run_id)
        if point is None:
            return None
        if run_id not in self._claims:
            return {"kind": "unavailable", "scope": "run_cumulative",
                    "content": [], "staged": [],
                    "error": "No final observation was saved for this run"}
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
