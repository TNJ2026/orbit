# View Workflows

Use this procedure for requests such as `open workflows`, `view workflow
list`, `show workflows`, `打开工作流`, `查看工作流`, `工作流列表`, and close
equivalents.

1. Resolve the current project workspace exactly as in [open-orbit.md](open-orbit.md).
2. Call `list_workflows`. Pass `ready_only=true` only when the user asks for
   runnable workflows; otherwise show the complete published list.
3. The tool's workflow-list MCP App is the visible result. It contains only
   workflow items, not current runs or authoring jobs. Every item exposes a
   **New goal** action directly. When the host mounts this card, treat it as
   the complete presentation of the workflow list: do not restate, enumerate,
   summarize, or render the same workflows as a Markdown table or a second
   list in the Agent response. A short sentence pointing to the displayed card
   is enough.
4. `list_workflows` answers the Agent with a count and a pointer, not with the
   catalogue: the card holds the rows, and repeating them in the text block is
   how one answer arrives twice. When the Agent needs the values — the host
   drew no card, or a question has to be answered about what is in the list —
   call `inspect_workflows`, which reads the same catalogue and draws nothing.
   Only then provide a concise textual fallback.
5. When the user clicks **New goal** on a list item, follow
   [execute-goal.md](execute-goal.md) using that exact workflow id. Do not make
   the user open the detail card first.
6. When the user clicks the item itself, the workflow-list card calls
   `get_workflow_definition` internally and switches to its built-in detail
   view. It must not open a separate workflow-detail MCP App card. The back
   action returns to the list inside the same card.
7. A detail-view **New goal** request follows [execute-goal.md](execute-goal.md)
   using that exact workflow. A **Modify** request follows
   [authoring-with-current-app.md](authoring-with-current-app.md) for revision.
8. A **Delete** request is destructive. Re-read the exact workflow, show its
   name and id, request explicit confirmation, then execute only a current
   Runtime command/tool that authorizes deletion. After confirmation, call
   `delete_workflow` with the observed `latest_version` as `expected_version`
   and a fresh idempotency key. If that tool is unavailable, say so; do not
   construct a mutation URL.

Do not call `open_orbit_dashboard` for a workflow-list request: the dashboard
is the default for opening Orbit, while `list_workflows` is the dedicated list
card.
