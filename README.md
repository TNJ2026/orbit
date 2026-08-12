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

### Create a workflow

1. Open **Workflows** in Orbit.
2. Confirm that **Connected Agent** shows `app:chatgpt`.
3. Describe the workflow and select **Generate workflow**.
4. Codex receives the request, returns Workflow DSL, and handles compiler
   feedback until the draft succeeds or fails.
5. Review and publish the generated workflow.

Single-Agent means that the whole graph uses one `agent.*` Handler; it does
not impose a node count or fixed topology. Tools, decisions, parallel branches,
bounded loops, human tasks, and multiple terminal paths remain available.

### Run a goal

1. Open **Goal**.
2. Select a published workflow.
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

To appear in Orbit's Agent selector, the App must keep this call pending:

```text
wait_authoring_request(client="claude-desktop", timeout_seconds=300)
```

Orbit then shows `app:claude-desktop`. Connecting MCP alone does not register
an online authoring App. After receiving a request, the App submits one
Workflow DSL JSON object with `submit_authoring_response`, checks the result
with `get_authoring_job`, and repeats when compiler feedback requests another
attempt.

Runtime events can be consumed with `wait_app_event`, `list_app_events`, and
`ack_app_event`. Treat events as hints and re-read the referenced Run before
acting.

## CLI quick reference

```bash
orbit serve
orbit --version
orbit run start <workflow_id> --goal "..."
orbit run inspect <run_id> --json
orbit workflow validate <file> --catalog <catalog.json>
orbit workflow publish <file> --catalog <catalog.json> --expected-version <n>
orbit db check
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
