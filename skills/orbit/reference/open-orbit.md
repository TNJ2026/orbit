# Open Orbit

Read [hosts.md](hosts.md) first for this host's client name, how its MCP proxy
finds the workspace, and whether a listening call may be held open.

1. Resolve the current project workspace to an absolute directory. If the App
   has no project workspace, stop and ask the user to open or select a project
   directory; never use the process working directory as a fallback.
2. Call `open_orbit_dashboard`. The MCP proxy starts or discovers the Runtime
   for the resolved workspace, and the tool opens the native Orbit workflow
   card beside the conversation.
3. If the native tool is unavailable, open the UI the way
   [hosts.md](hosts.md) describes for this host. Do not use that fallback when
   the native tool is available.
4. Tell the user whether the native dashboard or the full browser UI was
   opened.
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
