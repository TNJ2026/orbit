# Run a published workflow in the current App

`start_run(execution_mode="current_app")` delegates every Agent action in a
published workflow to the conversation that starts the Run. It does not
require an installed Agent CLI, edit the published version, or depend on the
default missing-CLI fallback. An installed CLI is also overridden.

## Entry points

Ask the Agent: “运行这个工作流，所有 Agent 步骤都交给当前 App 执行，包括并行分支。”
The Orbit skill maps that request to the explicit MCP argument:

```json
{
  "workflow_id": "workflow:example",
  "goal": "The user's goal",
  "input": {},
  "execution_mode": "current_app",
  "wait": false,
  "idempotency_key": "a-unique-request-key"
}
```

The HTTP start command accepts the same field. Discover its URL from
`allowed_commands[]`. `default` (or omitting the field) retains the existing
Handler selection. Current-App starts, human resumes and ordinary recovery
return asynchronously so the conversation can claim the resulting work.

For a read-only preview, call `inspect_workflow_definition` with
`execution_mode="current_app"`. Its `execution_compatibility` reports whether
the adapted graph can compile. A default CLI incompatibility does not make
this preview fail. Input validation, archival state and project access still
apply. Workflow discovery for this mode must not filter by default CLI
readiness.

## Runtime contract

Before creating a Run, Runtime identifies Agent nodes by a published
`agent.*` name, an App/Harness delegation name, or a registered Handler's
`agent.invoke` capability. It binds these actions to `app.delegate` and stores
the adapted graph in the Run snapshot. `execution_mode` is persisted, returned
in run projections, and included in start idempotency. Resume uses that snapshot.

The snapshot retains original ports, edges, conditions, policies and input
defaults. Internal `_agent_step` metadata records the original Handler and
configuration; it is not a public DSL option. At invocation, the trusted App
adapter wraps the assembled inputs and authored instructions in
`request.input.task`. This avoids changing entry input names or back-edge
mappings simply to turn a `prompt` port into the App's task message.

Each ready node creates its own actor-scoped delegation. The App claims it,
performs the instructions with its normal tools, renews/checkpoints its lease,
and submits an object result. Prose uses `{"text": "..."}`. Text and JSON
artifact outputs are stored by Runtime according to the original output
policy; downstream App actions receive the authorized artifact contents.
Credential references remain references, without resolving Runtime secrets.
Project workspace grants and acceptance checks still apply.

Parallel graph branches can be claimed together and completed independently,
including through independent Execution Worker processes. The App decides
how many it can actually execute concurrently. Conflicting writes to a shared
workspace must be serialized by the App. Each lease belongs to a single
delegation; loss of the conversation does not authorize another actor to
claim it. Expired leases require reconciliation, and confirmed results are
normalized without executing the Agent again.

The adapter supports the Runtime's Agent action contract with a single
`result` output. Unsupported node kinds and binary artifact outputs are
rejected before creating a Run. Human nodes, joins, decisions and terminal
nodes retain their existing semantics.

This change adds MCP/HTTP and skill entry points. It does not add a UI selector
or an unattended App worker. Running installations need the updated Runtime
and Orbit skill before the new parameter and conversation loop are available.

## Verification

`tests/test_current_app_execution.py` covers absent and installed CLI bindings,
MCP/HTTP entry points, goal inputs, actor isolation, idempotency, persisted
snapshots, parallel execution through a real Worker, artifact transfer, opaque
credential references, human resume and reconciliation.
