# Open Orbit

1. Resolve the current project workspace to an absolute directory. If the App
   has no project workspace, stop and ask the user to open or select a project
   directory; never use the process working directory as a fallback.
2. Run the plugin's `scripts/start-orbit.sh <absolute-project-path>`. The same
   path may instead be supplied through `ORBIT_AGENT_APP_WORKSPACE`.
3. It checks `/health/ready` and starts a Runtime only when that workspace has
   no ready instance.
4. Open the returned URL in the Codex in-app browser.
5. Immediately call `wait_authoring_request` with `client="chatgpt"` and
   `timeout_seconds=300`. This registers `app:chatgpt`, which Orbit selects by
   default while it is connected.
6. If the wait times out and the current task is still active, call it again.
   Continue until a request arrives or the user asks to stop listening.
7. When a request arrives, follow
   [authoring-with-current-app.md](authoring-with-current-app.md) to generate and
   submit the Workflow DSL, then resume listening unless the user stops it.

Tell the user after the first wait starts that Orbit is open and this App is
listening. Do not claim that listening survives the current task: ending the
task ends `app:chatgpt` presence, while the Runtime may continue running.

Do not run `orbit mcp` beside `orbit serve`: it creates a second Runtime. The
registered MCP server is a stdio proxy to the same HTTP Runtime.

The bundled MCP process uses `ORBIT_AGENT_APP_WORKSPACE` when provided. Without
it, the proxy attaches only when exactly one live managed Orbit workspace can
be recovered from Host state; ambiguity fails closed instead of selecting a
project silently.
