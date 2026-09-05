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

Orbit starts the Runtime for the current project, opens the native workflow
dashboard beside the Codex conversation, and automatically begins listening
for workflow authoring requests. The full browser UI remains available from
the Runtime URL. While the task is active, `codex` appears in Orbit's
Agent selector and is selected by default.

The listening presence belongs to the active Codex task. Ending the task
removes `codex`; the Runtime may continue running. Start a new task and
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

The UI is available at `http://127.0.0.1:8848/ui`. That page lists the
Workspace Runtimes running on this machine and links into each one's UI; it
starts nothing, so a Workspace whose Runtime is not up does not appear there.

There is one UI, one catalog and one published Workflow library. A Workflow
names the Agents its author chose and runs on them wherever they exist.

A published Workflow pins the exact Handler build it was compiled against, and
for an Agent that build is its CLI version — so a Workflow written on another
machine, or one whose CLI has since been upgraded, names something that is not
here. A step with nowhere to go is carried to an Agent that is, and as little
as possible: to the same Agent's installed build where there is one, and only
failing that to whichever Agent this Runtime is talking to. A step whose Agent
*is* installed is never moved, so a Workflow that deliberately uses two Agents
keeps using two.

Which Agent stands in for a missing one follows the most recent MCP client to
introduce itself, and stays that Agent while none is connected. Where nothing
can be named the published binding stands and the compiler says whether it
resolves, exactly as it would if this fallback did not exist.

The published definition is never rewritten; the substituted graph is stored
with the run, so a finished run still names the Agent that actually executed
it after the connected Agent has changed.

## Use Orbit in Codex

### Open and connect

In a new Codex task, ask:

```text
Open Orbit
```

Codex starts or reuses the project Runtime, opens the UI, and calls
`wait_authoring_request(client="codex-app")`. The wait is automatically renewed
while the task remains active.

### Run a goal

1. Open **Goal**.
2. Select a published workflow, or describe one and let an Agent write it.
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
      "args": ["/absolute/path/to/orbit/start-orbit.sh", "--mcp-proxy"],
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
an online App: the pending call is what makes one addressable. An App asked to
write a Workflow submits the DSL with `submit_authoring_response` and processes
compiler feedback through `get_authoring_job`.

Runtime events can be consumed with `wait_app_event`, `list_app_events`, and
`ack_app_event`. `event_type` is `langgraph_run.<status>` for a run's state
changes and `langgraph_node.<outcome>` for one Handler attempt, which also
carries `node_id` and `attempt_id`. Node events come from Handlers with an
attempt journal — the ones whose execution is an effect that must not repeat —
so a replayed superstep announces nothing. Treat events as hints and re-read
the referenced Run before acting.

A run executes inside the request that starts it. `POST /api/v1/langgraph-runs`
with `"wait": false` returns as soon as the run exists, and executes it in the
background — what the UI asks for, so the page can watch a goal it started.
Everything that decides whether the run may exist has already happened either
way; what waiting buys is being told how it ended.

One goal runs at a time, per actor: starting a second
while one is `running`, `waiting` or `interrupted` is refused with
`active_goal_exists`, and the refusal names the run holding the slot so a
client can go to it. Cancelling or finishing releases it.

Runs are kept until you say otherwise. `/api/v1/ops/status` reports what the
engine is holding, and `create_app(run_retention_days=N)` forgets runs that
ended more than N days ago — whole ones, since a run without its console or
its checkpoints describes itself wrongly. A run waiting on a person, or one
whose Handler ended `unknown`, is never forgotten.

What a run's Handlers printed is read from
`GET /api/v1/langgraph-runs/{run_id}/output?after=<chunk_id>`, which needs the
sensitive scope. It is a console, not a log: bounded per attempt and per
stream, written outside every transaction, and never something a replay reads.

## CLI quick reference

```bash
orbit serve
orbit --version
orbit mcp
orbit mcp --project-root /absolute/path/to/project
orbit run list
orbit run inspect <run_id>
orbit workflow validate <file> --catalog <catalog.json>
orbit workflow publish <file> --catalog <catalog.json> --expected-version <n>
```

`orbit serve` binds to `127.0.0.1` by default. Runtime state and Artifacts are
project-scoped; published Workflow definitions are host-wide and visible from
every Workspace. The Hub also owns reusable Workflow source templates and
aggregates Agent statistics from live Workspace Runtimes.

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
