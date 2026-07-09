# orbit

[简体中文](./README.zh-CN.md) | **English**

> A local multi-agent workflow orchestrator: it splits a coding goal into tasks that move through a configurable workflow graph, and runners headlessly invoke agent CLIs (Claude Code, Codex CLI, Gemini CLI, your own agent) to run implement → test → review → integrate. Each task runs isolated in its own git worktree, and failures loop back for rework.

```
goal/task ──▶ orbit (workflow engine + scheduler) ──▶ runner ──▶ agent CLI (Claude Code / Codex / Gemini …)
                   │                                        │
                   └──────── SQLite ~/.orbit/projects/<project>/ ◀── results (WORKFLOW_OUTCOME)
```

- One command to start: Web UI + scheduler + embedded runner, all in one process.
- Tasks flow through a configurable graph: parallel branches, merges, rework loop-backs, machine-verification gates.
- Each task runs isolated in its own git worktree; the `integrate` step merges the branch back to main. Orbit auto-initializes a git repo (with a base commit) when a project isn't one yet, and degrades to running unisolated when git isn't installed.
- Runners invoke agent CLIs headlessly and can be split by role / agent and scaled horizontally.
- State persists to SQLite: nothing lost across restarts, with timeout / stuck-run backstops.

## Table of Contents

- [Install](#install)
- [Quick Start](#quick-start)
- [Advanced Usage](#advanced-usage)
- [Workflow Engine](#workflow-engine)
- [Local Web UI](#local-web-ui)
- [Task Collaboration Model](#task-collaboration-model)
- [Role Files](#role-files)

## Install

Requires Python ≥ 3.10 and [uv](https://docs.astral.sh/uv/). `git` is used for
per-task worktree isolation (orbit auto-creates a repo if the project isn't one
yet); the runners invoke agent CLIs (Claude Code, Codex, …), so install those you
plan to use. Runs natively on macOS, Linux, and Windows (process control is
handled per-OS — POSIX process groups, Windows `taskkill`).

**Global CLI (recommended)** — install once, then run `orbit` in any project on
any machine:

```bash
uv tool install git+https://github.com/TNJ2026/orbit.git
uv tool update-shell            # ensure ~/.local/bin is on PATH (first time only)
# update later:  uv tool upgrade orbit
```

**From a local checkout** (to hack on orbit itself):

```bash
git clone https://github.com/TNJ2026/orbit.git
uv tool install --editable ./orbit   # global `orbit` that reflects your edits live
# or run in-place without installing:  cd orbit && uv run orbit serve
```

`uv run orbit …` and `uv tool` create the environment on first use, so a separate
`uv sync` is not required. Without uv: `pip install -e .`.

## Quick Start

Zero config, from any repo:

```bash
cd <your-project>          # orbit orchestrates the project in the current directory
orbit up                   # gitignore .orbit/ + agents/, then serve with packaged defaults
```

Then open `http://127.0.0.1:8848/ui` and:

1. **Team** page — assign an agent CLI (Claude Code, Codex, …) to each core role (hub / implementer / reviewer). `up` ships default role prompts and a default workflow but an **empty team**, so this one-time step is required before a goal can run.
2. Give **hub** a goal — the engine splits it into subtasks and drives them through the workflow.

`up` copies nothing you need to commit (it only appends `.orbit/` and `agents/` to `.gitignore`; a UI role edit still materializes `agents/` on demand but stays out of git) and accepts every `serve` flag (`--host` / `--port` / `--db` / `--no-runner` / `--runner-concurrency`). Not installed globally? `uvx --from git+https://github.com/TNJ2026/orbit.git orbit up` pulls it in on the fly. Already prepared, or happy with the defaults? Just `orbit serve`. (From a local checkout without a global install, prefix any command with `uv run`.)

## Advanced Usage

### Customize & commit config — `orbit config`

`orbit up` / `orbit serve` need no setup. Run `orbit config` (formerly `orbit init`, still accepted as an alias) only when you want to edit role prompts, customize the workflow, and commit them for the team: it writes `agents/*.md`, `.orbit/workflow.json`, `team.json`, and a `CLAUDE.md` section into the repo — **intentionally not gitignored** so they can be committed and shared. It also seeds a default team by spreading the core roles over your installed agent CLIs, so a goal can often run right after.

### Multi-process: decoupled serve + standalone runners

By default `serve` embeds one runner, so **restarting serve interrupts in-flight steps** (they auto-rerun once the lease expires). For restart safety / multi-host / horizontal scaling, split scheduling from execution:

```bash
# Terminal 1: UI / scheduling only, no embedded worker
orbit serve --no-runner

# Terminal 2+: standalone runners (restarting serve doesn't touch them)
orbit runner --name runner-local
```

Runs then live in separate processes, so restarting serve doesn't kill in-flight tasks — scheduling pauses briefly, runners keep going. A runner is a stateless worker; split it by agent / role and scale it:

```bash
orbit runner --roles implementer --max-concurrency 2   # 2 parallel implementation workers
orbit runner --roles reviewer --agent antigravity      # only antigravity's reviews
orbit runner --project /path/to/repo --name box-a      # explicit project
```

- `--agent NAME` (repeatable): only claim jobs assigned to this agent.
- `--roles a,b`: only claim jobs for these workflow roles (roles resolve to steps per the workflow config).
- `--max-concurrency N`: run N jobs in parallel, default 5 (each worker leases as `<name>-0/-1/…`).
- `--project PATH`: explicit project root instead of cwd resolution.
- `--once`: claim one job, run it, and exit (good for scripts / CI).

Claiming is an atomic DB-level operation (`UPDATE ... WHERE status=... AND lease<=now`), so concurrent runners never claim the same job twice; if a runner dies, its job is re-claimed once the lease expires.

### Database & operational model

The default database path looks like `~/.orbit/projects/<project-dir-name>-<path-hash>/messages.db`. The project root is probed upward via the nearest `.git` / `pyproject.toml` — starting from a subdirectory still resolves to the same database. To share manually or point at an old database, override with `--db`.

**One project = one daemon = one port.** The db is decided by the daemon's launch directory. To run several projects at once, start a separate daemon per project on distinct `--port`s.

Upgrading from an older version: the old global database at `~/.dev_loop/messages.db` is no longer loaded by default (a notice is printed on startup). To keep using it: `orbit serve --db ~/.dev_loop/messages.db`; to migrate it into a project, `cp` the file to the new path printed at startup.

### Access points

After startup, open the local Web UI: `http://127.0.0.1:8848/ui` — the main entry for observing and operating tasks / workflows / queues. Everything goes through `/api/*` JSON routes (local-only), which scripts can also call directly.

Each daemon writes the current project into `~/.orbit/projects/index.json` on startup. Any project's `/ui` can see other project daemons from this index: online projects switch directly via the Project dropdown; offline ones show metadata only and need their daemon started in that project's directory. The cross-project UI is an aggregated view only — writes still go to the selected project's own daemon.

## Workflow Engine

The workflow engine is logically three layers — **Scheduler** (decides the next step, advances), **Runner/Worker** (executes agent CLIs), and the `run_jobs` queue between them. They ship in one process by default, but can be split apart:

| Layer | Responsibility |
|---|---|
| **Scheduler** (thread inside serve) | Enqueues "run this step" into `run_jobs`; single-point-consumes finished jobs and advances the workflow (dispatch / rework / accept); runs timeout / health backstops |
| **Runner / Worker** | Claims jobs from `run_jobs` (with lease + heartbeat), executes each agent's CLI, streams stdout/stderr, parses the outcome, and writes the result back to the job |

Default workflow (design-first): `intake(hub) → product_design → [ui_design ∥ architecture] → plan(hub) → implement → test → review → integrate(hub) → accept(hub)`. The goal runs the design steps once, then `plan` splits it into implementation subtasks (one per module) that each begin at `implement`; `review` has a loop-back edge to `implement`, and `test` can carry a `verify` command as a machine gate. The runner hands the step prompt to an agent CLI headlessly; the agent reports by printing `WORKFLOW_OUTCOME: done|rework|blocked` at the end (see `agents/_protocol.md`).

### Default: single process

```bash
orbit serve        # UI + Scheduler + embedded Runner, all in one process
```

`serve` **embeds one in-process runner** by default (name `serve-embedded`, concurrency 5), so after kicking off a goal you don't need to start a runner manually — enqueue job → embedded runner executes → scheduler advances, fully automatic. The **Jobs** tab in the UI shows queue state (pending / running / finished / done).

> ⚠️ The embedded runner shares serve's lifecycle: **restarting serve interrupts in-flight steps** (the step auto-reruns once its lease expires). For restart safety / multi-host / horizontal scaling, split them apart — see [Advanced Usage → Multi-process](#multi-process-decoupled-serve--standalone-runners).

**Job lifecycle:** `pending → running` (runner claims) `→ finished` (runner done, reports outcome) `→ done` (scheduler advances to the next step).

### Design-first & the `decompose` step

Where a goal splits into subtasks is set by the step flagged `decompose: true`. The default workflow flags `plan`, so the goal itself runs the design steps once (`intake → product_design → [ui_design ∥ architecture] → plan`), then `plan` (hub) emits the subtask JSON using the design output as context. Each subtask begins at the decompose step's successors (`implement` onward), inheriting that output — so the design steps run **once** on the goal, not per subtask, and subtasks partition cleanly by the architecture's modules.

Move the split by flagging a different step in `.orbit/workflow.json`; with **no** `decompose` flag, a goal splits at the entry step instead (`intake`), and every subtask re-runs the whole workflow — simpler, but design then happens per subtask. The flag is config-only (edit the JSON, same as `isolate`/`integrate`); a decompose step is auto-required, never isolated, and must have a forward successor for its subtasks to start at.

### Goal convergence check (goal_verify)

Once all of a goal's business subtasks self-test and close, orbit runs the `goal_verify` command on the integrated main tree to accept the whole result objectively.

- **Auto-detected by default**: with no `goal_verify` set, orbit infers a common test command from project markers (`npm test`, `cargo test`, `python -m unittest discover -s tests`, …). Good for a quick try — confirm the guess in the Workflow panel.
- **Declare it explicitly for real use**: save the command into `.orbit/workflow.json` (via UI/CLI). It should be idempotent, runnable offline, and cover unit/integration tests. Point it at a script (`./scripts/goal-verify.sh`) for monorepos.
- It runs in the project root under a hard timeout (`VERIFY_HARD_TIMEOUT_SECONDS`, default 900s). Pass → goal `accepted`; fail → `stalled` + hub is notified.
- `goal_verify` is a plain shell/test command — it uses no LLM, so it **consumes no tokens and does not count against `goal_token_budget`**.

### Token accounting & budget

Every run records a token count, aggregated per goal so you can watch and cap spend.

- **Per run**: orbit parses each run's usage — preferring the agent CLI's own usage line (accurate), falling back to the `TOKENS_USED: <n>` sentinel every runner is asked to print (approximate, model-estimated; see `agents/_protocol.md`). The count is stored on the run and shown in its run log; the last usage line wins, since CLI usage lines are cumulative.
- **Per goal**: usage is summed across the goal's whole subtree (goal + subtasks + step cards). The **Goals** tab shows the running total.
- **Budget cap**: set `goal_token_budget` in the workflow config as a hard ceiling on a goal's total subtree tokens (a per-goal override beats the workflow default; `0` = unlimited). When a goal crosses its budget, further dispatch is frozen and hub is notified (the goal goes `blocked`).
- `goal_verify` and step `verify` commands run no LLM, so they cost **no tokens** and never count against the budget.

## Local Web UI

`/ui` is a local console for observing and operating the workflow:

- Switch between running project daemons via the Project dropdown at the top
- View installed common agent CLIs and the team config
- **Board**: tasks by status column (todo / in progress / testing / review / blocked / done)
- **Workflow**: visually edit the workflow graph (steps, roles, edges, `verify` command, goal budget)
- **Jobs**: the `run_jobs` execution queue (status / outcome / claimant / lease expiry) to confirm the runner is consuming
- **Goals**: goal progress and subtree token spend; **Force End** to hard-stop (kill running runners + close the whole tree)
- Per-step run logs (command, exit code, stdout/stderr tail)

The UI reaches the local store only through `/api/*` JSON routes, and only serves local clients.

## Task Collaboration Model

Give `hub` a goal; the engine splits it into business subtasks that run through the workflow in parallel, each step executed headlessly by the matching role's agent.

### Roles & constraints

Roles in the default workflow (see `agents/`):

- `hub`: orchestrator. Splits the goal, integrates/merges, does final acceptance; no big implementation or review work.
- `implementer`: makes the code change and self-tests.
- `reviewer`: hunts bugs, test gaps, design risk; reviews only, doesn't edit code.
- `tester`: designs and runs tests, reproduces failures, reports coverage risk.
- `architect` / `product_designer` / `ui_designer` / `security_auditor` / `refactorer`: design / audit / refactor roles wired in as needed.

Constraints:

1. By default only `hub` (in the `integrate` step) writes the main worktree; other roles work in their own worktrees.
2. A worker handles exactly one clearly-bounded small task at a time.
3. A worker writes artifacts to files, ends output with a one-line conclusion + artifact paths, and prints `WORKFLOW_OUTCOME`.

### Task content format

A task's `content` should be structured so an agent can execute it unambiguously:

```
Task Type: review

Context:
- Repo path: ...
- Change under review: ...

Deliverable:
- Findings ordered by severity
- Missing tests
- Residual risk
```

### Task statuses

| Status | Meaning |
|---|---|
| `created` | Created, not yet in the workflow |
| `assigned` | Dispatched to the target role |
| `in_progress` | A step's runner is executing |
| `reviewing` | At a review step |
| `accepted` | Hub accepted the result (a terminal state) |
| `blocked` | Blocked, needs input or an environment change |
| `stalled` | Parent goal stalled because a subtask is blocked |
| `closed` | Task archived (a terminal state) |

## Role Files

The `agents/` directory ships ready-to-use role definitions: `_protocol.md` (shared **execution conventions**), `hub.md` (orchestrator), `implementer.md`, `reviewer.md`, `tester.md`, and more. The runner injects the matching role's `.md` into the step prompt, and the agent works from it.

`hub` can also run as your **interactive main session** — splitting goals, integrating, and accepting:

```bash
claude --append-system-prompt "$(cat agents/hub.md)"
```

`CLAUDE.md` / `AGENTS.md` / `GEMINI.md` are thin entry points holding only project facts and role pointers.

Add a role: copy `agents/_template.md` to `agents/<role-name>.md` and replace the placeholders — the template carries the naming rules and the criteria for splitting responsibilities.
