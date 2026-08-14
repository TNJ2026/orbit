# Orbit

[简体中文](./README.zh-CN.md) | **English**

Orbit is a local, durable LangGraph workflow Runtime for Agent Apps. It combines
the Runtime, API, Web UI, durable timers, workflow authoring, and MCP integration
in one process. Project data is stored under `~/.orbit/projects/`.

## Install in Codex

Download `orbit-marketplace-<version>.zip` from the matching GitHub Release,
then run:

```bash
unzip orbit-marketplace-<version>.zip
codex plugin marketplace add ./orbit-marketplace
codex plugin add orbit@orbit-local
```

Alternatively, install it from the Codex plugin UI after adding the extracted
Marketplace directory.

1. Open **Plugins** in the Codex app.
2. Find **Orbit** under **Orbit Local** and select **Install**.
3. Start a new Codex task so the installed Skill and MCP tools are loaded.
4. Open the project that should own the workflow Runtime.
5. Ask Codex: `Open Orbit`.

Orbit requires an explicit project directory. If the chat has no project
workspace, startup stops and asks the user to open or select one; it never uses
an incidental process working directory.

Orbit starts the Runtime for the current project, opens
`http://127.0.0.1:8848/ui`, and automatically begins listening for workflow
authoring requests. While the task is active, `app:chatgpt` appears in Orbit's
Agent selector and is selected by default.

The listening presence belongs to the active Codex task. Ending the task
removes `app:chatgpt`; the Runtime may continue running. Start a new task and
ask it to open Orbit to reconnect.

## Install the CLI

Orbit requires Python 3.10 or newer and
[uv](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/TNJ2026/orbit.git
uv tool update-shell
```

To work from source:

```bash
git clone https://github.com/TNJ2026/orbit.git
cd orbit
uv sync --extra dev
uv run orbit serve
```

The UI is available at `http://127.0.0.1:8848/ui`.

The single-Agent authoring UI is the default. Start the unchanged advanced
multi-Agent UI explicitly when needed:

```bash
uv run orbit serve --ui-mode multi-agent
```

## Use Orbit in Codex

### Open and connect

In a new Codex task, ask:

```text
Open Orbit
```

Codex starts or reuses the project Runtime, opens the UI, and calls
`wait_authoring_request(client="chatgpt")`. The wait is automatically renewed
while the task remains active.

### Use a workflow template

1. On the Orbit home page, confirm that **Connected Agent** shows `app:chatgpt`.
2. Choose **Direct execution**, **Plan then execute**, or **Execute then review**.
3. Review the read-only graph, enter the goal, and select **Start goal**; or
   provide a name and select **Publish workflow** to save a reusable flow.
4. Published workflows appear at the top of the selector with their full graph.
5. Before every run, Orbit binds all Agent nodes to the currently connected
   Agent Handler, stores the resolved graph snapshot, and executes it with LangGraph.

Single-Agent mode exposes no DSL, draft, or version number. Publishing reuses
the existing Workflow version store internally for durable storage and graph
projection, while the UI shows only the current definition. Changes affect only
new Runs; an existing Run always recovers from its own graph snapshot. The
multi-Agent UI retains full Workflow authoring.

### Run a goal

1. Open **Goal**.
2. Select a workflow template.
3. Enter the goal and start it.
4. Follow step progress in the workspace or inspect completed runs in
   **History**.

Orbit can also be operated through MCP with `list_runs`, `inspect_run`,
`start_run`, and `cancel_run`. Clients must follow the Runtime's
`allowed_commands[]`; do not construct mutation URLs.

### Stop Orbit

Select **Stop Orbit** beside the Refresh button and confirm. This stops the
Runtime, workers, timers, MCP endpoint, and event connections for the project.

## Connect another Agent App

Any MCP-capable Agent App can connect through Orbit's stdio proxy. Adapt this
example to the App's MCP configuration format:

```json
{
  "mcpServers": {
    "orbit": {
      "command": "bash",
      "args": ["/absolute/path/to/orbit/scripts/start-mcp-proxy.sh"],
      "env": {
        "ORBIT_AGENT_APP_WORKSPACE": "/absolute/path/to/project"
      }
    }
  }
}
```

For Orbit to recognize it as the connected Agent, the App must keep this call pending:

```text
wait_authoring_request(client="claude-desktop", timeout_seconds=300)
```

Orbit then shows `app:claude-desktop`. Connecting MCP alone does not register
an online App. Default single-Agent mode uses the pending call only for presence
and starts templates directly. Only `--ui-mode multi-agent` asks the App to
submit Workflow DSL with `submit_authoring_response` and process compiler
feedback through `get_authoring_job`.

Runtime events can be consumed with `wait_app_event`, `list_app_events`, and
`ack_app_event`. `event_type` is `langgraph_run.<status>` for a run's state
changes and `langgraph_node.<outcome>` for one Handler attempt, which also
carries `node_id` and `attempt_id`. Node events come from Handlers with an
attempt journal — the ones whose execution is an effect that must not repeat —
so a replayed superstep announces nothing. Treat events as hints and re-read
the referenced Run before acting.

## CLI quick reference

```bash
orbit serve
orbit --version
orbit mcp
orbit workflow validate <file> --catalog <catalog.json>
orbit workflow publish <file> --catalog <catalog.json> --expected-version <n>
```

`orbit serve` binds to `127.0.0.1` by default. Runtime state and Artifacts are
project-scoped; published workflow definitions are shared through the local
Orbit workflow library.

## Development

```bash
uv sync --extra dev
.venv/bin/python -m unittest discover -s tests
node --test tests/ui/client_modules.test.mjs
```

Build the Python package with:

```bash
uv build
python scripts/build-marketplace-release.py \
  --version 0.4.0 \
  --output dist/orbit-marketplace-0.4.0.zip
```

Pushing a tag such as `v0.4.0` runs the Release workflow, verifies that the tag
matches `src/orbit/__init__.py`, runs the tests, and uploads the wheel, source
distribution, and local Marketplace ZIP to the GitHub Release.
