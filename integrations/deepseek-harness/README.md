# Orbit for DeepSeek Harness

This directory is the installable Host Profile Bundle for `deepseek-harness`.
Its `OrbitGateway` starts one Orbit stdio Runtime per normalized Workspace,
deduplicates concurrent starts, performs the capability handshake, and routes
each call under a `harness:session:<id>` actor.

## Prerequisites

- Install `orbit` so the executable is on the Harness Host's `PATH`.

Install this directory into the target Harness Profile as a local package,
then restart that Profile. Client code accesses the generated `orbit` Remote;
it never receives the child process handle or Orbit loopback credentials.

## Compatibility

| Component | Supported range |
| --- | --- |
| Orbit integration protocol | `orbit-harness/1` |
| Harness packages | `>=0.1.1-rc.2 <0.2.0` |
| React | `^18.2.0` |
| Node.js | current Harness-supported Node runtime |

The Gateway refuses an incompatible Orbit integration protocol during startup.
Runtime codecs also reject malformed core DTOs before they reach the Client.

## Upgrade and rollback

Before upgrading, stop the Harness Profile so it releases the Workspace Runtime
ownership lock. Install the new bundle, rebuild the Profile, and restart it.
Verify the Orbit Settings row reports connected and open one historical Run.

To roll back, stop the Profile, reinstall the previous bundle version, rebuild,
and restart. Delegation and reconciliation records are additive SQLite tables;
rollback does not require deleting the Runtime database. Do not run old and new
Profiles against the same Workspace database concurrently.

## Current boundary

The Host exposes Runtime/Run inspection, Steps, Graph, Edges, cursor-based
output, bounded Artifact content, and command execution. Commands are accepted
only after the Host re-reads the Run and matches the requested command and
revision against Orbit's current `allowed_commands[]`.

The Client contributes an `orbit-run` Conversation Node, a right-side Run
detail drawer with lazy cursor-followed step output, bounded Artifact preview,
refresh restoration and keyboard/focus handling, a guarded Human Resume form, and an
Orbit Runtime row in General Settings. Browser code reaches Orbit only through
the generated Host Remote; it never connects to loopback directly.

The Orbit CLI takes a non-blocking ownership lock for the runtime database.
Starting a second managed MCP process or a manual `orbit serve` against the
same database fails instead of creating two writers.

## Subagent delegation

`harness.subagent@1.0.0` is registered before Orbit seals its Handler runtime.
At Session Bridge startup, the Host pins an actor-scoped Execution Lease from
the live `ctx.subagents` provider list, Workspace identity, delegation count,
wall-clock ceiling, and expiry. Claim validates that policy and consumes the
delegation budget atomically before the worker starts a Provider. The worker
then holds a separate renewable Job Lease. An expired Job Lease becomes an
unknown external result and is never handed to a second provider automatically;
an absent or expired Execution Lease is a known refusal before execution.
An operator may append an idempotent `confirmed_succeeded` or
`confirmed_failed` reconciliation to an unknown delegation from the Drawer.
This is an audit verdict only: the original step remains unknown and is never
resumed or retried.

An active Execution Lease is immutable except for its expiry refresh: its
Workspace, Provider allowlist, and budgets cannot be widened or retargeted, and
no lease may exceed 24 hours. Actor-scoped statistics are available to the
Harness profile. Operations may prune bounded old terminal jobs; unresolved
unknown jobs are retained until a reconciliation exists.

Core MCP responses cross runtime codecs before reaching Host or Client code.
Malformed Run, Step, Output, Artifact, or Delegation payloads fail at the
Gateway boundary instead of flowing through TypeScript assertions.

Provider names are checked against the live Harness registry before start.
Codex, Claude Code, and ACP pass through the same Provider-neutral policy gate;
unregistered providers, isolation mismatches, and unsupported concurrency are
settled before `subagents.start()`.
Write delegations fail closed unless the Host Workspace is already marked
`exclusive` or `worktree`; this plugin does not claim that a label created
isolation. The Host records a bounded Git before/after observation as the
result's Effect Manifest and rejects a declared read-only task that changed
the Workspace.
