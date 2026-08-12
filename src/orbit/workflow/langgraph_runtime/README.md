# Agent-generated LangGraph workflows

This adapter implements the safe generation boundary:

```text
Agent prompt -> Workflow DSL -> Orbit validation/Canonical IR
             -> exact trusted Handler binding -> LangGraph StateGraph
```

The Agent never supplies executable Python. `compile_generated_workflow`
first runs the existing structural and semantic compiler. Every executable IR
node must then resolve to a `BoundHandler` with the exact name, version, and
manifest fingerprint recorded in the IR.

```python
from orbit.workflow.langgraph_runtime import (
    BoundHandler,
    LangGraphHandlerRegistry,
    compile_generated_workflow,
)

runtime_handlers = LangGraphHandlerRegistry([
    BoundHandler(
        "send_email", "1.0.0", manifest.fingerprint,
        lambda inputs, config, context: {"message_id": send(inputs)},
    ),
])

workflow = compile_generated_workflow(
    agent_json,
    handler_catalog,
    schema_catalog,
    runtime_handlers,
    checkpointer=checkpointer,
)
result = workflow.invoke(
    {"request": request},
    config={"configurable": {"thread_id": run_id}},
)
```

Supported IR behavior includes fixed and conditional routing, exclusive or
parallel fan-out, joins, bounded back-edge loops and rework, input defaults,
compiled condition and mapping ASTs, explicit primary results, and any
LangGraph checkpointer supplied by the application.

## Authoring capability matrix

New LangGraph workflows use a deliberately smaller contract than the legacy
Runtime. The compiler rejects unsupported declarations before a run is created;
it never silently ignores them.

| Contract area | Supported |
| --- | --- |
| Node kinds | `action`, `decision`, `human`, `join`, `terminal` |
| Handler adapters | reviewed Agent, Tool, Transform, or other exact-version `BoundHandler` adapters |
| Routing | success/error/timeout/cancel, conditions, exclusive and parallel fan-out |
| Join policies | `all`, `all_successful`, `any`, `n_of_m`, `deadline` |
| Repetition | bounded `loop` and `rework`, with `fail` or `error_route` exhaustion |
| Completion | positive `required_terminal_count` |
| Durability | SQLite checkpoints, retry timers, join deadlines, recovery, cancellation |
| Data | inline values plus artifact/secret refs when every bound Handler declares the transport |

Agent authoring must not emit `agentic`, `foreach`, `subflow`, or `extension`
node kinds, nor top-level IR extensions. These belong to the legacy Runtime
contract and are intentionally outside the new LangGraph workflow language.

Handlers return a mapping for the normal success path. For explicit failure
families they return `HandlerOutcome(output, route="error" | "timeout" |
"cancel")`; matching edges receive the structured output. Arbitrary Python
exceptions are not converted into business routes.

With a checkpointer, pass a stable `thread_id`. If a Handler calls LangGraph's
`interrupt()`, resume with `workflow.resume(value, config=the_same_config)`.
`workflow.stream(...)` exposes native LangGraph progress chunks.

## Durable application service

`LangGraphWorkflowService` is the isolated application boundary for published
workflows. It loads immutable versions from `SQLiteWorkflowVersionStore`, uses
a separate SQLite database for LangGraph checkpoints, and keeps run metadata
and idempotency receipts outside graph state:

```python
service = LangGraphWorkflowService(
    workflow_versions,
    runtime_handlers,
    run_db_path=project_dir / "langgraph-runs.sqlite3",
    checkpoint_db_path=project_dir / "langgraph-checkpoints.sqlite3",
)

run = service.start(
    "workflow:review",
    {"request": request},
    idempotency_key=request_id,
)
if run.status == "interrupted":
    run = service.resume(
        run.run_id,
        True,
        expected_revision=run.revision,
        idempotency_key=approval_request_id,
    )
```

Run metadata has `running`, `interrupted`, `completed`, and `failed` states.
Interrupt IDs and JSON payloads are projected on `run.interrupts`, so callers
do not need to decode checkpoint internals. When a parallel superstep exposes
multiple interrupts, resume each with its explicit `interrupt_id`. The service
durably accumulates those responses and submits them to LangGraph together once
the superstep is complete; an omitted ID is accepted only when exactly one
interrupt remains.
Starting with the same idempotency key and identical request returns the first
run; reusing the key for different input is rejected. Resume has the same
idempotent receipt behavior, optimistic revision checks, and a conditional
claim so two callers cannot resume one interrupt concurrently. Initial input
is durable as well as checkpoints, so
`recover()` can restart a process that died before writing its first checkpoint.
`recover_running()` performs the same recovery over a bounded startup snapshot.

This service deliberately is not mounted on Orbit's existing `/api/v1/runs`.
ADR 002 keeps LangGraph isolated from the primary event-sourced Runtime; an
HTTP or MCP surface for this adapter should use distinct routes and capabilities.

## Optional HTTP surface

For the local Runtime, enable the adapter explicitly:

```console
orbit serve --enable-langgraph
```

This creates `langgraph-runs.sqlite3` and
`langgraph-checkpoints.sqlite3` beside the Runtime database. Use
`--langgraph-state-dir PATH` to place both elsewhere. Production wiring exposes
only reviewed adapters: the deterministic built-in `transform` Handler and
Agent Handlers produced by the trusted discovery allowlist. Development-tool
Handlers remain on the event-sourced Runtime.

Agent nodes receive a stable run/attempt identity and write an attempt journal
before submitting to the CLI. A completed response is replayable if the graph
checkpoint write is interrupted. A timeout, cancellation, process loss, or an
attempt found `started` during recovery parks the run as `unknown`; it is never
submitted a second time automatically. This preserves Orbit's core
unknown-external-result rule while Agent execution migrates to LangGraph.
Cancellation is persisted before live Handler cancellation hooks run. A late
result therefore cannot overwrite `cancelled`.

Passing `langgraph_service=service` to `create_app()` explicitly mounts a
separate surface. Omitting it leaves every route absent:

| Method | Path | Scope |
| --- | --- | --- |
| `GET` | `/api/v1/langgraph-runs` | `runtime.read` |
| `POST` | `/api/v1/langgraph-runs` | `runtime.write` |
| `GET` | `/api/v1/langgraph-runs/{run_id}` | `runtime.read` |
| `POST` | `/api/v1/langgraph-runs/{run_id}/resume` | `runtime.write` |
| `POST` | `/api/v1/langgraph-runs/{run_id}/cancel` | `runtime.write` |
| `POST` | `/api/v1/langgraph-runs/{run_id}/recover` | `runtime.ops.write` |

Writes require the normal `idempotency-key` header. Read DTOs advertise a
resume command only to actors with write scope and only while interrupted;
clients do not infer commands from status. The existing Orbit `/api/v1/runs`
tools remain unchanged.

The same explicit injection advertises six MCP tools to agents:
`list_langgraph_runs`, `inspect_langgraph_run`, `start_langgraph_run`,
`resume_langgraph_run`, `cancel_langgraph_run`, and `recover_langgraph_run`.
They reuse the same MCP
identity and scopes as HTTP; recovery requires `runtime.ops.write`. When the
service is absent, none of these optional tools appears in `tools/list`.

Handlerless `human` nodes compile to native LangGraph interrupts. Their
checkpoint payload includes the workflow, node, declared input, and config;
resume with an object keyed by the human node's declared output ports (or a
scalar when it declares exactly one output).

## Gradual engine migration

When the adapter is enabled, workflow catalog and detail projections include
`langgraph_compatibility`. The service compiles the immutable published version
against the reviewed registry before reporting it compatible. A writer receives
the server-issued `langgraph_run.start` command only for a compatible version;
unsupported workflows carry a stable reason and detail instead of failing after
the caller has selected an engine. The existing `run.start` command remains
available during migration, so switching is explicit and reversible per run.
