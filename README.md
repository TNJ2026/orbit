# Orbit

[简体中文](./README.zh-CN.md) | **English**

> A local workflow runtime for agent work. A workflow is a static graph; a run
> advances by appending events to a log, and every node is executed by a handler
> that was registered before the runtime started. Restart it mid-run and it
> picks up exactly where it was, because the log — not a process — is the state.

```
workflow definition ──▶ plan ──▶ jobs ──▶ handlers
                                   │
        event log (SQLite) ◀───────┘──▶ read models ──▶ /api/v1, /mcp, /ui
```

- **Durable by construction.** Commands are idempotent, jobs are leased, and a
  handler that loses its lease mid-call reports an *unknown* result rather than
  being silently retried into a second side effect.
- **The server decides what may be done.** Every action a client can take is
  advertised as an `allowed_commands[]` entry with a target and an expected
  version. Clients never invent a mutation URL.
- **Nothing runs that was not registered.** The handler registry is sealed
  before the first worker starts, and a plan is bound to the exact manifest
  fingerprint it was compiled against.
- **One process.** `orbit serve` is the runtime, the API, the UI, the workers
  and the timer dispatcher. State lives in SQLite under `~/.orbit/projects/`.

## Scope

orbit runs **static** workflow graphs: a published workflow compiles to a plan
whose shape does not change while it runs. Dynamic planning — foreach groups,
subflows and agentic regions that rewrite their own graph — is implemented in
the domain and service layers but is not reachable from a running system: the
DSL has no syntax for it and no loop drives the planner. See
[docs/migration/unwired-capabilities.md](docs/migration/unwired-capabilities.md).

## Table of Contents

- [Install](#install)
- [Quick Start](#quick-start)
- [CLI Reference](#cli-reference)
- [HTTP API](#http-api)
- [MCP](#mcp)
- [Development Tools](#development-tools)
- [Upgrading from the Pre-Cutover Engine](#upgrading-from-the-pre-cutover-engine)

## Install

Requires Python ≥ 3.10 and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/TNJ2026/orbit.git
uv tool update-shell            # ensure ~/.local/bin is on PATH (first time only)
```

From a local checkout:

```bash
git clone https://github.com/TNJ2026/orbit.git
uv tool install --editable ./orbit   # global `orbit` reflecting your edits
# or run in place:  cd orbit && uv run orbit serve
```

## Quick Start

```bash
cd <your-project>
orbit workflow publish my-workflow.yaml --catalog catalog.json --expected-version 0
orbit serve
```

Open `http://127.0.0.1:8848/ui`. The console shows runs, what each one is
waiting on, its plan, and an inbox of anything waiting on a person. It is
bilingual (`zh-CN` / `en-US`) and follows your browser's language.

Or drive it from the terminal:

```bash
orbit run start my-workflow --input '{"value": 1}'
orbit run inspect run:abc123          # status, open responsibilities, recent errors
```

`orbit serve` binds loopback and treats the person at the keyboard as the
operator. A request that did not arrive over loopback gets no identity at all,
so exposing the port yields 401s rather than an open runtime.

## CLI Reference

```bash
orbit --version
orbit <command> --help
```

### `orbit serve`

Start the runtime.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Bind address. |
| `--port` | `8848` | Port. |
| `--db` | per-project | Database path; defaults to `~/.orbit/projects/<project>/runtime.db`. |
| `--runner-concurrency` | `5` | Jobs the in-process workers run in parallel. |
| `--no-agent-discovery` | off | Skip probing for installed agent CLIs. |
| `--dev-tools` | off | Register the trusted git and verify tools (see below). |
| `--acknowledge-discard-legacy-data` | off | One-time cutover acknowledgement (see below). |

### `orbit run`

```bash
orbit run start <workflow_id> [--input JSON] [--goal TEXT] [--workflow-version N]
                              [--idempotency-key KEY] [--json]
orbit run inspect <run_id> [--json]
```

`run start` writes through the same application service as the HTTP API, so a
start from the terminal and a start from the UI produce identical events. Pass
`--idempotency-key` to make re-running the whole command idempotent — without
it, each invocation is a new run.

### `orbit workflow`

```bash
orbit workflow validate <file> --catalog <catalog.json> [--json]
orbit workflow compile  <file> --catalog <catalog.json> [--output PATH]
orbit workflow publish  <file> --catalog <catalog.json> --expected-version N [--db PATH]
```

`--expected-version` is a compare-and-set: publishing against a stale version
fails rather than overwriting someone else's.

### `orbit db check`

Audits event, projection, receipt and snapshot integrity. Read-only unless you
pass `--drop-invalid-snapshots`, which deletes only corrupt snapshot caches —
events and projections are never modified. Exits non-zero when the database is
not sound.

## HTTP API

Everything lives under `/api/v1`. Reads are cursor-paged and carry a schema
version; writes require an `idempotency-key` header and an `expected_version`
in the body.

| Route | Purpose |
| --- | --- |
| `GET /api/v1/runs` | Runs, newest first. `?active=true` to filter. |
| `GET /api/v1/runs/{id}` | Summary: status, workflow, budget. |
| `GET /api/v1/runs/{id}/responsibilities` | What the run is waiting on, plus the commands you may issue. |
| `GET /api/v1/runs/{id}/timeline` | The event log. |
| `GET /api/v1/runs/{id}/errors` | Failures, as a projection rather than a filtered timeline. |
| `GET /api/v1/runs/{id}/plan` | Plan definition: nodes, handlers, edges. No run state. |
| `GET /api/v1/runs/{id}/plan/overlay` | Run state per node, stamped with its plan version. |
| `GET /api/v1/runs/{id}/plan/diff` | What changed between two plan versions. |
| `GET /api/v1/inbox` | Everything waiting on a person, across all runs. |
| `GET /api/v1/recovery` | What the runtime believes is stuck. |
| `GET /api/v1/handler-catalog` | Installed handlers and discovered agent CLIs. |
| `POST /api/v1/runs` | Start a run. |
| `POST /api/v1/runs/{id}/cancel` | Cancel at a known version. |
| `POST /api/v1/runs/{id}/budget` | Grant more budget. |
| `POST /api/v1/human-tasks/{id}/claim` \| `/submit` | Claim or decide a human task. |
| `POST /api/v1/recovery/apply` | Apply recovery actions. |

Definition and overlay are deliberately separate: a single "node with a status"
blob makes it impossible to tell a replanned graph from a retried node, and
invites a client to paint last version's statuses onto this version's plan.

`/health/live` and `/health/ready` sit outside `/api/v1`. Readiness reports the
database, migrations, the sealed registry, and every background loop by name.

## MCP

`POST /mcp` speaks JSON-RPC 2.0 with tools `list_runs`, `inspect_run`,
`start_run` and `cancel_run`, behind the same identity and authorisation as the
HTTP API. Tool discovery is open; every tool call needs a scope.

```toml
# .codex/config.toml
[mcp_servers.orbit]
url = "http://127.0.0.1:8848/mcp"
```

## Development Tools

`orbit serve --dev-tools` registers four trusted tools — `git.status`,
`git.diff`, `git.integrate` and `verify` — that run inside a git worktree the
runtime provisions per workspace ref.

A workflow **selects** a tool by name and passes bounded arguments. It cannot
supply a program, a flag, a path or a shell string: each adapter owns a frozen
argv, and verification runs a *named profile* registered by the composition
root. There is no shell anywhere in that path, and the child's environment is
built explicitly rather than inherited.

Capability policy is applied before the registry is sealed, so a tool the
deployment did not grant does not exist rather than being refused later.
`git.integrate` is the only tool that writes, so it is the only one whose lost
lease produces an unknown result and an escalation instead of a retry.

## Upgrading from the Pre-Cutover Engine

Earlier versions of orbit were a different system: a task queue with a
`messages.db`, `orbit start` / `up` / `init` / `config` / `runner`, and an
unversioned `/api/tasks` surface. All of it is gone.

If a project still holds a pre-migration database, `orbit serve` refuses to
start and exits with code 3. Its contents — including any runtime data written
before the cutover — are abandoned, not migrated. orbit will not open, copy or
delete those files; that is your call. To continue:

```bash
orbit serve --acknowledge-discard-legacy-data
```

That writes a `0600` marker recording which paths you acknowledged and when,
and nothing else. There is deliberately no import path: a half-supported import
is exactly how two sources of truth come back.

## Development

```bash
.venv/bin/python -m unittest discover -s tests
node --test tests/ui/client_modules.test.mjs   # client modules, if node is installed
```

The browser suite needs its own extras and skips without them:

```bash
uv pip install -e '.[dev]'
python -m playwright install chromium
```
