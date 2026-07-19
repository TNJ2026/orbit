# Unwired capabilities

**Status**: open — M7 is **not** passed
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
| Planner / Agentic Region | `application/planner_service.py` | 10 | ❌ | ❌ |
| PlanPatch (dynamic DAG) | `application/plan_service.py` | 9 | — | ❌ |

"Built and tested" is not an overstatement — these have event catalogs,
reducers, repositories, migrations, integrity checks and contract tests. What
none of them has is a caller in a running `orbit serve`.

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

### 3. Nothing drives the planner

```bash
grep -rn "request_decision" src/orbit/ | grep -v planner_service.py
grep -n "planner" src/orbit/web/app.py
```

Both are empty. `PlannerApplicationService.request_decision()` has no caller,
and the composition root starts workers, a timer dispatcher and a recovery
scanner — no planner loop. `PlannerRecoveryScanner` exists and is tested, but
nothing constructs it outside tests.

Consequence: even if a workflow could declare an Agentic Region, the planner
attempt it created would never be claimed, and the run would wait forever.

### 4. Foreach is reachable only backwards, through recovery

`RecoveryManager` scans for orphaned Foreach groups and can aggregate them.
That path is now wired (see below), but it can only clean up groups that
something else created — and nothing else does.

## What was fixed while investigating

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
| M2, task 1 (line 283ff) | the composition root manages Worker, Timer, **Planner** and Recovery | `_build_loops()` starts workers, a timer and recovery. No planner loop. |
| M3, task 17 (line 321) | discovery results are policy-checked and registered **before the registry is sealed** | ✅ **fixed.** Discovery now runs ahead of `RuntimeComposition`, and each granted agent is registered as `AgentHandler(TrustedCliAgentClient(...))`. |

So **P1-1 (planner dispatcher) and P1-2 (agent handlers) are migration
defects**, not future scope. P1-2 is now closed; P1-1 remains open.

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
3. **Planner loop in the composition root** — a background loop that claims
   planner attempts, calls the provider, and commits PlanPatches, plus the
   `PlannerRecoveryScanner` alongside the durable one.
4. **Capability and budget policy** — an agentic region's planner calls cost
   money and can enlarge the graph; both need to sit inside the existing budget
   and capability gates rather than beside them.
5. **UI and API** — the plan diff view already exists and would finally have
   something to diff; foreach groups and subflow links need read models and
   inbox representation.

Each of 1–3 is a gate of its own, and 2 touches the kernel, which is the part
of the system with the strongest replay and determinism guarantees.

## Recommendation

**M7 is not passed.** An earlier revision of this document and of the release
notes said it was. Gate 7 (browser E2E over budget, recovery and artifacts) was
claimed on the strength of a test named
`test_a_budget_grant_from_the_ui_moves_the_account` that opened the account
from the backend and never touched the UI, alongside a module docstring listing
coverage that did not exist. That has been corrected: the budget grant is now
driven through the browser, and the two genuine absences — no artifact UI, no
recovery-apply affordance — are pinned by tests that assert the gap.

Before the runtime can be called releasable:

1. Wire the planner loop into the composition root (plan M2 task 1).
2. Register discovered agents before the registry seals (plan M3 task 17).
3. Close the P2 contract violations: budget expected-version, per-finding
   recovery apply, the hardcoded `start_run` endpoint, and the cutover check
   that only guards `serve`.

Foreach, subflow and agentic DSL support remain out of scope and deserve their
own plan.
