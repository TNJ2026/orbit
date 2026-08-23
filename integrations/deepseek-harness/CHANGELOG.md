# Changelog

## Unreleased

- Changed the Host Gateway to discover an independently started `orbit serve`
  Runtime and communicate over HTTP MCP; it no longer owns a Runtime process.
- Removed Harness Subagent execution from Session Bridge startup. CLI Agent
  execution and credentials now stay entirely inside Orbit Runtime.
- Automatically attach and recover Session Bridges for live root Sessions,
  deriving the Runtime cursor from durable Session events.
- Added loopback-only, prefix-restricted HTTP MCP Session actors so independent
  Runtime event streams remain isolated between Harness Sessions.
- Added six Workspace-aware native Harness tools backed by Orbit MCP. Their
  execution derives Session routing in Host code and owns idempotency and
  advertised-command revision checks.
- Added a real-process independent Runtime E2E gate and endpoint rediscovery
  after transport loss or Runtime restart.
- Added cross-platform `ORBIT_RUNTIME_ROOT` discovery for custom Runtime database locations.
- Expanded General Settings diagnostics with Runtime/protocol versions, tool
  profile, reconnect probing and a copyable independent-Runtime start command.
- Added the native Orbit product workspace with Session Run history, full Run
  Drawer, Workflow and Artifact catalogs, Agent-backed workflow generation and
  modification, explicit image Attachment import, and downloadable diagnostics.
- Removed the cancelled Harness Subagent executor, provider policy, Git effect
  observer, related tests/codecs, and direct Agent/Subagent peer dependencies.

## 0.1.0

- Added the Workspace-scoped Orbit stdio Gateway and Harness MCP profile.
- Added Session Run cards, the Run Detail Drawer, cursor output, Artifacts and Human Resume.
- Added durable `harness.subagent` delegation with Execution and Job Leases.
- Added Effect Manifest observation, runtime DTO codecs and manual reconciliation.
- Added subprocess, Provider lifecycle and policy fault-injection tests.

Known limitations: Harness does not currently expose a per-subagent cwd/worktree
or internal model-call budget, so this release validates existing isolation and
wall time but does not claim to create worktrees or meter Provider-internal calls.
