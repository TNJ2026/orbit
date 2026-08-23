# Orbit for DeepSeek Harness

This directory is the installable Host Profile Bundle for `deepseek-harness`.
Orbit Runtime stays an independent process. The `OrbitGateway` discovers the
`orbit serve` instance published for the normalized Workspace, performs the
capability handshake, and communicates with it only through HTTP MCP.

## Prerequisites

- Install `orbit` so the executable is on the Harness Host's `PATH`.
- Start Orbit independently for the Harness Workspace:

  `orbit serve --project-root /absolute/path/to/workspace --mcp-tool-profile harness`

By default the Gateway discovers ownership records below `~/.orbit`. If Orbit
uses a database outside that tree, set `ORBIT_RUNTIME_ROOT` for the Harness
Profile to the directory containing the Runtime ownership record. The same
setting works on macOS, Linux and Windows; no platform-specific socket path is
required.

Install this directory into the target Harness Web Profile with one command:

`dsh plugin --profile web add /absolute/path/to/orbit/integrations/deepseek-harness`

Then restart that Profile. Remove it with
`dsh plugin --profile web remove @orbit-runtime/dsh-orbit`. Client code uses the
bundle's same-origin Host API; it never receives the Runtime endpoint, child
process handle, actor header, or Orbit loopback credentials.

Maintainers can verify install, Host/Web startup, HTTP readiness and clean
removal in an isolated temporary Profile with `npm run smoke:profile`. Set
`DSH_BIN` only when the Harness launcher is not named `dsh` on `PATH`. To test
the exact release artifact, set `DSH_BUNDLE_SPEC` to an absolute `.tgz` path.

## Compatibility

| Component | Supported range |
| --- | --- |
| Orbit Runtime | `>=0.4.0 <0.5.0` |
| Orbit integration protocol | `orbit-harness/1` |
| Harness packages | `>=0.1.1-rc.2 <0.2.0` |
| Verified Harness launcher | `0.1.0-rc.6` |
| React | `^18.2.0` |
| Node.js | `>=22` (verified with Harness bundled Node.js 26) |

The Runtime ownership, scoped identity, real HTTP MCP process E2E, TypeScript
build and package surface run in CI on Linux, macOS and Windows. The Profile
install/start/remove smoke additionally requires a locally installed `dsh`
launcher and is exposed as the maintainer command above.

The Gateway refuses an incompatible Orbit integration protocol during startup.
Runtime codecs also reject malformed core DTOs before they reach the Client.
When an MCP transport fails, the cached endpoint is discarded; the next Bridge
poll or tool call reruns discovery, allowing `orbit serve` to restart on a new
port without restarting Harness.

## Upgrade and rollback

Before upgrading, install the new bundle, rebuild the Profile, and restart it.
The independent Orbit Runtime can remain running when its protocol is compatible.
Verify the Orbit Settings row reports connected and open one historical Run.

To roll back, stop the Profile, reinstall the previous bundle version, rebuild,
and restart. Rollback does not require deleting the Runtime database.

## Current boundary

The Host exposes Runtime/Run inspection, Steps, Graph, Edges, cursor-based
output, bounded Artifact content, and command execution. Commands are accepted
only after the Host re-reads the Run and matches the requested command and
revision against Orbit's current `allowed_commands[]`.

This bundle contributes a resident Orbit panel to the Harness shell overlay. It
folds down to a badge that says whether anything is running and opens to a list
of this Workspace's Runs; `/orbit` takes no argument and folds it either way.
The panel can be docked to the side or detached and dragged, and remembers
which between browser sessions.

It deliberately stops there. Graphs, Artifacts and Workflow authoring are drawn
by Orbit's own Runtime UI, which the panel opens in a new tab rather than
redrawing — a second drawing of them here would be a second answer to the same
question, and the bundle carries a test that fails if one starts to appear. Colours come from the shell's
`--dsw-alias-*` tokens, so the panel follows the Harness theme rather than
holding an opinion about it.

Opening a Run shows its steps; opening a step shows that step's output. Nothing
below the first level is fetched until a row is expanded, and following stops
when there is nothing left to follow.

A Run can be cancelled, an interrupted one continued, and a step waiting on a
person ruled on, all from the panel. Every mutation carries the revision the
panel was displaying and is refused if Orbit has moved past it — a button that
quietly acted on a newer Run than the one being read would be worse than one
that fails.

Polling follows the work: every couple of seconds while a Run is moving, every
fifteen while none is, and one round trip per tick that carries only a Session
id — the Host derives the Workspace itself, for reads and writes alike.

## Starting a Run

By saying so. The Agent has `orbit_list_workflows` and `orbit_start_run`, so
"run the CSV cleaner over today's export" is the whole interface; the Run
appears in the panel a moment later.

Neither of you has to remember what exists. The bundle names the ready
Workflows of every bridged Workspace in the model's context — with the inputs
each one needs, which is the mistake it exists to prevent — so the Agent can
answer "which of these did you mean" instead of asking what Orbit is. The panel
lists the same catalog below the Runs, for the person doing the asking, and
`/orbit-workflows` opens the shell's own popup — the one `/model` uses, with
its search box and empty state — and selecting a Workflow opens it in Orbit.

Not starting it: a popupSelect is one list and one pick, with nowhere to put the
goal these Workflows declare an input for, and starting without it is refused by
the Runtime. Orbit's page is where that sentence gets written.

Both read one cache filled by the panel's own poll, so nothing asks twice for
something that changes when a Workflow is published; a Runtime that is down
leaves the last answer standing rather than emptying the context.

The panel deliberately has no start button. Harness is a place where work is
described to an Agent, and a Run the panel started itself would be a Run the
Agent knows nothing about — it could not tell you how it went, or take the
next step from it. Starting through the Agent keeps the Run in the
conversation that asked for it.

Authoring is elsewhere for a different reason: writing or revising a Workflow
means reading the generated DSL, its compile diagnostics and its diagram, and
that is Orbit's own UI, one press away from the panel's title bar.

The Host API at `/plugins/dsh-orbit/api` on the Harness origin remains available
for a caller that wants Run inspection, Steps, Graph, Edges, cursor-based output,
bounded Artifact content, and Attachment import. Every call carries a Workspace,
and the Host trusts none of them: each is checked against the Session it claims
to belong to, or against the Workspace registry, before any Gateway call. It
never lets a caller reach Orbit loopback directly.

Image Artifacts can be imported into Harness Attachment storage after both
Orbit's 2 MiB proxy bound and Harness image admission pass. The current Harness
Attachment contract supports PNG, JPEG, WebP and GIF; other media stays in Orbit.

The diagnostics document contains only Workspace/Session ids, protocol
capabilities, aggregate counts, Gateway counters and Bridge state. It does not
contain the MCP endpoint, actor header, raw output, Artifact bytes, task prompts
or credentials.

The Host automatically attaches one Bridge to every live root Session with a
`cwd`, including Sessions restored during startup. The Bridge derives its
cursor and known Run ids from durable `orbit/run-*` Session events, so a Host
restart resumes without a second cursor database. Session disposal aborts the
poller, and a temporarily unavailable Runtime is retried without blocking the
Session lifecycle.

For the `harness` MCP profile, Orbit accepts `x-orbit-actor` only from loopback,
only on `/mcp`, and only under `harness:session:*`. This refines the existing
single local-operator identity for event and single-goal isolation; it does not
grant a remote caller or local process any additional scope.

## Agent tools

The Host registers a bounded native Harness tool surface which routes each
execution through the same Workspace-aware MCP Gateway:

- `orbit_list_workflows`
- `orbit_list_runs`
- `orbit_inspect_run`
- `orbit_start_run`
- `orbit_cancel_run`
- `orbit_resume_run`

The model never supplies an endpoint, actor, idempotency key, or mutation
revision. The Host derives Workspace and Session from `ToolRunContext`, creates
idempotency keys, and re-reads `allowed_commands[]` before cancel or resume.
`orbit_start_run` always sends `wait: false`; Run progress is projected by the
Session Bridge rather than holding a Harness tool call open.

Every MCP tool call also carries an `orbit/workspace` metadata object derived
by the Host from the Harness Workspace registry: its stable Workspace id and
canonical path, plus isolation metadata when available. A Host API caller
supplies a Workspace too, and it is verified against the Session before use, so
Orbit Runtime discovery and execution follow the Harness Workspace rather than a
caller-constructed `cwd:` identity.

The Orbit CLI takes a non-blocking ownership lock for the runtime database and
publishes its Workspace and MCP endpoint in that ownership record. Harness
never owns that lock and never creates a second writer.

## Agent execution boundary

Harness does not execute Orbit workflow nodes and does not call Harness
Subagent Providers on Orbit's behalf. Agent discovery, CLI credentials,
sandboxing, process cleanup, retry semantics and effects remain owned by the
independent Orbit Runtime. Harness is an MCP client and UI projection only.

Core MCP responses cross runtime codecs before reaching Host or Client code.
Malformed Run, Step, Output, or Artifact payloads fail at the Gateway boundary
instead of flowing through TypeScript assertions.
