# Execution Request

Normalize every goal-execution request before selecting or starting a
workflow. Accept ordinary language and structured YAML-like input; do not
require the user to learn the full form for simple requests.

## Canonical Model

```yaml
action: execute
workflow:
  selector:
    type: id | name | auto
    value: string | null
  version: latest | integer
goal: string
input: {}
options:
  dry_run: false
  follow: interrupt
  on_missing: ask
  confirm: auto
```

Only `goal`, `input`, and the resolved workflow ID and concrete version map to
`start_run`. `options` controls the Skill. Generate `idempotency_key` internally
and always call `start_run` with `wait=false`.

Defaults are `selector.type=auto`, `version=latest`, `input={}`,
`dry_run=false`, `follow=interrupt`, `on_missing=ask`, and `confirm=auto`.

## Accepted Forms

A short request such as:

```text
Use "Service release" to deploy the current branch to staging.
```

normalizes to a name selector plus the stated goal. A token that exactly
matches a published workflow ID normalizes to an ID selector; otherwise treat
an explicit workflow token as a name. Never treat a fuzzy name match as an ID.

For precise control, accept a structured request:

```yaml
workflow:
  id: wf_release_01
  version: 3
goal: Deploy the current branch and verify its health check
input:
  environment: staging
  branch: current
options:
  follow: terminal
  dry_run: false
  on_missing: fail
  confirm: auto
```

`workflow.id` and `workflow.name` are mutually exclusive aliases for the
canonical selector. Reject conflicting selectors rather than choosing one.
Treat requests phrased as preview, plan, or preflight as `dry_run=true` unless
the user explicitly asks to start the run too.

## Resolve And Validate

1. Preserve explicit user values. Fill declared workflow defaults next, then
   project context that is direct and unambiguous. Label any remaining inferred
   value in the preview; do not infer a material target or external effect.
2. Validate `input` against the selected workflow's declared inputs. Ask only
   for missing required values. Reject invalid types, invalid enumerations, and
   unknown fields unless the workflow explicitly permits additional fields.
   Offer an obvious spelling correction rather than applying it silently.
   When a goal-ready workflow declares a required inline object input named
   `prompt` and binds `run.goal` to its `goal` property, materialize that input
   explicitly as `input.prompt.goal=<normalized goal>` unless the user already
   supplied `input.prompt`. Do not assume every connected Runtime version will
   synthesize this object from `goal` alone.
   Never call `start_run` to probe or discover the accepted input shape: the
   tool is bound to the goal-execution MCP App, so even a validation failure can
   leave a separate error card in the conversation. Resolve the shape from the
   inspected workflow definition and make the first `start_run` call valid.
3. Validate option values exactly:
   - `follow`: `none`, `interrupt`, or `terminal`.
   - `on_missing`: `ask`, `generate`, or `fail`.
   - `confirm`: `auto`, `always`, or `never`.
   - `dry_run`: boolean.
4. Resolve `latest` to the current published version for the preview, but omit
   `workflow_version` from `start_run` unless the user selected a concrete
   version. A run remains pinned to the definition it started with.

Track each resolved input value conceptually as `user`, `workflow_default`,
`project_context`, or `inferred`. The labels are for explanation and preview;
do not add them to the workflow's `input` object.

## Execution Preview

Before a dry run or confirmation, show a compact preview containing:

- workflow name, ID, and resolved version;
- goal and validated input;
- defaults or inferred assumptions that materially affect execution;
- known effects when Orbit exposes them, otherwise any effects evident from
  the published definition;
- follow, missing-workflow, and confirmation behavior.

Do not claim an effect classification is authoritative when the Runtime does
not expose one. `confirm=never` only suppresses an extra Skill preview prompt;
it does not waive any host, browser, MCP, or external-system confirmation.

## State Model

Use these conceptual states and do not skip their exit conditions:

```text
parse -> resolve_workflow -> validate_input -> preview
      -> start -> inspect -> follow
      -> interrupted -> wait_for_user -> resume -> inspect
      -> succeeded | failed | cancelled
```

Do not start before resolution and validation, start a new run to answer an
interrupt, continue polling a terminal run, or cancel merely because the
current task ends.
