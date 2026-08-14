"""The Agentic Workflow runtime.

`domain` holds the pure contracts, `dsl` compiles a document into a
`WorkflowIR`, and `langgraph_runtime` executes one. Import from the module
that owns a name rather than from here: a package-level re-export hub makes
every consumer look like it depends on everything.
"""
