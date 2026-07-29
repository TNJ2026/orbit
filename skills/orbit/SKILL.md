---
name: orbit
description: Open and use the local Orbit workflow Runtime, automatically listen for workflow authoring requests in the current App, and connect other MCP-capable Agent Apps.
---

# Orbit

Use this skill for requests to open Orbit, inspect local workflow runs, or work
with Orbit's workflow UI.

For opening the Runtime, follow [open-orbit.md](reference/open-orbit.md).

For generating a workflow in the current App after the user clicks Generate in
Orbit, follow [authoring-with-current-app.md](reference/authoring-with-current-app.md).

For configuring or operating Orbit from another Agent App, follow
[using-from-other-agent-apps.md](reference/using-from-other-agent-apps.md).

For waiting on or processing Runtime events captured by the App, follow
[runtime-events.md](reference/runtime-events.md).

Use the registered Orbit MCP tools for workflow operations. Do not construct
mutation URLs: follow the Runtime's `allowed_commands[]` responses.
