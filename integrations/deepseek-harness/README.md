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
