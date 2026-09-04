from __future__ import annotations
import tempfile, unittest
from pathlib import Path

from orbit.platform.project_occupancy import ProjectOccupancyRegistry, ProjectBusy
from orbit.workflow.langgraph_runtime.project_access import (
    ProjectAccessCoordinator, ProjectAccessNeed, ProjectAccessUnavailable,
    RELEASING_STATUSES, project_access_need,
)
from orbit.workflow.domain.definitions import (
    IRHandlerRef, IRNode, IRPolicy, IRPort, IRResult, WorkflowIR,
)

SCHEMA = "example://value/1.0"
FP = "sha256:" + "a" * 64

def port(name): return IRPort(name, SCHEMA, True, False, None, "")

def node(nid, *, handler="agent.opencode", policies=()):
    ref = None if handler is None else IRHandlerRef(handler, "1.0.0", FP)
    return IRNode(nid, "action", (port("prompt"),), (port("result"),), ref, {}, tuple(policies), None, None)

def ir(nodes, policies=()):
    return WorkflowIR("1.3","workflow:t","T","",{},(port("value"),),(),tuple(nodes),(),
                      (nodes[0].id,),(nodes[-1].id,),tuple(policies),(),{},IRResult(nodes[0].id,"result"))

DIRECT = IRPolicy("pa","workspace_access",{"mode":"read_write","isolation":"none"})
DIRECT_RO = IRPolicy("pa","workspace_access",{"mode":"read_only","isolation":"none"})
COPY = IRPolicy("pa","workspace_access",{"mode":"read_only","isolation":"worktree"})

class NeedTests(unittest.TestCase):
    def test_no_policy_needs_nothing(self):
        self.assertFalse(project_access_need(ir([node("a")])))

    def test_git_worktree_access_is_run_wide(self):
        need = project_access_need(
            ir([node("a", policies=("pa",))], [COPY]), direct=False,
        )
        self.assertTrue(need.required)
        self.assertEqual(("a",), need.agent_nodes)

    def test_isolation_none_is_required_and_write(self):
        need = project_access_need(ir([node("a", policies=("pa",))], [DIRECT]))
        self.assertTrue(need.required); self.assertTrue(need.write)

    def test_non_git_access_is_always_full_write_access(self):
        need = project_access_need(ir([node("a", policies=("pa",))], [DIRECT_RO]))
        self.assertTrue(need.required); self.assertTrue(need.write)

    def test_every_agent_node_gets_the_directory_not_just_the_declaring_one(self):
        need = project_access_need(ir(
            [node("declares", policies=("pa",)), node("silent"), node("t", handler=None)],
            [DIRECT]))
        self.assertEqual(("declares","silent"), need.agent_nodes)

    def test_a_policy_nobody_references_asks_for_nothing(self):
        self.assertFalse(project_access_need(ir([node("a")], [DIRECT])))

class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        import subprocess
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.project = self.root/"project"; self.project.mkdir()
        # A git project: acquiring one establishes a recovery point first
        # (§6), and a project that cannot have one is refused rather than run
        # unprotected — see `test_a_non_git_project_is_refused`.
        for argv in (("git","init","--initial-branch=main"),
                     ("git","config","user.email","t@e.com"),
                     ("git","config","user.name","T")):
            subprocess.run(argv, cwd=self.project, capture_output=True, check=True)
        (self.project/"seed.txt").write_text("seed\n")
        subprocess.run(("git","add","-A"), cwd=self.project, capture_output=True, check=True)
        subprocess.run(("git","commit","-m","init"), cwd=self.project, capture_output=True, check=True)
        self.registry = ProjectOccupancyRegistry(self.root/"occ")

    def coord(self, *, write=True):
        return ProjectAccessCoordinator(self.project, registry=self.registry, write_granted=write)

    def test_acquiring_nothing_when_not_needed(self):
        c = self.coord(); c.acquire("r1", ProjectAccessNeed())
        self.assertFalse(c.held_by("r1")); self.assertEqual((), self.registry.occupancies())

    def test_write_needs_the_operator_switch(self):
        c = self.coord(write=False)
        with self.assertRaises(ProjectAccessUnavailable):
            c.acquire("r1", ProjectAccessNeed(required=True, write=True))

    def test_read_only_direct_access_does_not_need_the_write_switch(self):
        c = self.coord(write=False)
        c.acquire("r1", ProjectAccessNeed(required=True, write=False))
        self.addCleanup(c.release, "r1", "completed")
        self.assertTrue(c.held_by("r1"))

    def test_two_runs_cannot_hold_one_project(self):
        a = self.coord(); b = self.coord()
        need = ProjectAccessNeed(required=True, write=True)
        a.acquire("r1", need); self.addCleanup(a.release, "r1", "completed")
        with self.assertRaises(ProjectBusy):
            b.acquire("r2", need)

    def test_reacquiring_for_the_same_run_is_idempotent(self):
        c = self.coord(); need = ProjectAccessNeed(required=True, write=True)
        c.acquire("r1", need); c.acquire("r1", need)
        self.addCleanup(c.release, "r1", "completed")
        self.assertEqual(1, len(self.registry.occupancies()))

    def test_settling_releases_the_project(self):
        c = self.coord(); c.acquire("r1", ProjectAccessNeed(required=True, write=True))
        c.release("r1", "completed")
        self.assertFalse(c.held_by("r1")); self.assertEqual((), self.registry.occupancies())

    def test_waiting_does_not_release_the_project(self):
        c = self.coord(); c.acquire("r1", ProjectAccessNeed(required=True, write=True))
        self.addCleanup(c.release, "r1", "completed")
        for status in ("running", "waiting", "interrupted"):
            c.release("r1", status)
            self.assertTrue(c.held_by("r1"), status)

    def test_unknown_never_releases_the_project(self):
        """Nobody can say the Agent subprocess stopped, so the claim stays."""
        c = self.coord(); c.acquire("r1", ProjectAccessNeed(required=True, write=True))
        self.addCleanup(c.release, "r1", "cancelled")
        c.release("r1", "unknown")
        self.assertTrue(c.held_by("r1"))
        self.assertNotIn("unknown", RELEASING_STATUSES)

    def test_abandon_drops_the_lock_but_leaves_the_record(self):
        c = self.coord(); c.acquire("r1", ProjectAccessNeed(required=True, write=True))
        c.abandon("r1")
        self.assertFalse(c.held_by("r1"))
        self.assertEqual(["r1"], [o.run_id for o in self.registry.occupancies()])

if __name__ == "__main__":
    unittest.main()


class ServiceSeamTests(unittest.TestCase):
    """The two seams the service drives: claim before executing, release on settle.

    A recording double rather than a live coordinator, because the operator
    grant that lets an `isolation: none` workflow past the compiler's
    capability gate is not wired yet — these assert the seams exist and fire
    in the right places, which is what this increment adds.
    """

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from tests.test_web_composition import (
            publish_linear_workflow, transform_registration,
        )
        from orbit.workflow.langgraph_runtime import build_service

        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        publish_linear_workflow(root / "runtime.db")
        self.service = build_service(
            root / "runtime.db", [transform_registration()],
            state_directory=root / "langgraph",
        )

    class Recorder:
        class _FreeRegistry:
            def blocked_by(self, _path):
                return ()

        def __init__(self, need):
            self.need = need
            self.acquired: list[str] = []
            self.released: list[tuple[str, str]] = []
            self.write_granted = True
            self.project_root = "/tmp/project"
            self.registry = self._FreeRegistry()

        def preflight(self, need):
            pass

        def acquire(self, run_id, need):
            self.acquired.append(run_id)

        def release(self, run_id, status):
            self.released.append((run_id, status))

    def test_a_run_without_project_access_claims_nothing(self) -> None:
        recorder = self.Recorder(ProjectAccessNeed())
        self.service.project_access = recorder
        original = self.service._project_need
        self.service._project_need = lambda ir: None

        run = self.service.start(
            "workflow:linear", {"value": 1}, idempotency_key="k", actor="local",
        )

        self.assertEqual("completed", run.status)
        self.assertEqual([], recorder.acquired)
        # `release` is still told the outcome; it is the coordinator that
        # decides a run it never held needs nothing done.
        self.assertEqual([(run.run_id, "completed")], recorder.released)
        self.service._project_need = original

    def test_settlement_observes_outside_transaction_and_persists_failure(self):
        recorder = self.Recorder(ProjectAccessNeed())
        def finalize(run_id, status):
            # A second writer must be able to acquire SQLite immediately.
            with self.service._connect() as connection:
                connection.execute("PRAGMA busy_timeout=1")
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
            raise OSError("metadata unavailable")
        recorder.finalize = finalize
        self.service.project_access = recorder
        self.service._project_need = lambda ir: None
        run = self.service.start("workflow:linear", {"value": 1},
                                 idempotency_key="settle-failure", actor="local")
        self.assertEqual("completed", run.status)
        self.assertEqual([(run.run_id, "completed")], recorder.released)
        self.service.project_access = None
        summary = self.service.project_summary(run.run_id)
        self.assertEqual("unavailable", summary["kind"])
        self.assertIn("metadata unavailable", summary["error"])

    def test_a_run_that_needs_the_project_claims_before_executing(self) -> None:
        need = ProjectAccessNeed(required=True, write=True, agent_nodes=("collect",))
        recorder = self.Recorder(need)
        self.service.project_access = recorder
        self.service._project_need = lambda ir: need

        run = self.service.start(
            "workflow:linear", {"value": 1}, idempotency_key="k", actor="local",
        )

        self.assertEqual([run.run_id], recorder.acquired)
        self.assertEqual([(run.run_id, "completed")], recorder.released)

    def test_a_start_queues_behind_whoever_is_holding_the_project(self) -> None:
        """Held is *not yet*, and the Runtime can wait by itself.

        This used to be refused before a Run existed, which told the second
        person to come back and start it again by hand — for a wait the
        Runtime is perfectly able to do. The run is now created, parked
        `waiting`, and in the queue, which is also what makes it something a
        person can see and cancel.
        """

        from orbit.platform.project_occupancy import (
            ProjectOccupancyRegistry,
        )
        from orbit.workflow.langgraph_runtime.project_access import (
            ProjectAccessCoordinator,
        )
        from pathlib import Path

        import subprocess

        root = Path(self.temp.name)
        project = root / "project"
        project.mkdir()
        for argv in (
            ("git", "init", "--initial-branch=main"),
            ("git", "config", "user.email", "t@e.com"),
            ("git", "config", "user.name", "T"),
        ):
            subprocess.run(argv, cwd=project, capture_output=True, check=True)
        (project / "seed.txt").write_text("seed\n")
        subprocess.run(("git", "add", "-A"), cwd=project, capture_output=True, check=True)
        subprocess.run(
            ("git", "commit", "-m", "init"), cwd=project,
            capture_output=True, check=True,
        )
        registry = ProjectOccupancyRegistry(root / "occ")
        holder = ProjectAccessCoordinator(
            project, registry=registry, write_granted=True,
        )
        holder.acquire("someone-else", ProjectAccessNeed(required=True, write=True))
        self.addCleanup(holder.release, "someone-else", "completed")

        self.service.project_access = ProjectAccessCoordinator(
            project, registry=registry, write_granted=True,
        )
        self.service._project_need = lambda ir: ProjectAccessNeed(
            required=True, write=True,
        )

        run = self.service.start(
            "workflow:linear", {"value": 1}, idempotency_key="k", actor="local",
        )

        self.assertEqual("waiting", run.status)
        self.assertFalse(self.service.project_access.held_by(run.run_id))
        waiting = next(
            item for item in run.interrupts if item["type"] == "project_wait"
        )
        self.assertIn("someone-else", waiting["reason"])
        # And it is in the line, durably, rather than only in a timer.
        with self.service._connect() as connection:
            queued = [
                row["run_id"] for row in connection.execute(
                    "SELECT run_id FROM langgraph_project_waiters"
                )
            ]
        self.assertEqual([run.run_id], queued)

    def test_the_holder_finishing_brings_the_next_look_forward(self) -> None:
        """Waking is what makes the queue a queue rather than a poll.

        Without it the run still gets in eventually — its own timer fires —
        but a project handed back a second after a run queued would sit idle
        for the whole poll interval with nobody holding it.
        """

        from orbit.platform.project_occupancy import ProjectOccupancyRegistry
        from orbit.workflow.langgraph_runtime.project_access import (
            ProjectAccessCoordinator,
        )
        from pathlib import Path
        import subprocess

        root = Path(self.temp.name)
        project = root / "waking"
        project.mkdir()
        for argv in (
            ("git", "init", "--initial-branch=main"),
            ("git", "config", "user.email", "t@e.com"),
            ("git", "config", "user.name", "T"),
        ):
            subprocess.run(argv, cwd=project, capture_output=True, check=True)
        (project / "seed.txt").write_text("seed\n")
        subprocess.run(("git", "add", "-A"), cwd=project, capture_output=True, check=True)
        subprocess.run(
            ("git", "commit", "-m", "init"), cwd=project,
            capture_output=True, check=True,
        )
        registry = ProjectOccupancyRegistry(root / "occ-waking")
        holder = ProjectAccessCoordinator(
            project, registry=registry, write_granted=True,
        )
        holder.acquire("holder", ProjectAccessNeed(required=True, write=True))
        self.service.project_access = ProjectAccessCoordinator(
            project, registry=registry, write_granted=True,
        )
        self.service._project_need = lambda ir: ProjectAccessNeed(
            required=True, write=True,
        )
        run = self.service.start(
            "workflow:linear", {"value": 1}, idempotency_key="waking",
            actor="local",
        )
        self.assertEqual("waiting", run.status)

        def due() -> str:
            with self.service._connect() as connection:
                return str(connection.execute(
                    "SELECT due_at FROM langgraph_timers WHERE run_id=?"
                    " AND purpose='project_wait'", (run.run_id,),
                ).fetchone()["due_at"])

        scheduled = due()
        holder.release("holder", "completed")
        woken = self.service._wake_next_for_project()

        self.assertEqual(run.run_id, woken)
        self.assertLess(due(), scheduled)

    def test_the_queued_run_gets_the_project_and_finishes(self) -> None:
        """The whole point, end to end: refused nothing, waited, then ran."""

        from orbit.platform.project_occupancy import ProjectOccupancyRegistry
        from orbit.workflow.langgraph_runtime.project_access import (
            ProjectAccessCoordinator,
        )
        from pathlib import Path
        import subprocess

        root = Path(self.temp.name)
        project = root / "queueing"
        project.mkdir()
        for argv in (
            ("git", "init", "--initial-branch=main"),
            ("git", "config", "user.email", "t@e.com"),
            ("git", "config", "user.name", "T"),
        ):
            subprocess.run(argv, cwd=project, capture_output=True, check=True)
        (project / "seed.txt").write_text("seed\n")
        subprocess.run(("git", "add", "-A"), cwd=project, capture_output=True, check=True)
        subprocess.run(
            ("git", "commit", "-m", "init"), cwd=project,
            capture_output=True, check=True,
        )
        registry = ProjectOccupancyRegistry(root / "occ-queueing")
        holder = ProjectAccessCoordinator(
            project, registry=registry, write_granted=True,
        )
        holder.acquire("holder", ProjectAccessNeed(required=True, write=True))
        self.service.project_access = ProjectAccessCoordinator(
            project, registry=registry, write_granted=True,
        )
        self.service._project_need = lambda ir: ProjectAccessNeed(
            required=True, write=True,
        )
        run = self.service.start(
            "workflow:linear", {"value": 1}, idempotency_key="queueing",
            actor="local",
        )
        self.assertEqual("waiting", run.status)

        holder.release("holder", "completed")
        self.service._wake_next_for_project()
        self.service.recover_due()

        settled = self.service.get(run.run_id)
        self.assertEqual("completed", settled.status)
        with self.service._connect() as connection:
            self.assertEqual([], connection.execute(
                "SELECT run_id FROM langgraph_project_waiters"
            ).fetchall())

    def test_a_wait_that_has_gone_on_too_long_is_called_off(self) -> None:
        """`waiting` that can never end is a failure wearing a hopeful status.

        The holder may be a run nobody will ever answer. A queue nobody
        empties is worse than a refusal somebody reads, so the wait is
        bounded and says what it was waiting for.
        """

        from orbit.platform.project_occupancy import ProjectOccupancyRegistry
        from orbit.workflow.langgraph_runtime.project_access import (
            ProjectAccessCoordinator,
        )
        from pathlib import Path
        import subprocess

        root = Path(self.temp.name)
        project = root / "too-long"
        project.mkdir()
        for argv in (
            ("git", "init", "--initial-branch=main"),
            ("git", "config", "user.email", "t@e.com"),
            ("git", "config", "user.name", "T"),
        ):
            subprocess.run(argv, cwd=project, capture_output=True, check=True)
        (project / "seed.txt").write_text("seed\n")
        subprocess.run(("git", "add", "-A"), cwd=project, capture_output=True, check=True)
        subprocess.run(
            ("git", "commit", "-m", "init"), cwd=project,
            capture_output=True, check=True,
        )
        registry = ProjectOccupancyRegistry(root / "occ-too-long")
        holder = ProjectAccessCoordinator(
            project, registry=registry, write_granted=True,
        )
        holder.acquire("holder", ProjectAccessNeed(required=True, write=True))
        self.addCleanup(holder.release, "holder", "completed")
        self.service.project_access = ProjectAccessCoordinator(
            project, registry=registry, write_granted=True,
        )
        self.service._project_need = lambda ir: ProjectAccessNeed(
            required=True, write=True,
        )
        run = self.service.start(
            "workflow:linear", {"value": 1}, idempotency_key="too-long",
            actor="local",
        )
        # Backdated rather than waited for: the limit is hours, and a test
        # that spent them would be measuring the clock, not the queue.
        with self.service._connect() as connection:
            connection.execute(
                "UPDATE langgraph_project_waiters SET enqueued_at='2000-01-01T00:00:00Z'"
                " WHERE run_id=?", (run.run_id,),
            )
            connection.commit()

        self.service._wake_next_for_project()
        self.service.recover_due()

        settled = self.service.get(run.run_id)
        self.assertEqual("failed", settled.status)
        self.assertIn("still held", settled.error or "")
        with self.service._connect() as connection:
            self.assertEqual([], connection.execute(
                "SELECT run_id FROM langgraph_project_waiters"
            ).fetchall())

    def test_a_cancelled_run_leaves_the_line(self) -> None:
        """Nothing else would ever take it out: it is waiting, not running."""

        from orbit.platform.project_occupancy import ProjectOccupancyRegistry
        from orbit.workflow.langgraph_runtime.project_access import (
            ProjectAccessCoordinator,
        )
        from pathlib import Path
        import subprocess

        root = Path(self.temp.name)
        project = root / "leaving"
        project.mkdir()
        for argv in (
            ("git", "init", "--initial-branch=main"),
            ("git", "config", "user.email", "t@e.com"),
            ("git", "config", "user.name", "T"),
        ):
            subprocess.run(argv, cwd=project, capture_output=True, check=True)
        (project / "seed.txt").write_text("seed\n")
        subprocess.run(("git", "add", "-A"), cwd=project, capture_output=True, check=True)
        subprocess.run(
            ("git", "commit", "-m", "init"), cwd=project,
            capture_output=True, check=True,
        )
        registry = ProjectOccupancyRegistry(root / "occ-leaving")
        holder = ProjectAccessCoordinator(
            project, registry=registry, write_granted=True,
        )
        holder.acquire("holder", ProjectAccessNeed(required=True, write=True))
        self.addCleanup(holder.release, "holder", "completed")
        self.service.project_access = ProjectAccessCoordinator(
            project, registry=registry, write_granted=True,
        )
        self.service._project_need = lambda ir: ProjectAccessNeed(
            required=True, write=True,
        )
        run = self.service.start(
            "workflow:linear", {"value": 1}, idempotency_key="leaving",
            actor="local",
        )

        cancelled = self.service.cancel(
            run.run_id, expected_revision=run.revision,
            idempotency_key="cancel-leaving", actor="local",
        )

        self.assertEqual("cancelled", cancelled.status)
        with self.service._connect() as connection:
            self.assertEqual([], connection.execute(
                "SELECT run_id FROM langgraph_project_waiters"
            ).fetchall())

    def test_a_start_is_refused_when_write_was_not_granted(self) -> None:
        from orbit.workflow.langgraph_runtime.service import LangGraphRunConflict
        from orbit.platform.project_occupancy import ProjectOccupancyRegistry
        from orbit.workflow.langgraph_runtime.project_access import (
            ProjectAccessCoordinator,
        )
        from pathlib import Path

        root = Path(self.temp.name)
        project = root / "project2"
        project.mkdir()
        self.service.project_access = ProjectAccessCoordinator(
            project, registry=ProjectOccupancyRegistry(root / "occ2"),
            write_granted=False,
        )
        self.service._project_need = lambda ir: ProjectAccessNeed(
            required=True, write=True,
        )

        with self.assertRaises(LangGraphRunConflict) as caught:
            self.service.start(
                "workflow:linear", {"value": 1}, idempotency_key="k",
                actor="local",
            )
        self.assertIn("--agent-project-write", str(caught.exception))
        self.assertEqual([], list(self.service.list_runs()))


class RecoveryPointIntegrationTests(unittest.TestCase):
    """Taking the project also establishes the way back out of it (§6)."""

    def setUp(self):
        import subprocess
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"; self.project.mkdir()
        for argv in (("git","init","--initial-branch=main"),
                     ("git","config","user.email","t@e.com"),
                     ("git","config","user.name","T")):
            subprocess.run(argv, cwd=self.project, capture_output=True, check=True)
        (self.project/"tracked.txt").write_text("before\n")
        subprocess.run(("git","add","-A"), cwd=self.project, capture_output=True, check=True)
        subprocess.run(("git","commit","-m","init"), cwd=self.project, capture_output=True, check=True)
        (self.project/"untracked.txt").write_text("also before\n")
        self.registry = ProjectOccupancyRegistry(self.root / "occ")

    def coord(self):
        return ProjectAccessCoordinator(
            self.project, registry=self.registry, write_granted=True,
        )

    def test_a_recovery_point_is_established_when_the_project_is_taken(self):
        c = self.coord()
        c.acquire("r1", ProjectAccessNeed(required=True, write=True))
        self.addCleanup(c.release, "r1", "completed")

        status = c.status("r1")
        self.assertEqual("git", status["recovery"]["kind"])
        self.assertTrue(status["recovery"]["worktree_tree"])

    def test_the_claim_that_outlives_a_crash_carries_the_way_back(self):
        """A crash is exactly when somebody needs to be told what can be
        restored, and the occupancy record is the thing built to survive one."""

        c = self.coord()
        c.acquire("r1", ProjectAccessNeed(required=True, write=True))
        c.abandon("r1")  # as a Runtime stopping mid-run leaves it

        left = [o for o in self.registry.occupancies() if o.run_id == "r1"]
        self.assertEqual(1, len(left))
        self.assertTrue(left[0].recovery["worktree_tree"])
        self.assertIn("untracked files git is not ignoring", left[0].recovery["covered"])

    def test_the_baseline_can_actually_put_the_project_back(self):
        from orbit.workspace.recovery import GitRecoveryPoints, RecoveryPoint

        c = self.coord()
        c.acquire("r1", ProjectAccessNeed(required=True, write=True))
        self.addCleanup(c.release, "r1", "completed")
        (self.project / "tracked.txt").write_text("AGENT WROTE THIS\n")

        facts = c.status("r1")["recovery"]
        points = GitRecoveryPoints(self.project)
        point = RecoveryPoint(
            run_id="r1", project_root=self.project, kind="git",
            created_at=facts["created_at"], head=facts["head"],
            worktree_tree=facts["worktree_tree"], ref=facts["ref"],
        )
        points.restore(point, points.plan_restore(point))

        self.assertEqual("before\n", (self.project / "tracked.txt").read_text())

    def test_a_non_git_project_is_refused_rather_than_run_unprotected(self):
        from orbit.workspace.recovery import RecoveryUnavailable

        plain = self.root / "plain"; plain.mkdir()
        c = ProjectAccessCoordinator(
            plain, registry=self.registry, write_granted=True,
        )
        with self.assertRaises(RecoveryUnavailable):
            c.acquire("r1", ProjectAccessNeed(required=True, write=True))
        # And the project was handed straight back, not left held by a run
        # that never started.
        self.assertEqual([], [o for o in self.registry.occupancies() if o.run_id == "r1"])

    def test_a_summary_is_answerable_after_the_run_settled(self):
        """"What did that run change" is asked most often once it is over,
        so it is rebuilt from the ref rather than from a live claim."""

        c = self.coord()
        c.acquire("r1", ProjectAccessNeed(required=True, write=True))
        (self.project / "tracked.txt").write_text("AGENT WROTE\n")
        c.release("r1", "completed")

        summary = c.summarize("r1")

        self.assertEqual("run_cumulative", summary["scope"])
        self.assertIn(
            {"path": "tracked.txt", "status": "modified"}, summary["content"],
        )

    def test_a_run_that_never_held_the_project_has_no_summary(self):
        self.assertIsNone(self.coord().summarize("never-ran"))


class NonGitRecoveryTests(unittest.TestCase):
    """A non-git project can have a way back now — a partial one, declared."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"; self.project.mkdir()
        (self.project / "important.conf").write_text("original\n")
        (self.project / "unnamed.txt").write_text("nobody named me\n")
        self.registry = ProjectOccupancyRegistry(self.root / "occ")

    def coord(self):
        return ProjectAccessCoordinator(
            self.project, registry=self.registry, write_granted=True,
        )

    def test_it_picks_the_file_backup_strategy_off_git(self):
        from orbit.workspace.recovery import FileBackupRecoveryPoints

        self.assertIsInstance(
            self.coord().recovery_points, FileBackupRecoveryPoints,
        )

    def test_without_protect_it_is_still_refused(self):
        """Unchanged from before: no way back means no run (§6.2)."""

        from orbit.workspace.recovery import RecoveryUnavailable

        c = self.coord()
        with self.assertRaises(RecoveryUnavailable):
            c.acquire("r1", ProjectAccessNeed(required=True, write=True))
        self.assertEqual(
            [], [o for o in self.registry.occupancies() if o.run_id == "r1"],
        )

    def test_with_protect_the_run_may_proceed(self):
        c = self.coord()
        c.acquire("r1", ProjectAccessNeed(
            required=True, write=True, protect=("important.conf",),
        ))
        self.addCleanup(c.release, "r1", "completed")

        recovery = c.status("r1")["recovery"]
        self.assertEqual("file_backup", recovery["kind"])
        self.assertEqual(["important.conf"], recovery["covered"])
        self.assertIn(
            "every file the workflow did not name in workspace_access.protect",
            recovery["uncovered"],
        )

    def test_the_declared_file_can_actually_be_put_back(self):
        from orbit.workspace.recovery import FileBackupRecoveryPoints

        c = self.coord()
        c.acquire("r1", ProjectAccessNeed(
            required=True, write=True, protect=("important.conf",),
        ))
        self.addCleanup(c.release, "r1", "completed")
        (self.project / "important.conf").write_text("WRECKED\n")

        points = FileBackupRecoveryPoints(self.project, self.project / ".orbit")
        point = points.load("r1")
        points.restore(point, points.plan_restore(point))

        self.assertEqual(
            "original\n", (self.project / "important.conf").read_text(),
        )

    def test_a_git_project_still_uses_git(self):
        import subprocess
        from orbit.workspace.recovery import GitRecoveryPoints

        repo = self.root / "repo"; repo.mkdir()
        for argv in (("git","init","--initial-branch=main"),
                     ("git","config","user.email","t@e.com"),
                     ("git","config","user.name","T")):
            subprocess.run(argv, cwd=repo, capture_output=True, check=True)
        (repo / "a.txt").write_text("a\n")
        subprocess.run(("git","add","-A"), cwd=repo, capture_output=True, check=True)
        subprocess.run(("git","commit","-m","i"), cwd=repo, capture_output=True, check=True)

        coordinator = ProjectAccessCoordinator(
            repo, registry=self.registry, write_granted=True,
        )
        self.assertIsInstance(coordinator.recovery_points, GitRecoveryPoints)

    def test_a_project_that_can_have_no_way_back_leaves_no_run_behind(self):
        """Found by running it, not by reading it.

        The refusal was already right — §6.2 will not run a workflow it
        cannot undo — but it happened inside `_execute`, after the durable
        Run existed. So a refused start left a Run stuck `running`, and in
        single-goal mode that Run then occupied the slot and blocked every
        later start with `active_goal_exists`. Asked before the Run exists
        now, which is what `_require_project_available` is for.
        """

        from orbit.workflow.langgraph_runtime import build_service
        from orbit.workflow.langgraph_runtime.service import LangGraphRunConflict
        from tests.test_web_composition import (
            publish_linear_workflow, transform_registration,
        )

        publish_linear_workflow(self.root / "runtime.db")
        service = build_service(
            self.root / "runtime.db", [transform_registration()],
            state_directory=self.root / "langgraph",
            project_access=self.coord(),
        )
        service._project_need = lambda ir: ProjectAccessNeed(
            required=True, write=True,  # no `protect`, and not a git project
        )

        with self.assertRaises(LangGraphRunConflict) as caught:
            service.start(
                "workflow:linear", {"value": 1}, idempotency_key="k",
                actor="local",
            )

        self.assertIn("workspace_access.protect", str(caught.exception))
        self.assertEqual([], list(service.list_runs()))
