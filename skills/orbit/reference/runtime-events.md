# Runtime Events

Orbit's MCP proxy listens to the Runtime event WebSocket in the background and
stores notifications in a durable local Inbox.

1. Use `wait_app_event` when the user wants this task to wait for Runtime work.
2. Use `list_app_events` to inspect notifications captured while no task was active.
3. Treat each event as a hint. Re-read the run or inbox through Orbit MCP tools.
   For `request_cancelled`, match `request_id`, abort cooperative App work,
   discard partial output, and never call `submit_authoring_response`.
4. Execute only commands returned in `allowed_commands[]`; never construct a mutation URL.
5. Call `ack_app_event` only after processing succeeds or the user deliberately dismisses it.

An event does not autonomously wake a finished task. The background proxy
retains it until this task, another task, or a separate Agent worker consumes it.
