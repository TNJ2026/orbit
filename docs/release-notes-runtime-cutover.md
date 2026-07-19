# Release notes — Runtime cutover

> **READY FOR THE STATIC-RUNTIME CUTOVER.** The M7 re-audit is complete. The
> composition root supervises Planner dispatch and recovery, discovered Agent
> handlers register before the registry seals, and a published static workflow
> now reaches and resumes a Human node through the browser. The one remaining
> capability family in
> [docs/migration/unwired-capabilities.md](migration/unwired-capabilities.md) is
> dynamic structure support (foreach, subflow and agentic-region DSL/Kernel
> entry points); it is explicitly outside this migration's release scope.

This release replaces orbit's engine. It is not an upgrade of the previous
system; it is a different one that happens to share a name and a CLI prefix.
Read the data section before installing over an existing project.

## What orbit is now

A local workflow runtime. A workflow is a static graph compiled to an
immutable plan; a run advances by appending events to a log; each node is
executed by a handler registered before the runtime started. Restarting
mid-run resumes from the log rather than from a process's memory.

- One process: `orbit serve` is the runtime, the API, the UI, the workers and
  the timer dispatcher.
- One write path: `/api/v1`, with an idempotency key and an expected version on
  every mutation.
- One place that decides what may be done: the server advertises
  `allowed_commands[]`; clients never construct a mutation URL.

## Data: what is abandoned

**The previous engine's database is not migrated.** Its `messages.db` held task
queues, per-task worktrees, workflow config and — for projects that ran the
transitional builds — some runtime data written before the cutover. None of it
is read by this release.

If a project still has such a file, `orbit serve` **refuses to start** and exits
with code **3**, naming the path it found. It does not open, copy, import or
delete the file. To proceed:

```bash
orbit serve --acknowledge-discard-legacy-data
```

That writes a `0600` marker recording which paths you acknowledged and when —
paths and a timestamp, nothing from inside the file. Afterwards orbit starts
normally and the old file stays exactly where it is; deleting it is your
decision, on your schedule.

There is deliberately **no import path and no compatibility mode**. A partial
import would recreate two sources of truth, which is the specific failure this
cutover exists to end. If you need the old contents, read them with any SQLite
client before deleting them.

A fresh project never sees any of this: no legacy file, no prompt, no marker.

## Removed

| Removed | Replacement |
| --- | --- |
| `orbit start`, `orbit up` | `orbit serve` |
| `orbit init`, `orbit config` | none — the runtime needs no generated config |
| `orbit runner` | workers are in-process; use `--runner-concurrency` |
| `orbit workflow db-check` | `orbit db check` |
| `/api/tasks`, `/api/goals` and the rest of the unversioned API | `/api/v1` |
| the old single-page UI at `/ui` | the modular UI, also at `/ui` |
| `.orbit/workflow.json` | published workflow versions in the database |

Each retired command now fails as an unknown argument rather than doing
something unexpected.

## MCP consumers

`/mcp` is restored on plain JSON-RPC 2.0, but **the tool surface is not the one
that existed before**. The mailbox tools are gone; the tools are now
`list_runs`, `inspect_run`, `start_run` and `cancel_run`, behind the same
identity and authorisation as the HTTP API.

Update your client config — the server block was renamed:

```toml
[mcp_servers.orbit]
url = "http://127.0.0.1:8848/mcp"
```

## Security posture

- `orbit serve` binds loopback and treats the connection, not a header, as the
  identity. A request that did not arrive over loopback gets no identity, so
  exposing the port yields 401s rather than an open runtime.
- Development tooling is opt-in (`--dev-tools`) and cannot be handed a command:
  workflows select a tool by name, argv lives in source, and there is no shell
  in that path.
- Handler secrets are scoped per manifest and redacted from captured output.

## Open capability

- **Dynamic planning has no DSL syntax.** Foreach groups, subflows and agentic
  regions have domain models, services, persistence and contract tests, but no
  node kind expresses them. This is out of the migration's scope and needs its
  own plan; it is listed here so nobody discovers it by writing a workflow that
  will not compile.

## Non-blocking validation limits

- **Windows is untested.** Development tooling assumes POSIX process groups.
- **No visual-regression baseline.** The browser suite drives real interactions
  in both locales, but nothing compares rendered pixels between releases;
  contrast and focus order are covered by static asset guards.

## Testing

```bash
python -m unittest discover -s tests          # 739 tests
node --test tests/ui/client_modules.test.mjs  # client modules, needs node
uv pip install -e '.[dev]' && python -m playwright install chromium
```

A green suite is not a passed gate. The browser suite is paired with the dated
manual execution record below; the record states which release claims were
actually inspected instead of treating test names as proof by themselves.

### M7 manual gate execution record — 2026-07-19

These are execution results, not a restatement of the gate. The source audit,
real-browser paths and restart/cutover checks were rerun after the Planner,
Agent registration and static Human controller changes.

| Manual gate | Evidence executed | Result |
| --- | --- | --- |
| No old Task/Kanban/Message/fixed-development terminology in the UI | Audited the shipped `workflow-ui` assets and ran `tests.test_ui_assets`. Matches for ordinary program words such as `message` and the HumanTask token hint were inspected; none presents a retired product concept. | ✅ Pass |
| Every state and mutation button traces to Projection/AllowedCommand | `tests.test_ui_assets` rejects literal mutation endpoints and requires `allowed.href`; the Chromium Human, Budget, Cancel and Recovery paths clicked the commands advertised by the server. | ✅ Pass |
| A failed run is locatable through Why/Timeline/Error/Artifact surfaces | Chromium drove a real handler validation failure and displayed its message, `validation_error` category and `handler` source; the same run view exposed Timeline and Data/Artifact lineage surfaces. | ✅ Pass |
| Restart requires no database repair | `tests.test_restart_recovery` stopped and recreated the composition over the same SQLite file, completed in-flight work, checked for duplicate attempts, compared projections and ran the integrity checker. | ✅ Pass |
| Data abandonment is disclosed and explicitly acknowledged | The “Data: what is abandoned” section names `messages.db`, no import/compatibility path, refusal exit code 3, the explicit acknowledgement flag, marker contents and the fact that the old file is not deleted. `tests.test_cutover` reran refusal, marker and file-preservation behavior. | ✅ Pass |

Commands and results:

```text
.venv/bin/python -m unittest tests.test_ui_assets tests.test_restart_recovery tests.test_cutover
Ran 45 tests in 2.146s — OK

.venv/bin/python -m unittest tests.test_browser_e2e.HumanTaskTests tests.test_browser_e2e.BudgetTests tests.test_browser_e2e.CancelTests tests.test_browser_e2e.PlanAndRecoveryTests tests.test_browser_e2e.DataAndRecoverySurfaceTests tests.test_browser_e2e.RefreshTests
Ran 14 tests in 8.421s — OK

.venv/bin/python -m unittest discover -s tests
Ran 739 tests in 52.036s — OK
```

The browser suite (`tests/test_browser_e2e.py`) drives a real Chromium against
a real server: locale selection and switching, the new-run dialog, approving
from the inbox, granting budget to an exhausted run, cancelling a run parked on
a person, the plan panel's definition/overlay split, data and lineage, applying
a recovery finding from the ops page, and a failed run's error panel. The Human
workflow is driven end to end in both `zh-CN` and `en-US`, and locale-specific
controls have bilingual assertions. The suite skips when playwright is absent,
so a plain checkout still runs green.

## Verifying an install

```bash
orbit --version
orbit db check            # audits the project database, read-only
orbit serve               # then open http://127.0.0.1:8848/ui
```
