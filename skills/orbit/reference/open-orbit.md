# Open Orbit

Read [hosts.md](hosts.md) first for this host's client name, how its MCP proxy
finds the workspace, where the UI has to be opened, and whether a listening
call may be held open.

1. Resolve the current project workspace to an absolute directory. If the App
   has no project workspace, stop and ask the user to open or select a project
   directory; never use the process working directory as a fallback.
2. Call `open_orbit_dashboard`. The MCP proxy starts or discovers the Runtime
   for the resolved workspace and returns its published workflows.
3. Put the UI where the user can see it, the way [hosts.md](hosts.md)
   describes for this host. On some hosts step 2 has already drawn it; on
   others that call renders nothing and this step is what the user sees.
4. Tell the user where Orbit opened, and what the Runtime holds — the
   published workflows are in the step 2 response.
5. Register this App under its client name, so an author can address it from
   the Orbit UI. Whether to do that by holding `wait_authoring_request` open or
   by calling `register_authoring_client` and renewing it depends on the host:
   [hosts.md](hosts.md) says which, and why.
6. When a request arrives, follow
   [authoring-with-current-app.md](authoring-with-current-app.md) to generate and
   submit the Workflow DSL, then keep listening unless the user stops it.

Tell the user that Orbit is open and this App is registered under its client
name, and that they select that name in the **Written by** field to have this
App write a workflow — registering does not preselect it. Do not claim that
the registration survives the current task: ending the task ends this App's
presence, while the Runtime may continue running.

Do not run `orbit mcp` beside `orbit serve`: it creates a second Runtime. The
registered MCP server is a stdio proxy to the same HTTP Runtime.
