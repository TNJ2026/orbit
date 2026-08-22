# Changelog

## 0.1.0

- Added the Workspace-scoped Orbit stdio Gateway and Harness MCP profile.
- Added Session Run cards, the Run Detail Drawer, cursor output, Artifacts and Human Resume.
- Added durable `harness.subagent` delegation with Execution and Job Leases.
- Added Effect Manifest observation, runtime DTO codecs and manual reconciliation.
- Added subprocess, Provider lifecycle and policy fault-injection tests.

Known limitations: Harness does not currently expose a per-subagent cwd/worktree
or internal model-call budget, so this release validates existing isolation and
wall time but does not claim to create worktrees or meter Provider-internal calls.
