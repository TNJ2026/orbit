# Hosts

This skill is one file, read by more than one Agent App. The procedures are
written host-neutral; everything that actually differs between hosts is here.
Work out which host is running this task, then read its section.

## The client name

Orbit lists a connected App under the name it registers. Use:

| Host | Client name |
| --- | --- |
| Codex | `codex-app` |
| Claude Code | `claude-app` |
| Another MCP-capable App | its own stable name — see [using-from-other-agent-apps.md](using-from-other-agent-apps.md) |

The `-app` suffix is not decoration. A Runtime discovers installed CLIs as the
authoring Agents `codex`, `claude` and others, and a client name may not shadow
an installed Agent: registering as one is refused outright rather than
renamed. Two Apps connected at once need two distinct names.

## How the Runtime and its MCP proxy are reached

**Codex.** The bundled plugin ships the proxy, and the plugin host sets
`ORBIT_AGENT_APP_WORKSPACE` to the open project. The proxy registers that
workspace with the fixed loopback Hub and uses its workspace-scoped MCP URL.
The Hub starts or discovers a dynamic-port Runtime for that workspace. In a
projectless chat, the Host uses `ORBIT_DEFAULT_WORKSPACE` when configured,
otherwise `~/.orbit/workspaces/default`.

The Hub is the public MCP Gateway, not a transparent MCP proxy. It owns MCP
protocol lifecycle and App resources; workspace Runtimes expose a private
Agent-tool backend and remain authoritative for tool permissions, workflow
state, and `allowed_commands[]`. Each production workspace Runtime starts an
authenticated local Execution Worker process that holds the real Handler
adapters. The Control Runtime compiles and advances LangGraph, while Handler
invocation and cancellation cross that private worker boundary.

**Claude Code.** There is no plugin. The skill is reached through the
`.claude/skills/orbit` symlink in the checkout, and the MCP server comes from
the repo-root `.mcp.json`, which runs `scripts/start-mcp-proxy.sh`. When no
workspace is supplied, the proxy uses `ORBIT_DEFAULT_WORKSPACE` or
`~/.orbit/workspaces/default`; it does not guess from the process cwd.

Because this depends on the checkout, the skill is active only for someone
working inside it. It is not installable as a Claude plugin: there is no
`.claude-plugin/plugin.json`, and `scripts/build-marketplace-release.py`
packages `.codex-plugin` alone.

## Showing the UI beside the conversation

`open_orbit_dashboard` always starts or discovers the Runtime and returns the
current run list. Whether it also *draws* anything is the host's decision, not
Orbit's, so treat the visible surface as a separate question from the call.

The card it offers is an MCP App (the MCP Apps extension, SEP-1865). Orbit
serves it as the resource `ui://orbit/current-task-v14.html` with mime type
`text/html;profile=mcp-app`, and binds it to the tool through
`_meta.ui.resourceUri` — see `src/orbit/web/mcp_app.py`. There is nothing
host-specific in any of that: a host that implements MCP Apps mounts the card
in a sandboxed iframe and the user sees the current-task card; a host that does not
shows only the JSON the tool returned.

The card is a current-task projection by design. It shows the active or most
recent run, step progress, authoring state, and whether a person is needed.
Its embedded read-only views let a person choose a Workflow or inspect the
registered Agents without replacing the card. Its other actions send intent
back to the conversation; it does not mutate the Runtime directly. History,
graphs, logs, and workflow management remain in the full UI.

**Codex.** Mounts it. Nothing further is needed.

**Claude Code.** Observed not to mount it — the call returns data and the user
sees nothing. Say that this host does not currently render the MCP App. Send
the user to the full `/ui` only when they explicitly ask to operate Orbit
directly.

**Another App.** The card performs the MCP Apps handshake itself, so a host
that mounts it gets a working card rather than one stuck connecting. Hosts
have been reported to fetch the resource without mounting it, though, so look
at what actually happened and report when the host did not render the card.

The rule that outlives any of these: if the card did not appear, say so and
offer the full UI rather than silently opening a second surface.

When the Hub is not running and no MCP tool can reach it, start it with
`scripts/start-orbit.sh <absolute-project-path>` and open the workspace URL it
prints. Port 8848 belongs to the Hub; workspace Runtimes use discovered dynamic
ports and remain isolated from one another.

## When to hold a listening call

Opening Orbit never registers this App and never starts a listening call.
`wait_authoring_request` parks the task until someone clicks Generate in the
Orbit UI, so use it only for an explicit request to receive authoring work.
Under Codex a pending call sits beside a person who can keep working.

Under Claude Code a pending tool call blocks the conversation for its whole
timeout. Do not open one speculatively. Instead:

- **When explicitly asked to make this App available as a writer**, register
  presence with `register_authoring_client`. It marks the same address present
  without claiming work, for ten minutes, and is renewed by calling it again.
- **Hold `wait_authoring_request`** only when the user has said they are going
  to the Orbit UI to click Generate. Then the block is the point: the task has
  nothing else to do until the request arrives.

## Being listed is not being selected

Registering does not make Orbit write with this App by default. Connected App
names are layered underneath the discovered CLIs on purpose: a forked CLI
runs, while a parked prompt only waits and may never be answered. So the
Runtime names no App as its default writer, and the UI's writer menu opens on
whichever Agent sorts first, not on the App that just connected.

Tell the user to pick the client name in the **Written by** field. Never tell
them it is already selected.
