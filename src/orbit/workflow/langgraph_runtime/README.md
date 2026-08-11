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
parallel fan-out, joins, back-edge loops, input defaults, compiled condition
ASTs, compiled mapping ASTs, explicit primary results, and any LangGraph
checkpointer supplied by the application.

Only `success` edges are accepted in this first adapter. Error, timeout, and
cancel routing require an explicit failure-result contract; they are rejected
at compile time instead of being silently treated as success paths.
