# Unattended background Agent

> Status: parked. Orbit retains this implementation for later opt-in work,
> but the current `app.delegate@1.0.0` validator rejects
> `target: background_pool`, and workflow authoring only offers
> `target: run_initiator`. Starting this worker does not make background-pool
> Workflow configuration available.

Orbit can run one machine-wide background Agent worker separately from every
Workspace Runtime. Runtimes remain the authority for Workflow state and their
durable delegation queues; the worker reaches them only through the fixed Hub.

```text
Hub :8848
├── Runtime A ─┐
├── Runtime B ─┼── Background Agent Worker ── one child per delegation
└── Runtime C ─┘
```

## Workflow definition

An interactive step remains bound to the conversation that started the Run:

```yaml
handler: app.delegate@1.0.0
config:
  target: run_initiator
```

The retained implementation uses the following reserved shape. It is not a
currently publishable Workflow configuration:

```yaml
handler: app.delegate@1.0.0
config:
  target: background_pool
  pool: default
  effects: read
  isolation_mode: shared
  max_concurrency: 1
```

The two targets have disjoint claim paths. An Agent App cannot claim a
`background_pool` item, and the machine worker cannot claim a `run_initiator`
item.

## Start

The Hub can supervise the machine worker with Orbit's built-in Codex backend:

```bash
orbit hub serve --background-agent-backend codex
```

The equivalent environment variable is useful with `start-orbit.sh` and the
Agent App manifest:

```bash
export ORBIT_BACKGROUND_AGENT_BACKEND=codex
./start-orbit.sh
```

Serve additional pools by repeating `--background-agent-pool` when starting
the Hub. A standalone worker is also available:

```bash
orbit agent-worker \
  --backend codex \
  --pool default
```

`orbit agent-worker` defaults to the Codex backend, so `--backend codex` may
be omitted. Codex runs ephemerally, uses structured JSON output, and selects a
read-only or workspace-write sandbox from the delegation's declared effects.
It never enables the dangerous sandbox bypass.

For another Agent backend, supply a custom command instead:

```bash
orbit agent-worker --command '/absolute/path/to/orbit-agent-adapter'
```

## Adapter contract

The configured child command starts in the selected Workspace directory. It
receives one JSON delegation request on stdin and must write one JSON object to
stdout. Anything written to stderr is used as the failure reason when the
process exits unsuccessfully.

While the child runs, the worker renews its lease. Cancellation is delivered
on renewal and terminates the child. If the worker loses contact with the Hub,
it terminates the child and does not submit a result; the expired lease becomes
`unknown` and must be reconciled rather than automatically repeated.

This protocol is intentionally backend-neutral. A Codex CLI, DeepSeek Harness
CLI, or model API integration belongs in an adapter executable, not in the
Workspace Runtime.

## Current safety boundary

- Only loopback clients can access the Hub and Runtime worker endpoints.
- Background claims cross conversation actors only for explicitly addressed
  `background_pool` items.
- One worker processes one delegation at a time in this first implementation.
- Write-capable steps still require the existing project access policy,
  isolation mode, project occupancy lock, and recovery rules.
- An `unknown` delegation is never automatically requeued.
