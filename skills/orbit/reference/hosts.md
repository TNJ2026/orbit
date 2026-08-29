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
`ORBIT_AGENT_APP_WORKSPACE` to the open project. `open_orbit_dashboard` starts
or discovers the Runtime for that workspace.

**Claude Code.** There is no plugin. The skill is reached through the
`.claude/skills/orbit` symlink in the checkout, and the MCP server comes from
the repo-root `.mcp.json`, which runs `scripts/start-mcp-proxy.sh`. That file
only *declares* `ORBIT_AGENT_APP_WORKSPACE`; nothing sets it. So the proxy
takes the other path every time: it attaches when exactly one live managed
Orbit workspace can be recovered from Host state, and fails closed when more
than one could match. This is the normal path under Claude Code, not a
fallback — if it fails, the answer is to say which project, not to retry.

Because this depends on the checkout, the skill is active only for someone
working inside it. It is not installable as a Claude plugin: there is no
`.claude-plugin/plugin.json`, and `scripts/build-marketplace-release.py`
packages `.codex-plugin` alone.

## Showing the UI beside the conversation

`open_orbit_dashboard` always starts or discovers the Runtime and returns the
workflow list. Whether it also *draws* anything is the host's decision, not
Orbit's, so treat the visible surface as a separate question from the call.

The card it offers is an MCP App (the MCP Apps extension, SEP-1865). Orbit
serves it as the resource `ui://orbit/workflows.html` with mime type
`text/html;profile=mcp-app`, and binds it to the tool through
`_meta.ui.resourceUri` — see `src/orbit/web/mcp_app.py`. There is nothing
host-specific in any of that: a host that implements MCP Apps mounts the card
in a sandboxed iframe and the user sees the dashboard; a host that does not
shows only the JSON the tool returned.

The card is read-only by design. It lists published workflows and refreshes,
and offers no way to start a goal or write a workflow — those are asked of the
Agent in the conversation, which holds this skill. Keep it that way: a control
added there is a path around the Agent.

**Codex.** Mounts it. Nothing further is needed.

**Claude Code.** Observed not to mount it — the call returns data and the
user sees nothing — so a task that stops there has not opened Orbit from where
the user sits. Open the panel in the Browser pane instead, with
`preview_start` on `http://127.0.0.1:8848/panel`. That route serves the same
page as the resource above, so what the user gets beside the conversation is
what the card would have shown.

Open `/panel`, not `/ui`. They are different surfaces on purpose: the panel
lists workflows and refreshes, while the full UI at `/ui` also starts goals,
generates workflows and deletes them. Putting `/ui` beside the conversation
hands the user every mutating control the panel was built to withhold, and
routes work around the Agent that holds this skill rather than through it.
Send them to `/ui` only when they ask to operate Orbit directly.

**Another App.** The card performs the MCP Apps handshake itself, so a host
that mounts it gets a working panel rather than one stuck connecting. Hosts
have been reported to fetch the resource without mounting it, though, so look
at what actually happened before adding a second surface; if nothing was
drawn, open `/panel` in whatever browser the App has, or the user's own.

The rule that outlives any of these: if the card did not appear, show the UI
another way. If a host starts mounting it, drop that host's workaround rather
than leaving the user with two dashboards.

When the Runtime is not running and no MCP tool can reach it, start it with
`scripts/start-orbit.sh <absolute-project-path>` and open the URL it prints.

## When to hold a listening call

`wait_authoring_request` parks the task until someone clicks Generate in the
Orbit UI. Under Codex a pending call sits beside a person who can keep
working, so holding one and renewing it on timeout costs nothing.

Under Claude Code a pending tool call blocks the conversation for its whole
timeout. Do not open one speculatively. Instead:

- **On opening Orbit**, register presence with `register_authoring_client`.
  It marks the same address present without claiming work, for ten minutes,
  and is renewed by calling it again — the App appears in Orbit's writer menu
  exactly as a waiting one does.
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
