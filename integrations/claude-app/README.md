# claude-orbit

Orbit as a stdio MCP server, for a host that speaks MCP for itself.

Claude connects to MCP servers. Orbit *is* one — over HTTP, once a Runtime is
running for the project. This is the part in between: which Runtime serves this
directory, starting one when none does, refusing one whose protocol it does not
know, and saying something useful when any of that fails.

It is deliberately thin. All of the above is `integration-core`'s, already
written for the DeepSeek-Harness; what is here is a stdio transport, this
host's own wording for Orbit's failures, and the forwarding between them.

## Use

    npm install && npm run build

Then point a client at it:

```json
{
  "mcpServers": {
    "orbit": {
      "command": "node",
      "args": ["/path/to/orbit/integrations/claude-app/lib/main.js",
               "--project-root", "/path/to/your/project"]
    }
  }
}
```

### Flags

| flag | environment | default | what it decides |
| --- | --- | --- | --- |
| `--project-root` | — | the launch directory | which project this server serves. One Runtime serves one Workspace, so one of these serves one project — named at launch, never per request. |
| `--orbit-command` | `ORBIT_COMMAND` | `orbit` | which executable to discover and start Runtimes with, so a checkout can point at its own. |
| `--actor` | `ORBIT_ACTOR` | `claude` | who Orbit records as having done this. |

### About the actor

Orbit scopes every write by actor: who cancelled a Run is the point of
recording who cancelled it, and the one-goal-at-a-time slot is per-actor so one
caller cannot block another. Without a name every loopback caller is `local`,
which makes this server indistinguishable from a person at a terminal in the
same project. It is stable across restarts on purpose — a name with a pid in it
would leave a goal slot held by an actor that no longer exists, and make it
impossible to cancel a Run this server started a minute earlier.

### About the tool profile

A Runtime this server *starts* is asked for the `full` tool set, not the
Harness's subset: a general-purpose client has no use for the authoring claim
loop and every use for the tools that subset leaves out. A Runtime somebody
else already started keeps whatever it was started with — the profile belongs
to the Runtime, not to whoever connects.

## What it is not

Not the DeepSeek-Harness panel. That panel is injected into the host's own UI
through APIs Claude does not have — a resident React surface, composer input
triggers, a host-level popup. Orbit's own web UI covers the same ground and is
a link away; `get_capabilities` reports where it is.

## Notes

- stdout carries JSON-RPC and nothing else. Diagnostics go to stderr; a stray
  `console.log` is a protocol violation.
- Messages are answered in arrival order. MCP allows concurrency, but a
  Runtime that is still starting is the common case here, and a second request
  racing ahead would start a second Runtime.
- No failure is thrown at the caller. One pipe, one peer: a throw ends the
  session, while a failure the peer is told about is one it can act on. Every
  error reply carries the sentence in `error.message` and the raw text in
  `error.data.detail`, because a classification is a guess that can be wrong.
- Every forwarded call has a deadline. Without one a Runtime that stops
  answering leaves the client waiting on a promise nothing will settle, and a
  stdio peer has no other way to notice. A deadline reads as a timeout, not as
  a cancellation: one means Orbit may still be working, the other that nobody
  is waiting any more.
- It never stops the Runtime. That Runtime is independent and may be serving a
  terminal or another client; this server letting go of it is not a decision
  that nobody should have it.
