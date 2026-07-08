# orbit

[简体中文](./README.md) | **English**

> A local MCP server that lets multiple LLM CLIs / agents (Claude Code, Codex CLI, Gemini CLI, your own agent) exchange prompts through a shared mailbox and run multi-role task workflows.

```
Claude Code ──┐
Codex CLI  ───┼── HTTP (Streamable) ──▶ orbit daemon :8848/mcp ──▶ SQLite ~/.orbit/projects/<project>/messages.db
Gemini CLI ───┘
```

- A single long-running HTTP daemon; all clients connect to the same port, so state is shared for free.
- Per-project storage keyed by launch directory: agents / messages from different projects never mix in one database.
- Messages persist to SQLite: no message is lost across daemon restarts, and a recipient can pick them up after coming online later.
- Async mailbox model: `send_message` delivers, `check_inbox` claims under a lease, `ack_message` confirms completion (long-poll up to 60s).

## Table of Contents

- [Install](#install)
- [Start](#start)
- [Workflow Engine](#workflow-engine)
- [Connecting Clients](#connecting-clients)
- [MCP Tools](#mcp-tools)
- [Local Web UI](#local-web-ui)
- [Task Collaboration Model](#task-collaboration-model)
- [Orchestrator Mode](#orchestrator-mode)
- [Role Files](#role-files)
- [Appendix: Tool Return Formats](#appendix-tool-return-formats)

## Install

Requires Python ≥ 3.10 and [uv](https://docs.astral.sh/uv/):

```bash
git clone <repo-url> orbit
cd orbit
uv sync        # creates .venv and installs the sole dependency, mcp
```

Without uv: `pip install -e .`

## Start

```bash
uv run orbit serve                 # default 127.0.0.1:8848; db split per current project directory
uv run orbit serve --port 9000 --db /tmp/test.db
```

Three ways to bring it up — pick by need:

| Command | Writes files into the repo? | Use when |
|---|---|---|
| `orbit up` | Only appends to `.gitignore` (ignores `.orbit/`) | Zero-config use from another repo; ships with packaged role/workflow defaults |
| `orbit serve` | No | Already `init`-ed, or happy with the defaults |
| `orbit init` + `orbit serve` | Writes role / workflow / config files | Customize role prompts & workflow and commit them for the team |

### Zero-config start in another repo

When you don't want to copy role/config files into another repo, use `orbit up`: it first appends the state dir (`.orbit/`) to that repo's `.gitignore`, then serves using the **packaged role and workflow defaults** — nothing that needs to be committed lands in the repo. It accepts every `serve` flag (`--host` / `--port` / `--db` / `--no-runner` / `--runner-concurrency`).

```bash
orbit up                                   # orbit already installed
uvx --from git+<repo-url> orbit up         # not installed? uvx pulls it in on the fly
```

### Customize inside a repo

When you need to edit role prompts, customize the workflow, and commit them for the team, use `orbit init`: it writes `agents/*.md`, `.orbit/workflow.json`, `team.json`, `.mcp.json`, and a `CLAUDE.md` section into the repo. These are **intentionally not gitignored** so they can be committed and shared.

### Database & operational model

The default database path looks like `~/.orbit/projects/<project-dir-name>-<path-hash>/messages.db`. The project root is probed upward via the nearest `.git` / `pyproject.toml` — starting from a subdirectory still resolves to the same database. To share manually or point at an old database, override with `--db`.

**One project = one daemon = one port.** The db is decided by the daemon's launch directory, independent of which project a client connects from — all clients on the same port share one mailbox. To isolate multiple projects, start a separate daemon per project on distinct `--port`s, and point each project's MCP client config at its own port.

Upgrading from an older version: the old global database at `~/.dev_loop/messages.db` is no longer loaded by default (a notice is printed on startup). To keep using it: `orbit serve --db ~/.dev_loop/messages.db`; to migrate it into a project, `cp` the file to the new path printed at startup.

### Access points

After startup there are two entry points:

- MCP endpoint: `http://127.0.0.1:8848/mcp`
- Local Web UI: `http://127.0.0.1:8848/ui`

Each daemon writes the current project into `~/.orbit/projects/index.json` on startup. Any project's `/ui` can see other project daemons from this index: online projects can be switched directly via the Project dropdown at the top; offline projects show metadata only and require starting the corresponding daemon in that project's directory first. The cross-project UI is an aggregated view only — writes still go to the selected project's own daemon.

## Workflow Engine

The workflow engine is logically three layers — **Scheduler** (decides the next step, advances), **Runner/Worker** (executes agent CLIs), and the `run_jobs` queue between them. They ship in one process by default, but can be split apart:

| Layer | Responsibility |
|---|---|
| **Scheduler** (thread inside serve) | Enqueues "run this step" into `run_jobs`; single-point-consumes finished jobs and advances the workflow (dispatch / rework / accept); runs timeout / health backstops |
| **Runner / Worker** | Claims jobs from `run_jobs` (with lease + heartbeat), executes each agent's CLI, streams stdout/stderr, parses the outcome, and writes the result back to the job |

### Default: single process

```bash
orbit serve        # UI + MCP + Scheduler + embedded Runner, all in one process
```

`serve` **embeds one in-process runner** by default (name `serve-embedded`, concurrency 5), so after kicking off a goal you don't need to start a runner manually — enqueue job → embedded runner executes → scheduler advances, fully automatic. The **Jobs** tab in the UI shows queue state (pending / running / finished / done).

> ⚠️ The embedded runner shares serve's lifecycle: **restarting serve interrupts in-flight steps** (the step auto-reruns once its lease expires). For restart safety / multi-host / horizontal scaling, use the decoupled mode below.

**Job lifecycle:** `pending → running` (runner claims) `→ finished` (runner done, reports outcome) `→ done` (scheduler advances to the next step).

### Decoupled: serve without runner + standalone runners

```bash
# Terminal 1: UI / scheduling only, no embedded worker
orbit serve --no-runner

# Terminal 2+: standalone runners (restarting serve doesn't affect them; multi-instance)
orbit runner --name runner-local
```

In this mode runs live in separate runner processes, so **restarting serve does not kill in-flight tasks** — scheduling pauses briefly, runners keep going.

### Multi-instance runners

A runner is a stateless worker; it can be split by agent / role and run in parallel:

```bash
orbit runner --roles implementer --max-concurrency 2   # 2 parallel implementation workers
orbit runner --roles reviewer --agent antigravity      # only antigravity's reviews
orbit runner --project /path/to/repo --name box-a       # explicit project
```

- `--agent NAME` (repeatable): only claim jobs assigned to this agent.
- `--roles a,b`: only claim jobs for these workflow roles (roles resolve to steps per the workflow config).
- `--max-concurrency N`: run N jobs in parallel, default 5 (each worker has its own lease name `<name>-0/-1/…`).
- `--project PATH`: explicit project root instead of cwd resolution.
- `--once`: claim one job, run it, and exit (good for scripts / CI).

Claiming is an atomic DB-level operation (`UPDATE ... WHERE status=... AND lease<=now`), so concurrent runners never claim the same job twice; if a runner dies, its job is re-claimed by another once the lease expires.

## Connecting Clients

### Claude Code

```bash
claude mcp add --transport http orbit http://127.0.0.1:8848/mcp
```

### Gemini CLI

Add to `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "orbit": {
      "httpUrl": "http://127.0.0.1:8848/mcp"
    }
  }
}
```

### Codex CLI

Add to `~/.codex/config.toml` (recent versions support HTTP transport):

```toml
[mcp_servers.orbit]
url = "http://127.0.0.1:8848/mcp"
```

If your Codex version only supports stdio MCP, bridge with `mcp-remote`:

```toml
[mcp_servers.orbit]
command = "npx"
args = ["-y", "mcp-remote", "http://127.0.0.1:8848/mcp"]
```

### Google Antigravity CLI (agy)

agy loads MCP servers via its plugin mechanism (the `mcpServers` key in `settings.json` is not the config entry). Create a minimal plugin:

```bash
mkdir -p /tmp/orbit-plugin && cd /tmp/orbit-plugin
cat > plugin.json <<'EOF'
{ "name": "orbit", "version": "0.1.0", "description": "orbit mailbox MCP server" }
EOF
cat > mcp_config.json <<'EOF'
{ "mcpServers": { "orbit": { "serverUrl": "http://127.0.0.1:8848/mcp" } } }
EOF
agy plugin install /tmp/orbit-plugin
```

It lands in `~/.gemini/config/plugins/orbit/`. Optional: add entries like `mcp(orbit/register_agent)` to `permissions.allow` in `~/.gemini/antigravity-cli/settings.json` to skip confirmation prompts.

### Other standard MCP clients

Any MCP client that supports the Streamable HTTP transport just points at `http://127.0.0.1:8848/mcp`.

### Custom Python agent (direct SDK)

Connect directly with the official `mcp` package's `streamablehttp_client`: register → long-poll for messages → reply:

```python
import asyncio, json
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = "http://127.0.0.1:8848/mcp"

def parse(result):
    """Tool results arrive as JSON text content blocks."""
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads("".join(c.text for c in result.content if c.type == "text"))

async def main():
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()
            await s.call_tool("register_agent", {
                "name": "my-agent", "description": "custom python agent",
            })
            while True:
                inbox = parse(await s.call_tool("check_inbox", {
                    "agent": "my-agent", "wait_seconds": 30,   # long poll
                }))
                for msg in inbox["messages"]:
                    print(f"from {msg['sender']}: {msg['content']}")
                    await s.call_tool("send_message", {
                        "sender": "my-agent", "to": msg["sender"],
                        "content": "got it, done", "reply_to": msg["id"],
                    })
                    await s.call_tool("ack_message", {
                        "agent": "my-agent",
                        "message_id": msg["id"],
                        "lease_token": msg["lease_token"],
                    })

asyncio.run(main())
```

## MCP Tools

| Tool | Description |
|---|---|
| `register_agent(name, description)` | Register yourself; returns all currently registered agents |
| `list_agents()` | See which agents can receive messages |
| `send_message(sender, to, content, reply_to?, kind?, title?, task_status?)` | Send a prompt or task; `to="*"` broadcasts to all other agents |
| `check_inbox(agent, wait_seconds=0, lease_seconds=300)` | Claim unread messages under a lease; `wait_seconds=30` long-polls near real-time; un-acked messages are re-delivered after the lease expires |
| `ack_message(agent, message_id, lease_token)` | Confirm a message is handled so it won't be delivered again; `lease_token` comes from the message returned by `check_inbox` |
| `get_thread(message_id)` | Walk the `reply_to` chain to retrieve the whole conversation thread |

## Local Web UI

`/ui` is a minimal local console to observe and operate the same SQLite mailbox:

- Switch between running project daemons via the Project dropdown at the top
- View installed common agent CLIs and currently registered sessions
- View recent messages, filtered by `available` / `leased` / `read`
- View task messages, filtered by `created` / `assigned` / `in_progress` / `reviewing` / `accepted` / `blocked` / `stalled` / `closed`
- Pick an agent, claim its inbox, and get the lease + ack token
- View a message thread
- Send programming tasks via the Analyze / Implement / Review / Test templates, or reply via `reply_to`
- Mark task status
- Ack a claimed message
- **Jobs** tab: view the `run_jobs` execution queue (status / outcome / claimant / lease expiry) to confirm the runner is consuming normally
- **Goals** tab: view goal progress and subtree token spend; **Force End** to hard-stop (kill all running runners for that goal + close the whole tree)

The UI only reaches the local store through `/api/*` JSON routes. Merely viewing the message list does not claim messages; only clicking Claim inbox creates a lease.

## Task Collaboration Model

Treat orbit as a lightweight task dispatcher, not a group chat.

### Roles & constraints

Recommended agent split:

- `hub`: the main orchestrator agent. Splits tasks, merges conclusions, edits the main worktree, does final acceptance.
- `impl-*`: implementation agents. Do local implementation or patch proposals.
- `review-*`: review agents. Hunt for bugs, test gaps, and design risks.
- `test-*`: verification agents. Own test plans, failure reproduction, and command output.

Recommended constraints:

1. By default only `hub` writes the main worktree.
2. A worker handles exactly one clearly-bounded small task at a time.
3. A worker's reply must include file references, a conclusion, and how to verify.

### Task message format

Task messages should use `kind="task"`, with:

```json
{
  "title": "Review auth flow",
  "task_status": "assigned",
  "content": "Task Type: review\n\nContext:\n- Repo path: ...\n- Change under review: ...\n\nDeliverable:\n- Findings ordered by severity\n- Missing tests\n- Residual risk"
}
```

### Task statuses

| Status | Meaning |
|---|---|
| `created` | Created, not yet formally dispatched |
| `assigned` | Dispatched to the target agent |
| `in_progress` | Worker has claimed it and started |
| `reviewing` | Reviewer is reviewing |
| `accepted` | Hub accepted the result |
| `blocked` | Worker is blocked, needs input or an environment change |
| `stalled` | Parent goal stalled because a subtask is blocked |
| `closed` | Task archived |

## Orchestrator Mode

When one main agent dispatches tasks to several sub-agents and receives all their replies, replies may arrive concurrently. The storage layer has no races (writes are serialized, `check_inbox` claiming is atomic), but the consuming side must follow two conventions:

1. **Single consumer loop** — the main agent runs exactly one `check_inbox` polling loop. Multiple concurrent polls under the same agent name won't double-claim, but messages get randomly split across consumers.
2. **Process and ack one at a time** — when N replies arrive at once, process them in ascending `id` order, `ack_message` each as you finish, then poll again.

Correlate tasks with `reply_to`: remember the message id returned by `send_message` when dispatching; sub-agents reply with `reply_to`, and the main agent matches them up by it.

```python
# Main-agent consumer loop skeleton
while True:
    inbox = check_inbox(agent="hub", wait_seconds=30)
    for msg in sorted(inbox["messages"], key=lambda m: m["id"]):
        task_id = msg["reply_to"]          # the message id from dispatch
        handle_reply(task_id, msg)         # process one at a time, don't batch into the LLM
        ack_message(agent="hub", message_id=msg["id"], lease_token=msg["lease_token"])
```

## Role Files

The `agents/` directory ships ready-to-use role definitions: `_protocol.md` (shared communication conventions), `hub.md` (orchestrator), `reviewer.md`, `implementer.md`. Bind a role at startup:

```bash
claude --append-system-prompt "$(cat agents/hub.md)"        # main session doubling as orchestrator
agy -i 'read agents/reviewer.md and work as that role'      # agy as reviewer
codex "read agents/implementer.md and work as that role"    # codex as implementer
```

`CLAUDE.md` / `AGENTS.md` / `GEMINI.md` are thin entry points holding only project facts and role pointers.

Add a role: copy `agents/_template.md` to `agents/<role-name>.md` and replace the placeholders — the template carries the naming rules and the criteria for splitting responsibilities.

## Appendix: Tool Return Formats

All tools return JSON (as a text content block, and populate structuredContent where possible).

The agent object in `register_agent` / `list_agents`:

```json
{
  "name": "claude-code",
  "description": "Claude Code session in ~/developer/orbit",
  "registered_at": "2026-07-03T02:43:21+00:00",
  "last_seen": "2026-07-03T03:00:52+00:00"
}
```

`register_agent` wraps it: `{"registered": "claude-code", "agents": [<agent>, ...]}`.

`send_message`:

```json
{ "delivered": 1, "message_ids": [4] }
```

For a broadcast, `delivered` is the actual recipient count (one independent message id each); a broadcast with no other registered agents returns `delivered=0` plus a `note` field.

A direct message requires both `sender` and `to` to be registered agents; if a name doesn't exist, `delivered=0` and an `error` field is returned — avoiding messages that get stuck forever after a typo in the recipient.

`check_inbox`:

```json
{
  "agent": "claude-code",
  "count": 1,
  "messages": [
    {
      "id": 5,
      "sender": "antigravity",
      "recipient": "claude-code",
      "content": "…review content…",
      "reply_to": 4,
      "created_at": "2026-07-03T02:52:30+00:00",
      "delivery_count": 1,
      "lease_expires_at": "2026-07-03T02:57:30+00:00",
      "lease_token": "9b6c0e3d2d2f4d0a8b8b92d5a1b0d3c4"
    }
  ]
}
```

`check_inbox` only claims under a lease; it does not mark messages read. After processing, call `ack_message`:

```json
{ "acked": true, "message_id": 5 }
```

When calling `ack_message` you must pass back the message's `lease_token`. If a consumer crashes or forgets to ack, the message becomes claimable again after `lease_expires_at`, `delivery_count` increments, and a new `lease_token` is issued.

`get_thread` returns an array of message objects (with extra `read_at` / `leased_until` / `lease_owner` fields beyond inbox messages), in ascending id order starting from the thread root.
