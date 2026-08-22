# Orbit for DeepSeek Harness

This directory is the installable P0 Profile Bundle for
`deepseek-harness`. It starts Orbit's stdio MCP server through Harness's
official MCP Client and exposes only the `harness` compatibility profile.

## Prerequisites

- Install `orbit` so the executable is on the Harness Host's `PATH`.
- Start the Harness Profile with its working directory set to the Orbit
  workspace to control. Orbit derives the default project and database from
  that directory.

Install this directory into the target Harness Profile as a local package,
then restart that Profile. The model receives tools named
`mcp__orbit__<tool-name>`.

The bundle uses the dedicated actor `harness:profile`, so its active Run does
not consume the local Orbit UI actor's default single-goal slot. The P0 bundle
still serializes Runs started through that one Harness Profile; Session- or
Lease-scoped actors require the dynamic Gateway described below.

## P0 boundary

This bundle is deliberately single-workspace. The upstream MCP Client's
stdio `cwd` is plugin-instance configuration, not Session context, so a
static bundle cannot safely route different Harness Sessions to different
Orbit workspaces. The full integration must replace this process binding
with the planned Host-side `OrbitGateway`, keyed by a normalized Workspace
Reference. Browser UI must call that Host service rather than connecting to
Orbit's loopback HTTP server directly.

The Orbit CLI takes a non-blocking ownership lock for the runtime database.
Starting a second managed MCP process or a manual `orbit serve` against the
same database fails instead of creating two writers.
