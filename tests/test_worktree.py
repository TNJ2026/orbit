"""Per-task git worktree isolation + the integrate merge gate."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import orbit.server as server
from orbit.store import Store


def _init_repo(root: Path) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
    }
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    (root / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True, env=env)


class WorkflowSchemaTests(unittest.TestCase):
    def test_integrate_flag_forces_isolate_off(self):
        norm = server._normalize_workflow_step(
            {"id": "x", "name": "X", "role_id": "hub", "isolate": True, "integrate": True},
            0,
        )
        self.assertTrue(norm["integrate"])
        self.assertFalse(norm["isolate"])

    def test_isolate_flag_roundtrips(self):
        norm = server._normalize_workflow_step(
            {"id": "impl", "name": "Impl", "role_id": "implementer", "isolate": True},
            0,
        )
        self.assertTrue(norm["isolate"])
        self.assertFalse(norm["integrate"])

    def test_default_workflow_has_integrate_gate_and_isolation(self):
        steps = {s["id"]: s for s in server.default_workflow_steps()}
        for isolated in ("implement", "test", "review"):
            self.assertTrue(steps[isolated]["isolate"], isolated)
        self.assertTrue(steps["integrate"]["integrate"])
        self.assertFalse(steps["integrate"]["isolate"])
        self.assertEqual("hub", steps["integrate"]["role_id"])

        pairs = {(e["from"], e["to"]) for e in server.default_workflow_edges()}
        self.assertIn(("review", "integrate"), pairs)
        self.assertIn(("integrate", "accept"), pairs)
        self.assertIn(("integrate", "implement"), pairs)  # merge-conflict rework

    def test_default_workflow_is_design_first(self):
        steps = {s["id"]: s for s in server.default_workflow_steps()}
        # design-first: the goal splits at `plan` (after the design steps), not intake
        self.assertTrue(steps["plan"]["decompose"])
        self.assertEqual("hub", steps["plan"]["role_id"])
        self.assertFalse(steps["plan"]["isolate"])
        with TemporaryDirectory() as tmp:
            cfg = server.read_workflow_config(tmp)
            back = server._workflow_graph(cfg)
            self.assertEqual("plan", server._root_goal_decompose_step_id(cfg, back))
        # only `plan` is a decompose step
        self.assertEqual(
            ["plan"], [sid for sid, s in steps.items() if s["decompose"]]
        )

    def test_default_workflow_roundtrips_flags(self):
        with TemporaryDirectory() as tmp:
            server.write_workflow_config(
                server.default_workflow_steps(), tmp, server.default_workflow_edges()
            )
            loaded = {s["id"]: s for s in server.read_workflow_config(tmp)["steps"]}
        self.assertTrue(loaded["implement"]["isolate"])
        self.assertTrue(loaded["integrate"]["integrate"])
        self.assertFalse(loaded["integrate"]["isolate"])

    def test_verify_field_roundtrips(self):
        norm = server._normalize_workflow_step(
            {"id": "test", "name": "Test", "role_id": "tester", "verify": "  pytest -q  "},
            0,
        )
        self.assertEqual("pytest -q", norm["verify"])
        blank = server._normalize_workflow_step(
            {"id": "impl", "name": "Impl", "role_id": "implementer"}, 0
        )
        self.assertEqual("", blank["verify"])

    def test_default_test_is_mandatory_gate_with_rework_edge(self):
        steps = {s["id"]: s for s in server.default_workflow_steps()}
        self.assertTrue(steps["test"]["required"])  # mandatory verification step
        pairs = {(e["from"], e["to"]) for e in server.default_workflow_edges()}
        self.assertIn(("test", "implement"), pairs)  # verify failed -> rework


class StepSeparationTests(unittest.TestCase):
    """write_workflow_config nudges overlapping nodes apart so a config written
    outside the UI (hand edit / script) never stacks nodes on the canvas."""

    def test_cramped_same_row_is_separated(self):
        steps = [
            {"id": "test", "name": "T", "role_id": "tester", "y": 94.0, "x": 1490.0},
            {"id": "integrate", "name": "I", "role_id": "hub", "y": 94.0, "x": 1635.0},
            {"id": "accept", "name": "A", "role_id": "hub", "y": 94.0, "x": 1780.0},
        ]
        server._separate_overlapping_steps(steps)
        xs = sorted(s["x"] for s in steps)
        gaps = [b - a for a, b in zip(xs, xs[1:])]
        self.assertTrue(all(g >= server._WF_MIN_STEP_DX for g in gaps), xs)

    def test_default_layout_is_left_untouched(self):
        steps = server.default_workflow_steps()
        before = {s["id"]: (s["x"], s["y"]) for s in steps}
        server._separate_overlapping_steps(steps)
        after = {s["id"]: (s["x"], s["y"]) for s in steps}
        self.assertEqual(before, after)

    def test_parallel_stack_same_x_different_row_kept(self):
        # Branches stacked vertically (dy beyond the row tolerance) must not be
        # treated as an overlap and pushed apart.
        steps = [
            {"id": "arch", "name": "A", "role_id": "architect", "x": 620.0, "y": 40.0},
            {"id": "ui", "name": "U", "role_id": "ui_designer", "x": 620.0, "y": 148.0},
        ]
        server._separate_overlapping_steps(steps)
        self.assertEqual([620.0, 620.0], [s["x"] for s in steps])

    def test_duplicate_position_is_split(self):
        steps = [
            {"id": "a", "name": "A", "role_id": "hub", "x": 500.0, "y": 94.0},
            {"id": "b", "name": "B", "role_id": "hub", "x": 500.0, "y": 94.0},
        ]
        server._separate_overlapping_steps(steps)
        self.assertNotEqual(steps[0]["x"], steps[1]["x"])

    def test_write_config_persists_separated_coords(self):
        with TemporaryDirectory() as tmp:
            steps = server.default_workflow_steps()
            by = {s["id"]: s for s in steps}
            # Jam integrate on top of test (both isolate/role fine for a write).
            by["integrate"]["x"], by["integrate"]["y"] = by["test"]["x"] + 20, by["test"]["y"]
            saved = server.write_workflow_config(
                steps, tmp, server.default_workflow_edges()
            )
            xs = {s["id"]: s["x"] for s in saved["steps"]}
            self.assertGreaterEqual(
                abs(xs["integrate"] - xs["test"]), server._WF_MIN_STEP_DX
            )


class StepPromptTests(unittest.TestCase):
    TASK = {"id": 42, "title": "t", "content": "c", "is_goal": 0, "parent_task_id": None}

    def test_integrate_prompt_has_merge_instructions(self):
        with TemporaryDirectory() as tmp:
            p = server._build_step_prompt(
                tmp, self.TASK,
                {"id": "integrate", "name": "Integrate", "role_id": "hub", "integrate": True},
                "", can_rework=True,
            )
        self.assertIn("orbit/task-42", p)
        self.assertIn("git merge", p)

    def test_isolated_prompt_mentions_worktree_branch(self):
        with TemporaryDirectory() as tmp:
            p = server._build_step_prompt(
                tmp, self.TASK,
                {"id": "implement", "name": "Implement", "role_id": "implementer"},
                "", can_rework=True, isolated=True,
            )
        self.assertIn("orbit/task-42", p)
        self.assertIn("worktree", p)

    def test_non_isolated_prompt_uses_project_root(self):
        with TemporaryDirectory() as tmp:
            p = server._build_step_prompt(
                tmp, self.TASK,
                {"id": "intake", "name": "Triage", "role_id": "hub"},
                "", can_rework=False, isolated=False,
            )
        self.assertIn("项目根目录", p)
        self.assertNotIn("worktree", p)


class WorktreeLifecycleTests(unittest.TestCase):
    def test_non_git_project_skips_isolation(self):
        with TemporaryDirectory() as tmp:
            self.assertIsNone(server._ensure_task_worktree(tmp, 1))

    def test_ensure_is_idempotent_and_on_task_branch(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            wt1 = server._ensure_task_worktree(tmp, 5)
            self.assertIsNotNone(wt1)
            self.assertTrue(wt1.exists())
            branch = subprocess.run(
                ["git", "-C", str(wt1), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual("orbit/task-5", branch)
            # Second call reattaches the same worktree, never a duplicate.
            wt2 = server._ensure_task_worktree(tmp, 5)
            self.assertEqual(wt1, wt2)
            self.assertTrue(server._worktree_registered(root, wt1))

    def test_remove_then_recreate(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            wt = server._ensure_task_worktree(tmp, 7)
            server._remove_task_worktree(tmp, 7)
            self.assertFalse(wt.exists())
            self.assertFalse(server._branch_exists(root, "orbit/task-7"))
            wt2 = server._ensure_task_worktree(tmp, 7)
            self.assertIsNotNone(wt2)
            self.assertTrue(wt2.exists())

    def test_ensure_recovers_stale_unregistered_dir(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            # Simulate a leftover checkout dir from a force-removed worktree that
            # git no longer tracks: bare dir, no registration.
            stale = server._task_worktree_dir(tmp, 3)
            stale.mkdir(parents=True)
            (stale / "junk.txt").write_text("x", encoding="utf-8")
            wt = server._ensure_task_worktree(tmp, 3)
            self.assertIsNotNone(wt)
            self.assertTrue(server._worktree_registered(root, wt))


class GitProvisioningTests(unittest.TestCase):
    """Before a workflow with isolate/integrate steps starts, the engine makes
    sure a git repo with a base commit exists (or degrades if git is absent)."""

    def test_needs_git_predicate(self):
        self.assertTrue(
            server._workflow_needs_git({"steps": server.default_workflow_steps()})
        )
        self.assertFalse(
            server._workflow_needs_git(
                {"steps": [{"id": "a", "isolate": False, "integrate": False}]}
            )
        )

    def test_ensure_inits_non_git_dir_with_base_commit(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src.py").write_text("x = 1\n", encoding="utf-8")
            self.assertTrue(server._ensure_git_repo(tmp))
            self.assertTrue(server._is_git_repo(root))
            self.assertEqual("HEAD", server._worktree_base_ref(root))
            # runtime dirs are gitignored so integrate's `git status` stays clean
            ignored = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
            self.assertIn(".orbit/worktrees/", ignored)
            self.assertIn(".orbit/tasks/", ignored)
            # the base commit is enough to seed a real per-task worktree
            self.assertIsNotNone(server._ensure_task_worktree(tmp, 1))

    def test_ensure_seeds_base_in_empty_dir(self):
        with TemporaryDirectory() as tmp:
            self.assertTrue(server._ensure_git_repo(tmp))
            self.assertEqual("HEAD", server._worktree_base_ref(Path(tmp)))

    def test_ensure_leaves_existing_commits_untouched(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            head = subprocess.run(
                ["git", "-C", tmp, "rev-parse", "HEAD"], capture_output=True, text=True
            ).stdout.strip()
            # A dirty tree must NOT be auto-committed when a base already exists.
            (root / "wip.txt").write_text("in progress", encoding="utf-8")
            self.assertTrue(server._ensure_git_repo(tmp))
            head2 = subprocess.run(
                ["git", "-C", tmp, "rev-parse", "HEAD"], capture_output=True, text=True
            ).stdout.strip()
            self.assertEqual(head, head2)
            self.assertTrue((root / "wip.txt").exists())  # left uncommitted

    def test_warns_when_git_missing_for_isolating_workflow(self):
        from unittest import mock

        steps = server.default_workflow_steps()  # has isolate/integrate steps
        with mock.patch.object(server, "_git_available", return_value=False):
            w = server._workflow_graph_warnings(steps, server.default_workflow_edges())
        self.assertTrue(any("git is not installed" in x for x in w), w)

    def test_no_git_warning_when_git_available(self):
        from unittest import mock

        steps = server.default_workflow_steps()
        with mock.patch.object(server, "_git_available", return_value=True):
            w = server._workflow_graph_warnings(steps, server.default_workflow_edges())
        self.assertFalse(any("git is not installed" in x for x in w), w)

    def test_no_git_warning_for_workflow_without_isolation(self):
        from unittest import mock

        # No isolate/integrate step -> git is irrelevant, so no warning even
        # when git is unavailable.
        steps = [
            {"id": "intake", "name": "T", "role_id": "hub", "isolate": False, "integrate": False},
            {"id": "accept", "name": "A", "role_id": "hub", "isolate": False, "integrate": False},
        ]
        edges = [{"from": "intake", "to": "accept"}]
        with mock.patch.object(server, "_git_available", return_value=False):
            w = server._workflow_graph_warnings(steps, edges)
        self.assertFalse(any("git is not installed" in x for x in w), w)

    def test_integrate_noops_in_non_git_project(self):
        # A non-git project can't have a task branch; integrate must pass the step
        # (done) instead of dispatching the hub CLI to run `git merge` and fail.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / ".orbit" / "messages.db")
            store.register_agent("hub", "")
            ids = store.send_message("hub", "hub", "do it", kind="task", title="t")
            task_id = store.get_task_by_source_message(ids[0])["id"]
            step = {
                "id": "integrate", "name": "Integrate", "role_id": "hub",
                "isolate": False, "integrate": True, "task_status": "in_progress",
            }
            # runner_command would fail if ever spawned; the no-op must skip it.
            member = {"agent_name": "hub", "runner_command": "false"}
            res = server.run_step_worker(
                store, str(root), task_id, step, member,
                upstream_result="", advance=False,
            )
            self.assertEqual("done", res["outcome"])
            self.assertIn("not a git repository", res["result"])
            store.close()


class WorktreeSweepTests(unittest.TestCase):
    def test_sweep_reaps_finished_and_orphan_but_keeps_active(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            store = Store(root / ".orbit" / "messages.db")

            # Orphan: a worktree whose task row doesn't exist -> reaped.
            orphan = server._ensure_task_worktree(tmp, 999)
            self.assertTrue(orphan.exists())

            store.register_agent("hub", "")
            store.register_agent("codex", "")
            msg_ids = store.send_message("hub", "codex", "do it", kind="task", title="t")
            task_id = store.get_task_by_source_message(msg_ids[0])["id"]
            active = server._ensure_task_worktree(tmp, task_id)

            server._sweep_task_worktrees(store, tmp)
            self.assertFalse(orphan.exists())   # no task row
            self.assertTrue(active.exists())    # status 'created' -> kept

            # A regular task at 'accepted' is only passing through the accept
            # step (which may be non-terminal), so its worktree is kept.
            store.update_task_item_status(task_id, "accepted")
            server._sweep_task_worktrees(store, tmp)
            self.assertTrue(active.exists())    # accepted (non-goal) -> kept
            store.update_task_item_status(task_id, "closed")
            server._sweep_task_worktrees(store, tmp)
            self.assertFalse(active.exists())   # closed -> reaped
            store.close()


class VerifyGateTests(unittest.TestCase):
    def _setup(self, tmp):
        root = Path(tmp)
        _init_repo(root)
        store = Store(root / ".orbit" / "messages.db")
        store.register_agent("hub", "")
        store.register_agent("dev", "")
        ids = store.send_message("hub", "dev", "do it", kind="task", title="t")
        task_id = store.get_task_by_source_message(ids[0])["id"]
        return root, store, task_id

    def _run(self, root, store, task_id, runner_command, verify):
        # step id 'test' has a test->implement rework edge in the default config,
        # so a failing gate is rework-capable.
        step = {
            "id": "test", "name": "Test", "role_id": "tester",
            "isolate": True, "integrate": False, "task_status": "testing",
            "verify": verify,
        }
        member = {"agent_name": "dev", "runner_command": runner_command}
        return server.run_step_worker(
            store, str(root), task_id, step, member, upstream_result="", advance=False
        )

    def test_verify_pass_keeps_done(self):
        with TemporaryDirectory() as tmp:
            root, store, task_id = self._setup(tmp)
            res = self._run(
                root, store, task_id,
                "cat >/dev/null; echo WORKFLOW_OUTCOME: done", verify="true",
            )
            self.assertEqual("done", res["outcome"])
            store.close()

    def test_verify_failure_overrides_selfreported_done(self):
        with TemporaryDirectory() as tmp:
            root, store, task_id = self._setup(tmp)
            # Agent lies: claims done though nothing works. Gate `false` fails.
            res = self._run(
                root, store, task_id,
                "cat >/dev/null; echo WORKFLOW_OUTCOME: done", verify="false",
            )
            self.assertEqual("rework", res["outcome"])
            self.assertIn("机器验证失败", res["result"])
            store.close()

    def test_verify_runs_in_the_task_worktree(self):
        with TemporaryDirectory() as tmp:
            root, store, task_id = self._setup(tmp)
            # Agent writes a marker into its (worktree) cwd; the gate checks for it
            # in the same tree. Passing proves verify runs where the agent worked.
            res = self._run(
                root, store, task_id,
                "cat >/dev/null; touch built.txt; echo WORKFLOW_OUTCOME: done",
                verify="test -f built.txt",
            )
            self.assertEqual("done", res["outcome"])
            # And the marker is NOT in the main tree.
            self.assertFalse((root / "built.txt").exists())
            store.close()

    def test_no_verify_leaves_selfreport_untouched(self):
        with TemporaryDirectory() as tmp:
            root, store, task_id = self._setup(tmp)
            res = self._run(
                root, store, task_id,
                "cat >/dev/null; echo WORKFLOW_OUTCOME: done", verify="",
            )
            self.assertEqual("done", res["outcome"])
            store.close()


def _set_goal_verify(tmp, command):
    server.write_workflow_config(
        server.default_workflow_steps(), tmp, server.default_workflow_edges()
    )
    path = Path(tmp) / ".orbit" / "workflow.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["goal_verify"] = command
    path.write_text(json.dumps(data), encoding="utf-8")


class GoalConvergenceGateTests(unittest.TestCase):
    def _goal_with_closed_subtask(self, tmp):
        store = Store(Path(tmp) / ".orbit" / "messages.db")
        store.register_agent("hub", "")
        gid_msgs = store.send_message("hub", "hub", "the goal", kind="task", title="G")
        goal = store.get_task_by_source_message(gid_msgs[0])
        store.update_task_metadata(goal["id"], is_goal=True)
        sub_msgs = store.send_message(
            "hub", "hub", "a subtask", reply_to=gid_msgs[0], kind="task", title="S"
        )
        sub = store.get_task_by_source_message(sub_msgs[0])
        self.assertEqual(goal["id"], sub["parent_task_id"])
        store.update_task_item_status(sub["id"], "closed")
        return store, goal["id"], sub["id"]

    def test_no_goal_verify_accepts_on_aggregation(self):
        with TemporaryDirectory() as tmp:
            _set_goal_verify(tmp, "")
            store, goal_id, sub_id = self._goal_with_closed_subtask(tmp)
            server._recompute_parent_goal_status(store, store.get_task(sub_id), tmp)
            self.assertEqual("accepted", store.get_task(goal_id)["task_status"])
            self.assertFalse(store.has_workflow_action(goal_id, "goal_verify"))
            store.close()

    def test_configured_queues_action_and_holds_accept(self):
        with TemporaryDirectory() as tmp:
            _set_goal_verify(tmp, "true")
            store, goal_id, sub_id = self._goal_with_closed_subtask(tmp)
            server._recompute_parent_goal_status(store, store.get_task(sub_id), tmp)
            # Not accepted yet: the sweep owns that decision.
            self.assertEqual("in_progress", store.get_task(goal_id)["task_status"])
            self.assertTrue(store.has_workflow_action(goal_id, "goal_verify"))
            # Idempotent: recompute again doesn't create a second action.
            server._recompute_parent_goal_status(store, store.get_task(sub_id), tmp)
            pending = [
                a for a in store.list_workflow_actions("pending", 100)
                if a["action_type"] == "goal_verify" and a["task_id"] == goal_id
            ]
            self.assertEqual(1, len(pending))
            store.close()

    def test_sweep_pass_accepts_goal(self):
        with TemporaryDirectory() as tmp:
            _set_goal_verify(tmp, "true")
            store, goal_id, sub_id = self._goal_with_closed_subtask(tmp)
            server._recompute_parent_goal_status(store, store.get_task(sub_id), tmp)
            processed = server.goal_verify_sweep(store, tmp)
            self.assertEqual([{"goal_id": goal_id, "exit_code": 0}], processed)
            self.assertEqual("accepted", store.get_task(goal_id)["task_status"])
            store.close()

    def test_sweep_fail_stalls_goal_and_notifies_hub(self):
        with TemporaryDirectory() as tmp:
            _set_goal_verify(tmp, "false")
            store, goal_id, sub_id = self._goal_with_closed_subtask(tmp)
            server._recompute_parent_goal_status(store, store.get_task(sub_id), tmp)
            server.goal_verify_sweep(store, tmp)
            self.assertEqual("stalled", store.get_task(goal_id)["task_status"])
            self.assertTrue(store.has_unread("hub"))  # hub was notified
            store.close()

    def test_sweep_records_a_task_run(self):
        with TemporaryDirectory() as tmp:
            _set_goal_verify(tmp, "true")
            store, goal_id, sub_id = self._goal_with_closed_subtask(tmp)
            server._recompute_parent_goal_status(store, store.get_task(sub_id), tmp)
            server.goal_verify_sweep(store, tmp)
            runs = store.list_task_runs(goal_id)
            self.assertTrue(any(r["workflow_step"] == "goal_verify" for r in runs))
            store.close()

    def test_failed_verify_requeues_on_next_close(self):
        # A failed verification must not permanently block re-verification: after
        # the hub reworks and subtasks re-close, recompute queues a fresh check.
        with TemporaryDirectory() as tmp:
            _set_goal_verify(tmp, "false")
            store, goal_id, sub_id = self._goal_with_closed_subtask(tmp)
            server._recompute_parent_goal_status(store, store.get_task(sub_id), tmp)
            server.goal_verify_sweep(store, tmp)  # fails -> stalled, action failed
            self.assertEqual("stalled", store.get_task(goal_id)["task_status"])
            self.assertFalse(store.has_pending_workflow_action(goal_id, "goal_verify"))
            # rework re-close -> recompute fires again -> re-queue
            server._recompute_parent_goal_status(store, store.get_task(sub_id), tmp)
            self.assertTrue(store.has_pending_workflow_action(goal_id, "goal_verify"))
            self.assertEqual("in_progress", store.get_task(goal_id)["task_status"])
            store.close()

    def test_accepted_goal_not_requeued(self):
        with TemporaryDirectory() as tmp:
            _set_goal_verify(tmp, "true")
            store, goal_id, sub_id = self._goal_with_closed_subtask(tmp)
            server._recompute_parent_goal_status(store, store.get_task(sub_id), tmp)
            server.goal_verify_sweep(store, tmp)  # passes -> accepted
            self.assertEqual("accepted", store.get_task(goal_id)["task_status"])
            server._recompute_parent_goal_status(store, store.get_task(sub_id), tmp)
            self.assertFalse(store.has_pending_workflow_action(goal_id, "goal_verify"))
            store.close()


class GoalVerifyDetectionTests(unittest.TestCase):
    def test_detects_python_unittest(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            (root / "tests").mkdir()
            self.assertEqual(
                "python -m unittest discover -s tests",
                server._detect_goal_verify(root),
            )

    def test_python_marker_without_tests_dir_detects_nothing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            self.assertEqual("", server._detect_goal_verify(root))

    def test_detects_npm_only_with_test_script(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                '{"scripts": {"test": "jest"}}', encoding="utf-8"
            )
            self.assertEqual("npm test", server._detect_goal_verify(root))
            (root / "package.json").write_text(
                '{"scripts": {"build": "tsc"}}', encoding="utf-8"
            )
            self.assertEqual("", server._detect_goal_verify(root))

    def test_detects_make_test_target(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Makefile").write_text("build:\n\tcc x\ntest:\n\t./run\n", encoding="utf-8")
            self.assertEqual("make test", server._detect_goal_verify(root))

    def test_detects_cargo_and_go(self):
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
            self.assertEqual("cargo test", server._detect_goal_verify(Path(tmp)))
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "go.mod").write_text("module x\n", encoding="utf-8")
            self.assertEqual("go test ./...", server._detect_goal_verify(Path(tmp)))

    def test_no_markers_detects_nothing(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual("", server._detect_goal_verify(Path(tmp)))

    def test_configured_overrides_detected(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            (root / "tests").mkdir()
            _set_goal_verify(tmp, "true")
            self.assertEqual("true", server._effective_goal_verify(tmp))

    def test_effective_falls_back_to_detected(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            (root / "tests").mkdir()
            self.assertEqual(
                "python -m unittest discover -s tests",
                server._effective_goal_verify(tmp),
            )

    def test_detected_verify_queues_and_holds_accept(self):
        # No goal_verify configured, but project markers exist -> the detected
        # command gates the goal instead of accepting on aggregation.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            (root / "tests").mkdir()
            store = Store(root / ".orbit" / "messages.db")
            store.register_agent("hub", "")
            gmsgs = store.send_message("hub", "hub", "g", kind="task", title="G")
            goal = store.get_task_by_source_message(gmsgs[0])
            store.update_task_metadata(goal["id"], is_goal=True)
            smsgs = store.send_message(
                "hub", "hub", "s", reply_to=gmsgs[0], kind="task", title="S"
            )
            sub = store.get_task_by_source_message(smsgs[0])
            store.update_task_item_status(sub["id"], "closed")
            server._recompute_parent_goal_status(store, store.get_task(sub["id"]), tmp)
            self.assertEqual("in_progress", store.get_task(goal["id"])["task_status"])
            self.assertTrue(store.has_pending_workflow_action(goal["id"], "goal_verify"))
            store.close()


class ProcessControlPortabilityTests(unittest.TestCase):
    """Cross-platform process spawning/killing (POSIX process groups vs Windows
    taskkill). Most assertions are platform-dispatch checks so they run anywhere."""

    def test_detached_kwargs_match_platform(self):
        kw = server._detached_process_kwargs()
        if os.name == "nt":
            self.assertIn("creationflags", kw)
        else:
            self.assertEqual({"start_new_session": True}, kw)

    def test_windows_ppid_backend_is_noop_off_windows(self):
        if os.name != "nt":
            self.assertIsNone(server._snapshot_ppids_windows())

    def test_terminate_missing_pid_is_false(self):
        self.assertFalse(server._terminate_pid_tree(0))

    def test_terminate_pid_tree_kills_a_detached_process(self):
        if os.name == "nt":
            self.skipTest("uses a POSIX `sleep`; the kill path is taskkill on Windows")
        import time

        proc = subprocess.Popen(["sleep", "30"], **server._detached_process_kwargs())
        try:
            self.assertTrue(server._terminate_pid_tree(proc.pid))
            for _ in range(50):  # up to ~2.5s for SIGTERM to land
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(proc.poll(), "process survived terminate")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


class DescendantPidSnapshotTests(unittest.TestCase):
    def test_snapshot_maps_self_to_parent(self):
        import os

        mapping = server._snapshot_ppids()
        self.assertIsInstance(mapping, dict)
        self.assertTrue(mapping, "no ppid backend worked on this platform")
        self.assertEqual(os.getppid(), mapping.get(os.getpid()))

    def test_descendant_pids_includes_a_spawned_child(self):
        import os
        import subprocess
        import time

        child = subprocess.Popen(["sleep", "5"])
        try:
            found = False
            for _ in range(40):  # up to ~2s for the child to enter the table
                if child.pid in server._descendant_pids(os.getpid()):
                    found = True
                    break
                time.sleep(0.05)
            self.assertTrue(found, "spawned child not found among descendants")
        finally:
            child.terminate()
            child.wait()


class AcceptNonTerminalTests(unittest.TestCase):
    """`accepted` marks a task finished only for goals. A regular task passing
    through a non-terminal accept step stays live (watched, worktree kept)."""

    def test_task_workflow_finished_predicate(self):
        f = server._task_workflow_finished
        self.assertTrue(f(None))
        self.assertTrue(f({"task_status": "closed"}))
        self.assertTrue(f({"task_status": "accepted", "is_goal": True}))
        self.assertFalse(f({"task_status": "accepted", "is_goal": False}))
        self.assertFalse(f({"task_status": "accepted"}))
        self.assertFalse(f({"task_status": "in_progress"}))

    def test_regular_accepted_task_stays_non_terminal(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / ".orbit" / "messages.db")
            store.register_agent("hub", "")
            msgs = store.send_message("hub", "hub", "t", kind="task", title="T")
            tid = store.get_task_by_source_message(msgs[0])["id"]
            store.set_task_workflow_state(tid, task_status="accepted")
            ids = {t["id"] for t in store.list_non_terminal_tasks()}
            self.assertIn(tid, ids)
            store.close()

    def test_accepted_goal_is_terminal(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / ".orbit" / "messages.db")
            store.register_agent("hub", "")
            msgs = store.send_message("hub", "hub", "g", kind="task", title="G")
            gid = store.get_task_by_source_message(msgs[0])["id"]
            store.update_task_metadata(gid, is_goal=True)
            store.set_task_workflow_state(gid, task_status="accepted")
            ids = {t["id"] for t in store.list_non_terminal_tasks()}
            self.assertNotIn(gid, ids)
            store.close()

    def test_sweep_keeps_worktree_for_regular_accepted_task(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            store = Store(root / ".orbit" / "messages.db")
            store.register_agent("hub", "")
            msgs = store.send_message("hub", "hub", "t", kind="task", title="T")
            tid = store.get_task_by_source_message(msgs[0])["id"]
            wt = server._ensure_task_worktree(tmp, tid)
            self.assertIsNotNone(wt)
            self.assertTrue(wt.exists())
            # Passing through a non-terminal accept step: worktree must survive.
            store.set_task_workflow_state(tid, task_status="accepted")
            server._sweep_task_worktrees(store, tmp)
            self.assertTrue(wt.exists(), "regular accepted task's worktree was reaped")
            # Truly done: worktree is reaped.
            store.set_task_workflow_state(tid, task_status="closed")
            server._sweep_task_worktrees(store, tmp)
            self.assertFalse(wt.exists())
            store.close()


class ReapStaleRunsTests(unittest.TestCase):
    def test_reap_orphans_running_task_runs(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / ".orbit" / "m.db")
            store.register_agent("hub", "")
            tid = store.get_task_by_source_message(
                store.send_message("hub", "hub", "x", kind="task", title="t")[0]
            )["id"]
            run = store.create_task_run(tid, worker="dev", command="x", workflow_step="implement")
            self.assertEqual("running", store.get_task_run(run["id"])["status"])
            self.assertEqual(1, store.reap_stale_runs())
            self.assertEqual("orphaned", store.get_task_run(run["id"])["status"])
            store.close()


def _set_workflow_field(tmp, key, value):
    server.write_workflow_config(
        server.default_workflow_steps(), tmp, server.default_workflow_edges()
    )
    path = Path(tmp) / ".orbit" / "workflow.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data[key] = value
    path.write_text(json.dumps(data), encoding="utf-8")


class TokenBudgetConfigTests(unittest.TestCase):
    def test_budget_roundtrips_and_coerces(self):
        self.assertEqual(0, server._coerce_token_budget(None))
        self.assertEqual(0, server._coerce_token_budget("nope"))
        self.assertEqual(0, server._coerce_token_budget(-5))
        self.assertEqual(1000, server._coerce_token_budget("1000"))
        with TemporaryDirectory() as tmp:
            _set_workflow_field(tmp, "goal_token_budget", 12345)
            self.assertEqual(12345, server.read_workflow_config(tmp)["goal_token_budget"])
            # A plain UI save (no budget field) must preserve it.
            server.write_workflow_config(
                server.default_workflow_steps(), tmp, server.default_workflow_edges()
            )
            self.assertEqual(12345, server.read_workflow_config(tmp)["goal_token_budget"])

    def test_write_sets_and_clears_gates_explicitly(self):
        with TemporaryDirectory() as tmp:
            saved = server.write_workflow_config(
                server.default_workflow_steps(), tmp, server.default_workflow_edges(),
                goal_verify="pytest -q", goal_token_budget=5_000_000,
            )
            self.assertEqual(("pytest -q", 5_000_000),
                             (saved["goal_verify"], saved["goal_token_budget"]))
            # None preserves; explicit values override/clear.
            server.write_workflow_config(
                server.default_workflow_steps(), tmp, server.default_workflow_edges()
            )
            self.assertEqual(5_000_000, server.read_workflow_config(tmp)["goal_token_budget"])
            cleared = server.write_workflow_config(
                server.default_workflow_steps(), tmp, server.default_workflow_edges(),
                goal_verify="", goal_token_budget=0,
            )
            self.assertEqual(("", 0), (cleared["goal_verify"], cleared["goal_token_budget"]))


class TokenBudgetGateTests(unittest.TestCase):
    def _goal_with_subtask_tokens(self, tmp, tokens):
        store = Store(Path(tmp) / ".orbit" / "messages.db")
        store.register_agent("hub", "")
        gid = store.send_message("hub", "hub", "goal", kind="task", title="G")
        goal = store.get_task_by_source_message(gid[0])
        store.update_task_metadata(goal["id"], is_goal=True)
        sub = store.get_task_by_source_message(
            store.send_message("hub", "hub", "sub", reply_to=gid[0], kind="task", title="S")[0]
        )
        run = store.create_task_run(sub["id"], worker="dev", command="x", workflow_step="implement")
        store.finish_task_run(run["id"], "succeeded", 0, tokens)
        return store, goal["id"], sub["id"]

    def test_root_goal_id_walks_to_goal(self):
        with TemporaryDirectory() as tmp:
            store, goal_id, sub_id = self._goal_with_subtask_tokens(tmp, 10)
            self.assertEqual(goal_id, server._root_goal_id(store, store.get_task(sub_id)))
            self.assertEqual(goal_id, server._root_goal_id(store, store.get_task(goal_id)))
            store.close()

    def test_no_budget_never_blocks(self):
        with TemporaryDirectory() as tmp:
            _set_workflow_field(tmp, "goal_token_budget", 0)
            store, goal_id, sub_id = self._goal_with_subtask_tokens(tmp, 10_000)
            self.assertFalse(
                server._enforce_goal_token_budget(store, tmp, store.get_task(sub_id))
            )
            store.close()

    def test_under_budget_passes(self):
        with TemporaryDirectory() as tmp:
            _set_workflow_field(tmp, "goal_token_budget", 5000)
            store, goal_id, sub_id = self._goal_with_subtask_tokens(tmp, 1000)
            self.assertFalse(
                server._enforce_goal_token_budget(store, tmp, store.get_task(sub_id))
            )
            self.assertNotEqual("blocked", store.get_task(goal_id)["task_status"])
            store.close()

    def test_over_budget_blocks_goal_notifies_once(self):
        with TemporaryDirectory() as tmp:
            _set_workflow_field(tmp, "goal_token_budget", 500)
            store, goal_id, sub_id = self._goal_with_subtask_tokens(tmp, 1200)
            self.assertTrue(
                server._enforce_goal_token_budget(store, tmp, store.get_task(sub_id))
            )
            self.assertEqual("blocked", store.get_task(goal_id)["task_status"])
            self.assertTrue(store.has_workflow_action(goal_id, "budget_exceeded"))
            self.assertTrue(store.has_unread("hub"))
            # Idempotent: still True, but no second action / re-notify.
            store.fetch_unread("hub", 60)  # drain hub inbox
            self.assertTrue(
                server._enforce_goal_token_budget(store, tmp, store.get_task(sub_id))
            )
            actions = [
                a for a in store.list_workflow_actions("all", 100)
                if a["action_type"] == "budget_exceeded" and a["task_id"] == goal_id
            ]
            self.assertEqual(1, len(actions))
            self.assertFalse(store.has_unread("hub"))  # not re-notified
            store.close()

    def test_goal_override_beats_config_default(self):
        with TemporaryDirectory() as tmp:
            _set_workflow_field(tmp, "goal_token_budget", 0)  # no global default
            store, goal_id, sub_id = self._goal_with_subtask_tokens(tmp, 1200)
            store.update_task_metadata(goal_id, token_budget=500)  # per-goal ceiling
            self.assertTrue(
                server._enforce_goal_token_budget(store, tmp, store.get_task(sub_id))
            )
            self.assertEqual("blocked", store.get_task(goal_id)["task_status"])
            store.close()

    def test_goal_override_raises_ceiling_above_default(self):
        with TemporaryDirectory() as tmp:
            _set_workflow_field(tmp, "goal_token_budget", 500)  # low global default
            store, goal_id, sub_id = self._goal_with_subtask_tokens(tmp, 1200)
            store.update_task_metadata(goal_id, token_budget=5000)  # this goal gets more
            self.assertFalse(
                server._enforce_goal_token_budget(store, tmp, store.get_task(sub_id))
            )
            self.assertNotEqual("blocked", store.get_task(goal_id)["task_status"])
            store.close()

    def test_new_task_budget_defaults_to_zero(self):
        with TemporaryDirectory() as tmp:
            store, goal_id, sub_id = self._goal_with_subtask_tokens(tmp, 10)
            self.assertEqual(0, store.get_task(goal_id)["token_budget"])
            store.close()

    def test_dispatch_step_is_frozen_over_budget(self):
        with TemporaryDirectory() as tmp:
            _set_workflow_field(tmp, "goal_token_budget", 500)
            store, goal_id, sub_id = self._goal_with_subtask_tokens(tmp, 1200)
            sub = store.get_task(sub_id)
            step = {"id": "implement", "name": "Implement", "role_id": "implementer",
                    "task_status": "in_progress"}
            member = {"agent_name": "dev", "runner_command": "echo hi"}
            job = server._dispatch_step(store, tmp, sub, step, member, "")
            self.assertIsNone(job)  # no run job queued
            self.assertEqual("blocked", store.get_task(sub_id)["task_status"])
            self.assertEqual([], store.list_run_jobs(status="pending"))
            store.close()


if __name__ == "__main__":
    unittest.main()
