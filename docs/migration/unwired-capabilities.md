# Unwired capabilities

**Status**: open, as of the M7 release gate
**Origin**: closing M0 blocker B5 ("Step 11 Foreach/Subflow runtime gap")

The migration (M0–M7) replaced the legacy engine with the durable runtime and
is complete against its own task list. This document records something that
review surfaced and the migration plan never covered: a layer of capability
that is **built and tested but cannot be reached from a running system**.

It is written to be verified, not believed. Every claim below has a command.

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

## Why this is not a migration defect

The migration plan's task lists (M0–M7) never included "add foreach to the
DSL" or "run a planner loop". Its subject was replacing the legacy engine, and
that is done: publish, execute, human intervention, budget, recovery,
observability, the bilingual UI, MCP and the CLI all work end to end on static
graphs.

The honest description of today's system is: **a complete runtime for static
workflow graphs, with the dynamic-planning layer implemented but not
energised.**

The one thing that *was* a migration miss is B5. The M0 baseline assigned it to
M5; M5 was declared done after the git/verify tools without revisiting it.

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

Ship the runtime as it stands, with this gap stated in the release notes.
Nothing here is a correctness risk for what the system actually does today —
these capabilities are unreachable, not broken. Energising them is a product
decision about whether orbit is a static workflow runtime or an agentic one,
and it deserves its own plan rather than being absorbed into a migration that
has met its own gates.
