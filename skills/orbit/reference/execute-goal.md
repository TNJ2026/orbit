# Execute A Goal

Use this procedure when the user asks Orbit to carry out a goal, including when
the workflow was generated earlier in the same task.

First normalize and validate the user's request by following
[execution-request.md](execution-request.md). Keep user-facing workflow inputs
separate from Skill control options; only the normalized `goal`, `input`, and
resolved workflow identity are sent to `start_run`.

## Select The Workflow

1. When the request includes an explicit workflow ID, call
   `inspect_workflow_definition` for that ID directly and validate its published
   definition and goal readiness. This read has no App card binding. Do not call
   `list_workflows` first, and do not call `get_workflow_definition` as part of
   execution: direct goal execution must not open the workflow-list or
   workflow-detail card.
2. For an exact-name selector or no explicit selector, call `inspect_workflows`
   with `ready_only=true` and match the goal against names, descriptions,
   declared inputs, and goal readiness. This is the read that returns the
   catalogue as values and draws no card; `list_workflows` answers with a
   count and a pointer here, because its card is what a person was shown. Call
   `list_workflows` as well only when the request was also to *see* the list.
   Use `inspect_workflow_definition` when published steps are needed to
   distinguish plausible candidates, so selecting a goal does not replace the
   list with a workflow-detail card. If the host rendered the workflow-list
   MCP App, do not duplicate its rows in a Markdown table or another list.
   State only the selected workflow and the material reason for the selection
   before starting it. Provide a textual candidate list only when no card was
   rendered or when the user must choose between materially different matches.
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
   `never` adds no Skill-level confirmation. With `auto`, an explicit request
   to execute, run, or start the resolved workflow already authorizes the
   `start_run` mutation: do not ask again merely because the workflow contains
   later external-effect nodes, especially when a human interrupt or Runtime
   confirmation gates those effects. Ask only for a material inferred
   assumption, or when starting the run itself will immediately perform an
   external write that the request did not authorize and no later approval
   gate protects. This option never bypasses host, tool, Runtime, or workflow
   confirmation requirements at the point where an effect is actually
   authorized.
3. Call `start_run` with the selected `workflow_id`, the normalized `goal` and
   `input`, `wait=false`, and a fresh idempotency key. Include
   `workflow_version` only for a concrete selected version, not `latest`.
   This call opens the dedicated goal-execution MCP App, which shows only this
   run's progress and result. Do not call `open_orbit_dashboard` first unless
   the user separately asked to open Orbit.
4. Report the run identifier, then call `inspect_run`. Treat its status,
   revision, interrupts, and `allowed_commands[]` as authoritative.
5. Apply `options.follow`: `none` returns after inspection; `interrupt` follows
   until an interrupt or terminal state; `terminal` follows until a terminal
   state or until user input is required. Use `wait_app_event` when available,
   or bounded repeated inspection. Re-inspect after every event or conflict.
   Before following a new or resumed Run, call `list_delegations` with its
   default open statuses. Surface any `unknown` item for reconciliation; do
   not execute it again. When the user verifies it succeeded, call
   `reconcile_delegation` with `outcome: "confirmed_succeeded"` and the exact
   object result; Orbit restores that node from the recorded result and
   continues the existing Run without invoking the Agent again. For a verified
   failure, submit `outcome: "confirmed_failed"` and `error`. Continue or
   observe a still-leased item only when its
   `worker_id` is this task's stable worker ID. Then, while following, call
   `claim_delegation` with that stable worker ID for this
   task. A non-null delegation is a `run_initiator` `app.delegate` Agent step
   assigned to the current conversation: follow its request, execute it with the App's normal
   tools, renew before its lease expires when necessary, and call
   `checkpoint_delegation` after an independently resumable internal stage.
   Use the returned `checkpoint_revision` as the next expected revision and
   store enough structured state to continue without guessing which stage
   completed. A checkpoint also renews the lease. If the same task reconnects
   while that lease is still valid, continue from the returned checkpoint;
   an expired delegation is still `unknown` and must never be resumed merely
   because it has a checkpoint. Then call
   `complete_delegation` with exactly one of `result` or `error`. Continue
   claiming until none is queued. Never wait synchronously for a Run while it
   may be waiting for this same conversation; `start_run(wait=false)` above is
   what prevents that deadlock.
6. Execute only commands present in the latest `allowed_commands[]`. Never
   construct mutation URLs or infer a command from an earlier state.
7. If the run is interrupted, present the workflow, current step, question,
   and choices when available. Call `resume_run` only after the user supplies
   the answer, using the current revision, matching `interrupt_id`, and a fresh
   idempotency key. Build `value` from the interrupt's `output_ports`: its
   top-level keys are port IDs, never conversational labels invented from the
   answer. For a single `result` approval port, “approve”/“批准” maps to
   `{"result":{"decision":"approve","value":null}}`; rejection maps to the
   same shape with `decision:"reject"`. Re-inspect rather than guessing when
   the interrupt does not declare a usable output shape.
8. Stop following when its selected follow condition is met, the user asks to
   stop, or further progress requires user input. Do not cancel a run merely
   because the current task is ending.
   If this task is interrupted after claiming a delegation, do not let another
   task retry it: lease expiry deliberately makes the outcome unknown. On the
   next inspection, surface the Runtime's reconciliation requirement.
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
