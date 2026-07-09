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
- [Start](#start)
- [Workflow Engine](#workflow-engine)
- [Local Web UI](#local-web-ui)
- [Task Collaboration Model](#task-collaboration-model)
- [Role Files](#role-files)

## Install

Requires Python ≥ 3.10 and [uv](https://docs.astral.sh/uv/). `git` is used for
per-task worktree isolation (orbit auto-creates a repo if the project isn't one
yet); the runners invoke agent CLIs (Claude Code, Codex, …), so install those you
plan to use.

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

## Start

```bash
cd <your-project>                  # orbit orchestrates the project in the current directory
orbit serve                        # default 127.0.0.1:8848; db split per current project directory
orbit serve --port 9000 --db /tmp/test.db
```

(From a local checkout without a global install, prefix any command with `uv run`.)

Three ways to bring it up — pick by need:

| Command | Writes files into the repo? | Use when |
|---|---|---|
| `orbit up` | Only appends to `.gitignore` (ignores `.orbit/`) | Zero-config use from another repo; ships with packaged role/workflow defaults |
| `orbit serve` | No | Already `init`-ed, or happy with the defaults |
| `orbit init` + `orbit serve` | Writes role / workflow / config files | Customize role prompts & workflow and commit them for the team |

### Zero-config start in another repo

When you don't want to copy role/config files into another repo, use `orbit up`: it first appends the state dir (`.orbit/`) to that repo's `.gitignore`, then serves using the **packaged role and workflow defaults** — nothing that needs to be committed lands in the repo. It accepts every `serve` flag (`--host` / `--port` / `--db` / `--no-runner` / `--runner-concurrency`).

```bash
orbit up                                                        # orbit already installed
uvx --from git+https://github.com/TNJ2026/orbit.git orbit up    # not installed? uvx pulls it in on the fly
```

### Customize inside a repo

When you need to edit role prompts, customize the workflow, and commit them for the team, use `orbit init`: it writes `agents/*.md`, `.orbit/workflow.json`, `team.json`, and a `CLAUDE.md` section into the repo. These are **intentionally not gitignored** so they can be committed and shared.

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

Default workflow: `intake(hub) → product_design → [ui_design ∥ architecture] → implement → test → review → integrate(hub) → accept(hub)`, where `review` has a loop-back edge to `implement` and `test` can carry a `verify` command as a machine gate. The runner hands the step prompt to an agent CLI headlessly; the agent reports by printing `WORKFLOW_OUTCOME: done|rework|blocked` at the end (see `agents/_protocol.md`).

### Default: single process

```bash
orbit serve        # UI + Scheduler + embedded Runner, all in one process
```

`serve` **embeds one in-process runner** by default (name `serve-embedded`, concurrency 5), so after kicking off a goal you don't need to start a runner manually — enqueue job → embedded runner executes → scheduler advances, fully automatic. The **Jobs** tab in the UI shows queue state (pending / running / finished / done).

> ⚠️ The embedded runner shares serve's lifecycle: **restarting serve interrupts in-flight steps** (the step auto-reruns once its lease expires). For restart safety / multi-host / horizontal scaling, use the decoupled mode below.

**Job lifecycle:** `pending → running` (runner claims) `→ finished` (runner done, reports outcome) `→ done` (scheduler advances to the next step).

### Decoupled: serve without runner + standalone runners

```bash
# Terminal 1: UI / scheduling only, no embedded worker
orbit serve --no-runner

# Terminal 2+: standalone runners (restarting serve doesn't affect them; multi-instance)
orbit runner --name runner-local
```

In this mode runs live in separate runner processes, so **restarting serve does not kill in-flight tasks** — scheduling pauses briefly, runners keep going.

### Multi-instance runners

A runner is a stateless worker; it can be split by agent / role and run in parallel:

```bash
orbit runner --roles implementer --max-concurrency 2   # 2 parallel implementation workers
orbit runner --roles reviewer --agent antigravity      # only antigravity's reviews
orbit runner --project /path/to/repo --name box-a       # explicit project
```

- `--agent NAME` (repeatable): only claim jobs assigned to this agent.
- `--roles a,b`: only claim jobs for these workflow roles (roles resolve to steps per the workflow config).
- `--max-concurrency N`: run N jobs in parallel, default 5 (each worker has its own lease name `<name>-0/-1/…`).
- `--project PATH`: explicit project root instead of cwd resolution.
- `--once`: claim one job, run it, and exit (good for scripts / CI).

Claiming is an atomic DB-level operation (`UPDATE ... WHERE status=... AND lease<=now`), so concurrent runners never claim the same job twice; if a runner dies, its job is re-claimed by another once the lease expires.

### Goal convergence check (goal_verify)

Once all of a goal's business subtasks self-test and close, orbit runs the `goal_verify` command on the integrated main tree to accept the whole result objectively.

- **Auto-detected by default**: with no `goal_verify` set, orbit infers a common test command from project markers (`npm test`, `cargo test`, `python -m unittest discover -s tests`, …). Good for a quick try — confirm the guess in the Workflow panel.
- **Declare it explicitly for real use**: save the command into `.orbit/workflow.json` (via UI/CLI). It should be idempotent, runnable offline, and cover unit/integration tests. Point it at a script (`./scripts/goal-verify.sh`) for monorepos.
- It runs in the project root under a hard timeout (`VERIFY_HARD_TIMEOUT_SECONDS`, default 900s). Pass → goal `accepted`; fail → `stalled` + hub is notified.
- `goal_verify` is a plain shell/test command — it uses no LLM, so it **consumes no tokens and does not count against `goal_token_budget`**.

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
