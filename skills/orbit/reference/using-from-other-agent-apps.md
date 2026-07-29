# Using Orbit From Another Agent App

Use this procedure for an MCP-capable App such as another desktop Agent client.

## Connect the MCP proxy

Keep the Orbit Runtime running, then add an MCP stdio server to the App. Adapt
the field names to the App's MCP configuration format:

```json
{
  "mcpServers": {
    "orbit": {
      "command": "bash",
      "args": ["/absolute/path/to/orbit/scripts/start-mcp-proxy.sh"],
      "env": {
        "ORBIT_AGENT_APP_WORKSPACE": "/absolute/path/to/the/project"
      }
    }
  }
}
```

Use the Orbit source or installed plugin directory that actually contains the
script. The workspace selects the project-specific Runtime and data directory.
Restart or reconnect the App's MCP session after changing its configuration.

## Act as a workflow-writing App

1. Repeatedly call `wait_authoring_request` with a stable client name and a
   bounded timeout, for example `client="claude-desktop"` and
   `timeout_seconds=300`.
2. Keep the call pending while the user selects `app:claude-desktop` in Orbit
   and clicks Generate or requests a revision. The pending wait is the presence
   registration; connecting MCP alone does not add an `app:*` option.
3. Follow the returned prompt exactly and produce one Workflow DSL JSON object,
   without a Markdown fence or surrounding prose.
4. For cooperative cancellation, keep a WebSocket connected to
   `ws://127.0.0.1:8848/authoring/events?client=<client-name>`. When it emits
   `request_cancelled` for the active `request_id`, abort the App's model call
   when supported, discard partial output, and do not submit a response.
5. Call `submit_authoring_response` with the returned `request_id` and DSL.
6. Call `get_authoring_job` with the returned `job_id`. If compilation requests
   another attempt, wait again with the same client name and submit the revised
   document. Stop when the job is done, failed, cancelled, or the user stops it.

A wait timeout only removes the temporary online presence; it is not a workflow
generation failure. Start another wait to appear online again. If several Apps
are connected, give each one a distinct stable client name.
Orbit refuses late submissions after cancellation, so correctness does not
depend on whether the App can actually abort its model request.

## Operate workflow runs

Use the registered MCP tools rather than constructing mutation URLs:

- `list_runs` to discover runs.
- `inspect_run` to read current state and `allowed_commands[]`.
- `start_run` or `cancel_run` only when the corresponding action is allowed.

Treat the Runtime as the authority for identity and authorization. Re-read state
after a notification or conflict, and execute only commands returned by
`allowed_commands[]`.

## Receive Runtime events

Use `wait_app_event` for an active wait, `list_app_events` for retained Inbox
items, and `ack_app_event` only after successful processing or deliberate
dismissal. An event is a hint: inspect the referenced run before acting.
