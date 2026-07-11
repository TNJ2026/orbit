# orbit

[简体中文](./README.zh-CN.md) | **English**

> A local multi-agent workflow orchestrator: it splits a coding goal into tasks that move through a configurable workflow graph, and runners headlessly invoke agent CLIs (Claude Code, Codex CLI, Gemini CLI, your own agent) to run implement → review → test → integrate. Each task runs isolated in its own git worktree, and failures loop back for rework.

```
goal/task ──▶ orbit (workflow engine + scheduler) ──▶ runner ──▶ agent CLI (Claude Code / Codex / Gemini …)
                   │                                        │
                   └──────── SQLite ~/.orbit/projects/<project>/ ◀── results (WORKFLOW_OUTCOME)
```

- One command to start: Web UI + scheduler + embedded runner, all in one process.
- Tasks flow through a configurable graph: parallel branches, merges, rework loop-backs, machine-verification gates.
- Each task runs isolated in its own git worktree; the `integrate` step merges the branch back to main. Orbit auto-initializes a git repo (with a base commit) when a project isn't one yet, and degrades to running unisolated when git isn't installed.
- Runners invoke agent CLIs headlessly and can be filtered by step / Agent and scaled horizontally.
- State persists to SQLite: nothing lost across restarts, with timeout / stuck-run backstops.

## Table of Contents

- [Install](#install)
- [Quick Start](#quick-start)
- [Advanced Usage](#advanced-usage)
- [Workflow Engine](#workflow-engine)
- [Local Web UI](#local-web-ui)
- [Task Collaboration Model](#task-collaboration-model)

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
orbit start                # gitignore .orbit/, then serve with packaged defaults
```

Then open `http://127.0.0.1:8848/ui` and:

1. On **Workflow**, double-click each required step and select an installed Agent CLI. Its built-in command is used automatically; an explicit step Command remains available as an override.
2. Start a Goal; the engine runs design once, decomposes it when configured, and drives each work item through the remaining steps.

`start` copies no configuration into the repo; it only appends `.orbit/` to `.gitignore`. It accepts every `serve` flag (`--host` / `--port` / `--db` / `--no-runner` / `--runner-concurrency`). (`orbit up` is a back-compat alias.) Not installed globally? `uvx --from git+https://github.com/TNJ2026/orbit.git orbit start` pulls it in on the fly. From a local checkout, prefix commands with `uv run`.

## Advanced Usage

### Customize & commit config — `orbit config`

`orbit start` / `orbit serve` need no generated config. Run `orbit config` (`orbit init` remains an alias) only when you want to materialize `.orbit/workflow.json`, customize it, and commit the workflow for collaborators.

### Multi-process: decoupled serve + standalone runners

By default `serve` embeds one runner, so **restarting serve interrupts in-flight steps** (they auto-rerun once the lease expires). For restart safety / multi-host / horizontal scaling, split scheduling from execution:

```bash
# Terminal 1: UI / scheduling only, no embedded worker
orbit serve --no-runner

# Terminal 2+: standalone runners (restarting serve doesn't touch them)
orbit runner --name runner-local
```

Runs then live in separate processes, so restarting serve doesn't kill in-flight tasks — scheduling pauses briefly, runners keep going. A runner is a stateless worker; filter it by Agent / step and scale it:

```bash
orbit runner --steps implement --max-concurrency 2     # 2 parallel implementation workers
orbit runner --steps review --agent antigravity        # only antigravity's reviews
orbit runner --project /path/to/repo --name box-a      # explicit project
```

- `--agent NAME` (repeatable): only claim jobs assigned to this agent.
- `--steps a,b`: only claim jobs whose workflow step id is in this list.
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

Default workflow (design-first): `intake → product_design → ui_design → architecture → decompose → implement → review → test → integrate`. The design steps run sequentially; the goal runs them once, then `decompose` splits it into implementation subtasks that begin at `implement`. Review, test, and integrate can loop back to implement on rework. Integrate merges the task branch when present, checks acceptance criteria, verifies main, and closes the task on `done`. Each step selects its Agent directly; the runner uses that Agent's built-in CLI command unless the step has an explicit Command override.

Every standard step includes an editable default **Step prompt**. Double-click a node on Workflow to customize it. The custom text refines the generated instructions; engine-owned safety contracts (including read-only Review/Test boundaries), dynamic context, and the output protocol remain protected.

### Default: single process

```bash
orbit serve        # UI + Scheduler + embedded Runner, all in one process
```

`serve` **embeds one in-process runner** by default (name `serve-embedded`, concurrency 5), so after kicking off a goal you don't need to start a runner manually — enqueue job → embedded runner executes → scheduler advances, fully automatic. The **Jobs** tab in the UI shows queue state (pending / running / finished / done).

> ⚠️ The embedded runner shares serve's lifecycle: **restarting serve interrupts in-flight steps** (the step auto-reruns once its lease expires). For restart safety / multi-host / horizontal scaling, split them apart — see [Advanced Usage → Multi-process](#multi-process-decoupled-serve--standalone-runners).

**Job lifecycle:** `pending → running` (runner claims) `→ finished` (runner done, reports outcome) `→ done` (scheduler advances to the next step).

Each step instance persists structured execution details. Orbit records its task,
step metadata, and upstream result as `step_inputs`; runners are prompted to end
with `RESULT_SUMMARY: ...` and `ARTIFACTS: ["path-or-uri", ...]`. These become the
step card's `result_summary` and `artifacts` API fields. Older runner output is
kept compatible by falling back to the cleaned output as the result summary.

### Design-first & the `decompose` step

Where a goal splits into subtasks is set by the step flagged `decompose: true`. The default workflow uses the `decompose` step, so the goal itself runs the design steps once (`intake → product_design → ui_design → architecture → decompose`), then Decompose emits the subtask JSON using the design output as context. Each subtask begins at the decompose step's successors (`implement` onward), inheriting that output — so the design steps run **once** on the goal, not per subtask, and subtasks partition cleanly by the architecture's modules.

Move the split by flagging a different step in `.orbit/workflow.json`. With **no** `decompose` flag, the goal does not create work items: it traverses the complete workflow itself, which is useful for research, approval, publishing, and other single-subject processes. The flag is config-only (edit the JSON, same as `isolate`/`integrate`); a decompose step is auto-required, never isolated, and must have a forward successor for its work items to start at.

### Goal convergence check (goal_verify)

Once all of a goal's work items close—or a non-decomposing goal reaches its terminal step—orbit runs the `goal_verify` command on the resulting main tree to accept the whole result objectively.

- **Set per goal**: type the command in the **Goal verify command** box on the Goals page when you start a goal — it applies to that goal only. Leave it empty to auto-detect (see below).
- **Auto-detected when empty**: with no command set, orbit infers a common test command from project markers (`npm test`, `cargo test`, `python -m unittest discover -s tests`, …). Good for a quick try.
- **Declare it explicitly for real use**: it should be idempotent, runnable offline, and cover unit/integration tests. Point it at a script (`./scripts/goal-verify.sh`) for monorepos.
- It runs in the project root under a hard timeout (`VERIFY_HARD_TIMEOUT_SECONDS`, default 900s). Pass → goal `accepted`; fail → `stalled` + hub is notified.
- `goal_verify` is a plain shell/test command — it uses no LLM, so it **consumes no tokens and does not count against a goal's token budget**.

### Token accounting & budget

Every run records a token count, aggregated per goal so you can watch and cap spend.

- **Per run**: orbit parses each run's usage — preferring the agent CLI's own usage line (accurate), falling back to the `TOKENS_USED: <n>` sentinel in the engine output protocol (approximate, model-estimated). The count is stored on the run and shown in its run log; the last usage line wins, since CLI usage lines are cumulative.
- **Per goal**: usage is summed across the goal's whole subtree (goal + subtasks + step cards). The **Goals** tab shows the running total.
- **Budget cap**: set a token budget **per goal** when you start it (in the goal dialog) as a hard ceiling on that goal's total subtree tokens; `0` = unlimited. When a goal crosses its budget, further dispatch is frozen and hub is notified (the goal goes `blocked`).
- `goal_verify` and step `verify` commands run no LLM, so they cost **no tokens** and never count against the budget.

## Local Web UI

`/ui` is a local console for observing and operating the workflow:

- Switch between running project daemons via the Project dropdown at the top
- View installed Agent CLIs and their built-in commands
- **Board**: tasks by status column (todo / assigned / in progress / blocked / done)
- **Workflow**: visually edit the workflow graph (steps, Agents, prompts, edges, and per-step commands)
- **Jobs**: the `run_jobs` execution queue (status / outcome / claimant / lease expiry) to confirm the runner is consuming
- **Goals**: goal progress and subtree token spend; **Force End** to hard-stop (kill running runners + close the whole tree)
- **Settings**: UI language, **max rework rounds** (2–5, how many times a step may loop back before the engine blocks it), and **max concurrent tasks** (1–6, how many workflow steps run at once across all runners). Saved to `.orbit/settings.json`.
- Per-step run logs (command, exit code, stdout/stderr tail)

The UI reaches the local store only through `/api/*` JSON routes, and only serves local clients.

## Task Collaboration Model

Start a goal; the engine splits it into business subtasks that run through the workflow in parallel, with each step executed by its configured Agent.

### Step Agents & constraints

Each workflow step starts with no Agent and owns its editable prompt — you assign Agents on the Workflow page. A goal will not start until every reachable step has at least one runnable Agent; the start preflight names any unset step. When a step has multiple Agents, Orbit rotates that step's dispatches across them using persistent per-step history (round-robin over distinct tasks). Rework returns a task to the same Agent that first ran the step, so an implementer keeps its own worktree instead of handing a half-done change to the next CLI. Agent selection is independent per step, so Review/Test/Integrate retain their own execution pools.

Constraints:

1. Only the `integrate` step writes the main worktree; isolated steps share the task's own worktree.
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
| `assigned` | Dispatched to the target Agent |
| `in_progress` | A step's runner is executing |
| `blocked` | Blocked, needs input or an environment change |
| `closed` | Done (terminal) |

These task statuses are fixed across all workflows. Domain-specific phases such
as design, review, testing, or approval belong to workflow steps, not task status.
