# Execute A Goal

Use this procedure when the user asks Orbit to carry out a goal, including when
the workflow was generated earlier in the same task.

First normalize and validate the user's request by following
[execution-request.md](execution-request.md). Keep user-facing workflow inputs
separate from Skill control options; only the normalized `goal`, `input`, and
resolved workflow identity are sent to `start_run`.

## Select The Workflow

1. Call `list_workflows` with `ready_only=true`.
2. Resolve an explicit ID first, then an exact name. Without an explicit
   selector, match the goal against names, descriptions, declared inputs, and
   goal readiness. Use `get_workflow_definition` when published steps are
   needed to distinguish plausible candidates.
3. If one workflow clearly fits, use it. If several materially different
   workflows fit, present their relevant differences and ask the user to
   choose. Do not silently select a workflow whose effects or outcome differ
   from the request.
4. If none fits, apply normalized `options.on_missing`: `fail` reports the
   absence, `ask` offers authoring, and `generate` follows
   [authoring-with-current-app.md](authoring-with-current-app.md). Authoring is
   a distinct mutation and must be authorized by the request or the user's
   answer.

## Start And Follow The Run

1. If `options.dry_run=true`, show the resolved execution preview and stop
   without calling `start_run`.
2. Apply `options.confirm` to the preview. `always` asks before starting;
   `auto` asks when the resolved run has meaningful external effects or a
   material inferred assumption; `never` adds no Skill-level confirmation.
   This option never bypasses host or tool confirmation requirements.
3. Call `start_run` with the selected `workflow_id`, the normalized `goal` and
   `input`, `wait=false`, and a fresh idempotency key. Include
   `workflow_version` only for a concrete selected version, not `latest`.
4. Report the run identifier, then call `inspect_run`. Treat its status,
   revision, interrupts, and `allowed_commands[]` as authoritative.
5. Apply `options.follow`: `none` returns after inspection; `interrupt` follows
   until an interrupt or terminal state; `terminal` follows until a terminal
   state or until user input is required. Use `wait_app_event` when available,
   or bounded repeated inspection. Re-inspect after every event or conflict.
6. Execute only commands present in the latest `allowed_commands[]`. Never
   construct mutation URLs or infer a command from an earlier state.
7. If the run is interrupted, present the workflow, current step, question,
   and choices when available. Call `resume_run` only after the user supplies
   the answer, using the current revision, matching `interrupt_id`, and a fresh
   idempotency key.
8. Stop following when its selected follow condition is met, the user asks to
   stop, or further progress requires user input. Do not cancel a run merely
   because the current task is ending.
9. On completion, summarize the terminal status, workflow and version, run ID,
   failed step when applicable, and relevant committed artifacts. Read
   sensitive console output only when the request requires it.

An idempotency key identifies one logical mutation. Reuse it only to retry the
exact same request after an uncertain result; use a new key for every distinct
start, resume, or cancel operation.

## Combined Author-And-Run Requests

When the user asks to create a workflow and execute a goal in one request,
finish the authoring job first and obtain the published workflow identifier.
Then start that exact workflow using the procedure above. Do not claim the goal
was executed merely because the workflow compiled successfully.
