# Unwired capabilities

**Status**: static-runtime migration gates closed; dynamic structure support remains a later milestone
**Origin**: closing M0 blocker B5 ("Step 11 Foreach/Subflow runtime gap")
**Revised**: 2026-07-19, after review found this document itself was wrong

The migration replaced the legacy engine with the durable runtime, and the
static-graph half of that works end to end. This document records a layer of
capability that is **built and tested but cannot be reached from a running
system** — and, in two cases, was required by the migration plan and simply
not wired.

The first revision of this file said the plan "never covered" any of it. That
was wrong; §"What is a migration defect" below has the plan's own line numbers.
Getting this wrong is not a footnote: it moved two defects into the
out-of-scope column, which is exactly how they would have shipped.

Everything here is written to be verified, not believed. Every claim has a
command.

## Summary

| Capability | Domain + service | Tests | Expressible in the DSL | Driven at runtime |
| --- | --- | --- | --- | --- |
| Foreach group / item | `application/foreach_service.py` | 6 | ❌ | ❌ (only recovery) |
| Subflow link | `application/subflow_service.py` | 6 | ❌ | ❌ |
| Planner / Agentic Region | `application/planner_service.py` | 10+ | ❌ | dispatcher ✅; request creation ❌ |
| PlanPatch (dynamic DAG) | `application/plan_service.py` | 9 | — | ❌ |

"Built and tested" is not an overstatement — these have event catalogs,
reducers, repositories, migrations, integrity checks and contract tests. The
Planner dispatcher and recovery loop are now supervised by `orbit serve`, but
no published static workflow creates a Planner request. Foreach, Subflow and
PlanPatch still have no production caller.

## The evidence

### 1. The DSL cannot express them

```bash
grep -n '"kind": {"enum"' src/orbit/workflow/dsl/schema.py
```

```
"kind": {"enum": ["action", "decision", "join", "terminal", "extension"]},
```

There is no `foreach`, `subflow` or `agentic` node kind. A workflow author has
no syntax for any of these, so no published workflow can contain one, so no
plan compiled from a workflow can either.

### 2. The kernel has no commands for them

```bash
grep -n 'RUNTIME_COMMAND_TYPES' -A 2 src/orbit/workflow/domain/runtime.py
```

```
{"start_run", "schedule_node", "start_attempt", "complete_attempt",
 "fail_attempt", "cancel_node", "cancel_run", "advance_graph"}
```

Foreach, Subflow and Planner facts are *control-plane events*
(`domain/advanced_events.py`) appended by their services directly, not kernel
commands. Nothing in `kernel_families.py` creates a foreach group when a node
is scheduled, because no node kind says to.

### 3. The planner is driven, but no static workflow requests it

```bash
grep -rn "request_decision" src/orbit/ | grep -v planner_service.py
grep -n "planner" src/orbit/web/app.py
```

`PlannerApplicationService.request_decision()` still has no caller originating
from a published workflow. When trusted Agent discovery supplies a provider,
however, the composition root now starts `planner-1` and `planner-recovery`;
persisted attempts are claimed, executed and recovered. The remaining gap is
the DSL/kernel producer, not dispatch or lifecycle management.

### 4. Foreach is reachable only backwards, through recovery

`RecoveryManager` scans for orphaned Foreach groups and can aggregate them.
That path is now wired (see below), but it can only clean up groups that
something else created — and nothing else does.

## What was fixed while investigating

The final M7 gap is closed by the static `human` controller. DSL 1.2 compiles
Human nodes into ExecutionPlan 1.2 without assigning a Handler or Job. The
Kernel creates the linked HumanTask and moves Run/NodeRun to `waiting` in one
transaction; an authorised `submit_human_task` command completes the task,
records the Human result, resumes the Run and routes the graph in one second
transaction. Recovery treats an active HumanTask as a durable responsibility,
and the bilingual browser test now drives a published
Transform -> Human -> Terminal workflow without constructing a task directly.

`RecoveryManager` declared `ORPHAN_FOREACH` safe to auto-apply, but
`build_api_v1` constructed it with only the human service. `POST
/api/v1/recovery/apply` therefore raised
`RuntimeError("Foreach service is unavailable")` on the first real Foreach
group. Fixed in `web/api_v1.py`; `tests/test_recovery_wiring.py` now asserts
the property — every auto-applicable finding has a service — rather than a
list, so a new finding cannot be added without wiring it.

`ORPHAN_SUBFLOW` is deliberately **not** auto-applied: its child run is gone,
which needs a person. That is `safe=False` in the scanner and is pinned by a
test so it does not later look like the same oversight.

## What is a migration defect, and what is out of scope

**Corrected 2026-07-19.** An earlier revision of this document claimed the
migration plan "never included run a planner loop". That was wrong, and the
error mattered: it reclassified two of the plan's own requirements as
out-of-scope future work.

The plan does require both:

| Plan | Requirement | Reality |
| --- | --- | --- |
| M2, task 1 (line 283ff) | the composition root manages Worker, Timer, **Planner** and Recovery | ✅ fixed: provider discovery wires supervised Planner dispatch and recovery loops. |
| M3, task 17 (line 321) | discovery results are policy-checked and registered **before the registry is sealed** | ✅ **fixed.** Discovery now runs ahead of `RuntimeComposition`, and each granted agent is registered as `AgentHandler(TrustedCliAgentClient(...))`. |

So **P1-1 (planner dispatcher) and P1-2 (agent handlers) were migration
defects**, not future scope. Both are now closed.

Genuinely out of scope: DSL syntax for foreach, subflow and agentic regions,
and the kernel scheduling that would drive them. No plan task asks for those.

**B5** is a third, separate miss: the M0 baseline assigned it to M5, and M5 was
declared done after the git/verify tools without revisiting it.

The honest description of today's system: **a runtime for static workflow
graphs.** Dynamic planning is implemented at the domain and service layers and
cannot be reached — partly because the DSL has no syntax for it (out of scope),
and partly because the composition root never wires what the plan told it to
(a defect).

## What energising it would take

Not a patch. Roughly a further milestone:

1. **DSL and compiler** — node kinds for foreach, subflow and agentic regions;
   IR representation; validation rules (item scope, recursion depth, region
   boundaries); golden compile tests.
2. **Kernel scheduling** — scheduling a foreach node has to create the group
   and its items, respect the concurrency limit, and aggregate deterministically
   on completion; a subflow node has to start and link a child run and
   propagate cancellation and failure.
3. **Planner request production** — scheduling an Agentic Region must persist a
   PlanningContext/attempt for the already-wired dispatcher; accepted proposals
   must then enter the PlanPatch policy boundary.
4. **Capability and budget policy** — an agentic region's planner calls cost
   money and can enlarge the graph; both need to sit inside the existing budget
   and capability gates rather than beside them.
5. **UI and API** — the plan diff view already exists and would finally have
   something to diff; foreach groups and subflow links need read models and
   inbox representation.

Each of 1–3 is a gate of its own, and 2 touches the kernel, which is the part
of the system with the strongest replay and determinism guarantees.

## Recommendation

The defects found by the M7 re-audit are now closed: budget grant, Artifact
metadata/lineage, per-finding Recovery Apply and responsive/accessibility checks
are driven through the browser; discovered Agent handlers register before seal;
Planner dispatch/recovery is supervised; every browser mutation, including
`start_run`, comes from a server-advertised AllowedCommand; and all CLI entry
points use the cutover gate.

The M7 single-workflow Human scenario is now claimed: a published static
workflow reaches a HumanTask, is submitted from Inbox, resumes its Human node
and succeeds in both supported locales. The Human node is deliberately a
Kernel controller rather than a Handler, so a long human wait never occupies a
Job lease.

The claim includes the credential, not just the buttons. The kernel stores
only the submission token's hash and the in-process delivery adapter does not
survive a restart, so the inbox advertises a `human.token` AllowedCommand:
an authorised participant rotates the token over HTTP
(`POST /api/v1/human-tasks/{id}/token`), which invalidates the old one in the
same transaction. The browser E2E fetches its token through that command —
never out of process memory — so the journey it exercises is exactly the one
an operator has after a restart.

Foreach, subflow and agentic DSL support remain out of scope and deserve their
own plan.
