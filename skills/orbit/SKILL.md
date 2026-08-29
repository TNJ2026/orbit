---
name: orbit
description: Open Orbit, generate or modify Workflow DSL, and execute user goals with the local workflow Runtime; also inspect runs, process Runtime events, and connect other MCP-capable Agent Apps.
---

# Orbit

Use this skill for requests to open Orbit, create or modify a workflow, execute
a goal through a published workflow, inspect local runs, or work with Orbit's
workflow UI.

The procedures below are host-neutral. For what differs between the Agent Apps
that read this skill — the client name to register under, how the MCP proxy
finds the workspace, and when a listening call may be held open — read
[hosts.md](reference/hosts.md).

For opening the Runtime, follow [open-orbit.md](reference/open-orbit.md).

For generating or modifying a workflow with the current task, whether
the request starts in chat or from the Orbit UI, follow
[authoring-with-current-app.md](reference/authoring-with-current-app.md).

For selecting a published workflow, starting a run from the user's goal, and
following it through completion or an interrupt, follow
[execute-goal.md](reference/execute-goal.md).

For configuring or operating Orbit from another Agent App, follow
[using-from-other-agent-apps.md](reference/using-from-other-agent-apps.md).

For waiting on or processing Runtime events captured by the App, follow
[runtime-events.md](reference/runtime-events.md).

Use the registered Orbit MCP tools for workflow operations. Do not construct
mutation URLs: follow the Runtime's `allowed_commands[]` responses.
