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

## Opening the UI without the native tool

When `open_orbit_dashboard` is unavailable, run
`scripts/start-orbit.sh <absolute-project-path>` and open the URL it returns —
in Codex's in-app browser under Codex, and in the user's own browser
elsewhere. Claude Code has no in-app browser of its own to fall back to.

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
