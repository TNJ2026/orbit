# Open Orbit

Read [hosts.md](hosts.md) first for this host's client name, how its MCP proxy
finds the workspace, where the UI has to be opened, and whether a listening
call may be held open.

1. Resolve the current project workspace to an absolute directory. If the App
   has no project workspace, use `ORBIT_DEFAULT_WORKSPACE` when configured or
   `~/.orbit/workspaces/default`; never use the process working directory as a
   fallback.
2. Call `open_orbit_dashboard`. The MCP proxy selects the resolved workspace
   through the Hub, which starts or discovers its Runtime, and opens the
   default current-task dashboard.
   Keep this default for requests to open Orbit itself. Workflow-list requests
   use [view-workflows.md](view-workflows.md) and call `list_workflows` instead.
3. Put the UI where the user can see it, the way [hosts.md](hosts.md)
   describes for this host. On some hosts step 2 has already drawn it; on
   others that call renders nothing and this step is what the user sees.
4. Tell the user where Orbit opened and summarize the current or most recent
   run from the step 2 response. The full workflow catalog belongs in `/ui`.
Opening Orbit is display-only. Do not call `register_authoring_client`,
`wait_authoring_request`, or otherwise announce this App as an author merely
because the user opened Orbit. Register or listen only when the user explicitly
asks this App to receive workflow-authoring requests from the Orbit UI; then
follow [authoring-with-current-app.md](authoring-with-current-app.md).

Tell the user where Orbit opened and what the dashboard shows. Mention an
authoring client name only if registration was separately requested and
actually completed.

Do not run `orbit mcp` beside `orbit serve`: it creates a second Runtime. The
registered MCP server is a stdio proxy to the same HTTP Runtime.
