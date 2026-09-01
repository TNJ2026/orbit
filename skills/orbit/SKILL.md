---
name: orbit
description: Open Orbit or its workflow cards, generate or modify Workflow DSL, and execute user goals with the local workflow Runtime; also inspect workflows and runs, process Runtime events, connect other MCP-capable Agent Apps, and rebuild, refresh, or reinstall the local Orbit plugin in Codex during development.
---

# Orbit

Use this skill for requests to open Orbit, create or modify a workflow, execute
a goal through a published workflow, inspect local runs, or work with Orbit's
workflow UI.

## MCP App cards

Choose the card from the user's intent. Do not open the dashboard first unless
the user asked to open Orbit itself.

- **Orbit dashboard** — for `open Orbit`, `show Orbit`, `打开 Orbit`, and close
  equivalents. Call `open_orbit_dashboard`. This remains the default Orbit
  surface and keeps its current-task dashboard behavior. Opening Orbit is
  display-only: do not register an authoring client or start listening for
  authoring requests unless the user explicitly asks for that connection.
- **Workflow list** — for `open workflows`, `view workflows`, `list workflows`,
  `打开工作流`, `查看工作流列表`, `工作流列表`, and equivalent discovery
  requests. Call `list_workflows`; its MCP App shows only published workflows.
  Each list item offers **New goal** directly; use its exact workflow id and
  follow the goal-execution procedure without opening the detail card first.
  Clicking the item itself switches the same card to its built-in workflow
  detail view; it does not open another MCP App card. Do not substitute the dashboard.
- **Workflow detail** — the workflow-list card calls `get_workflow_definition`
  internally for the exact selected workflow. Its built-in detail view shows
  the definition and offers **New goal**, **Modify**, and **Delete** actions.
  These actions return to the conversation: resolve the
  exact workflow again, require explicit confirmation before deletion, and
  call `delete_workflow` with the observed latest version and a fresh
  idempotency key. Perform mutations only through an authorized Runtime tool. Never
  make the card itself bypass Agent authorization.
- **Workflow generation** — when a chat request generates a workflow, call
  `generate_workflow` directly after registration. Its MCP App shows only that
  authoring job's generation/validation/publication progress and result. Follow
  [authoring-with-current-app.md](reference/authoring-with-current-app.md); do
  not open the dashboard as a progress surrogate.
- **Goal execution** — call `start_run` only after workflow resolution and
  validation. Resolve an explicit workflow ID directly without calling
  `list_workflows`, so execution does not open the workflow-list card. Its MCP
  App shows only that run's steps, attention state, and result. Follow
  [execute-goal.md](reference/execute-goal.md); do not open the dashboard before
  the run unless the user separately asked for Orbit itself.

Cards are views, not authority. Suggested actions send a prompt back to the
Agent, and the Agent must re-read current state and obey `allowed_commands[]`.

The procedures below are host-neutral. For what differs between the Agent Apps
that read this skill — the client name to register under, how the MCP proxy
finds the workspace, where the UI has to be opened, and when a listening call
may be held open — read [hosts.md](reference/hosts.md).

For opening the Runtime, follow [open-orbit.md](reference/open-orbit.md).

For opening or viewing workflows and workflow details, follow
[view-workflows.md](reference/view-workflows.md).

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

For rebuilding or reinstalling the local Orbit plugin in Codex and preparing a
clean application restart, follow
[refresh-codex-plugin.md](reference/refresh-codex-plugin.md). This procedure
deliberately stops before fully quitting or reopening Codex.

Use the registered Orbit MCP tools for workflow operations. Do not construct
mutation URLs: follow the Runtime's `allowed_commands[]` responses.
