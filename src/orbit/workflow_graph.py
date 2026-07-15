"""Pure graph operations for Orbit workflow definitions."""

from __future__ import annotations

from typing import Any


def workflow_graph(cfg: dict[str, Any]) -> set[tuple[str, str]]:
    """Return the workflow's loop-back (rework) edges.

    Explicit ``rework`` markers win. Legacy graphs infer loop-backs from edges
    that close a DFS cycle.
    """
    explicit = {
        (edge["from"], edge["to"]) for edge in cfg["edges"] if edge.get("rework")
    }
    if explicit:
        return explicit
    ids = [step["id"] for step in cfg["steps"]]
    adjacency: dict[str, list[str]] = {}
    for edge in cfg["edges"]:
        adjacency.setdefault(edge["from"], []).append(edge["to"])
    color: dict[str, int] = {}
    back: set[tuple[str, str]] = set()
    for root in ids:
        if color.get(root):
            continue
        color[root] = 1
        stack = [(root, iter(adjacency.get(root, [])))]
        while stack:
            node, children = stack[-1]
            descended = False
            for child in children:
                if color.get(child, 0) == 0:
                    color[child] = 1
                    stack.append((child, iter(adjacency.get(child, []))))
                    descended = True
                    break
                if color[child] == 1:
                    back.add((node, child))
            if not descended:
                color[node] = 2
                stack.pop()
    return back


def forward_out(
    cfg: dict[str, Any], back: set[tuple[str, str]], step_id: str
) -> list[str]:
    return [
        e["to"] for e in cfg["edges"]
        if e["from"] == step_id and (e["from"], e["to"]) not in back
    ]


def workflow_entry_steps(cfg: dict[str, Any], back: set[tuple[str, str]]) -> list[str]:
    # An entry step has no forward incoming edge (a back edge into it, e.g. an
    # accept -> intake reopen, still leaves it an entry). But an explicit rework
    # target is never an entry, even though its only incoming edges are loop-backs.
    forward_in = {
        e["to"] for e in cfg["edges"] if (e["from"], e["to"]) not in back
    }
    rework_targets = {e["to"] for e in cfg["edges"] if e.get("rework")}
    return [
        s["id"] for s in cfg["steps"]
        if s["id"] not in forward_in and s["id"] not in rework_targets
    ]


def workflow_terminal_steps(
    cfg: dict[str, Any], back: set[tuple[str, str]]
) -> list[str]:
    forward_out_src = {
        e["from"] for e in cfg["edges"] if (e["from"], e["to"]) not in back
    }
    return [s["id"] for s in cfg["steps"] if s["id"] not in forward_out_src]


def workflow_execution_errors(
    cfg: dict[str, Any], back: set[tuple[str, str]]
) -> list[str]:
    # Entry/terminal/reachability all use the forward graph (loop-back edges
    # excluded) — the same classification the engine routes by. Raw edges
    # would misjudge legitimate patterns like an accept -> intake reopen
    # loop as a workflow with no entry at all.
    ids = [step["id"] for step in cfg["steps"]]
    steps = {step["id"]: step for step in cfg["steps"]}
    entries = workflow_entry_steps(cfg, back)
    terminals = workflow_terminal_steps(cfg, back)

    def _reach(seeds: list[str], reverse: bool = False, include_back: bool = False) -> set[str]:
        graph: dict[str, list[str]] = {}
        for edge in cfg["edges"]:
            if not include_back and (edge["from"], edge["to"]) in back:
                continue
            src, dst = (
                (edge["to"], edge["from"]) if reverse
                else (edge["from"], edge["to"])
            )
            graph.setdefault(src, []).append(dst)
        seen: set[str] = set()
        stack = list(seeds)
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(graph.get(node, []))
        return seen

    errors: list[str] = []
    if not entries:
        errors.append("no entry step")
    if not terminals:
        errors.append("no terminal step")
    for step in cfg["steps"]:
        if step.get("type") == "end" and any(
            edge["from"] == step["id"] for edge in cfg["edges"]
        ):
            errors.append(f"end step '{step['id']}' must not have outgoing edges")
        if step.get("decompose") and not forward_out(cfg, back, step["id"]):
            errors.append(f"decompose step '{step['id']}' has no forward successor")
        if step.get("unrouted", "allowed") != "allowed":
            routed_ports = {
                edge.get("port") or ("rework" if edge.get("rework") else "success")
                for edge in cfg["edges"]
                if edge["from"] == step["id"]
            }
            missing_ports = [
                port for port in step.get("ports", ["success", "rework"])
                if port not in routed_ports
            ]
            if missing_ports:
                errors.append(
                    f"step '{step['id']}' has unrouted ports: " + ", ".join(missing_ports)
                )
    for source in ids:
        by_port: dict[str, list[str]] = {}
        for edge in cfg["edges"]:
            if edge["from"] != source:
                continue
            port = edge.get("port") or ("rework" if edge.get("rework") else "success")
            by_port.setdefault(port, []).append(edge["to"])
        for port, targets in by_port.items():
            if any(steps[target].get("type") == "end" for target in targets) and len(targets) > 1:
                errors.append(
                    f"step '{source}' port '{port}' mixes an end step with other targets"
                )
    main_entry = entries[0] if entries else None
    # Reachability includes rework edges, so an explicitly rework-only step is
    # still reachable, not dead.
    reachable = _reach([main_entry], include_back=True) if main_entry else set()
    can_finish = _reach(terminals, reverse=True) if terminals else set()
    required_ids = [step_id for step_id in ids if steps[step_id]["required"]]
    unreachable_required = [step_id for step_id in required_ids if step_id not in reachable]
    if unreachable_required:
        errors.append("required steps unreachable: " + ", ".join(unreachable_required))
    stuck_required = [step_id for step_id in required_ids if step_id not in can_finish]
    if stuck_required:
        errors.append(
            "required steps with no path to terminal: " + ", ".join(stuck_required)
        )
    return errors


def main_workflow_reachable_steps(
    cfg: dict[str, Any], back: set[tuple[str, str]]
) -> list[dict[str, Any]]:
    entries = workflow_entry_steps(cfg, back)
    if not entries:
        return []
    graph: dict[str, list[str]] = {}
    for edge in cfg["edges"]:
        if (edge["from"], edge["to"]) in back:
            continue
        graph.setdefault(edge["from"], []).append(edge["to"])
    seen: set[str] = set()
    stack = [entries[0]]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, []))
    return [step for step in cfg["steps"] if step["id"] in seen]


# Outcomes that close out one dispatch of a step: the agent finished it
# (done/rework), the engine took it away (reassigned on timeout), or it was
# manually skipped. `blocked` is deliberately excluded so a blocked step stays
# "active" and can be recovered by completing it directly (see running_steps for
# the dispatch-guard variant that does treat blocked as finished).
_STEP_FINISHING_OUTCOMES = ("done", "rework", "reassigned", "skipped")


def last_dispatch_and_finish(
    transitions: list[dict[str, Any]],
) -> tuple[dict[str, tuple[int, str]], dict[str, int]]:
    """Per step: the most recent dispatch (id, assignee) and the id of the most
    recent finishing transition. A step is active when its latest dispatch has
    no finish after it. Using the LATEST dispatch rather than a dispatch/finish
    count means a runner killed before it finished (a dispatch with no matching
    finish) does not phantom-activate the step forever across restarts."""
    last_dispatch: dict[str, tuple[int, str]] = {}
    last_finish: dict[str, int] = {}
    for t in transitions:
        if t["outcome"] == "dispatched":
            last_dispatch[t["to_step"]] = (t["id"], t.get("note", ""))
        elif t["outcome"] in _STEP_FINISHING_OUTCOMES and t["from_step"]:
            last_finish[t["from_step"]] = t["id"]
    return last_dispatch, last_finish


def active_steps(transitions: list[dict[str, Any]]) -> list[str]:
    last_dispatch, last_finish = last_dispatch_and_finish(transitions)
    return [
        step for step, (did, _) in last_dispatch.items()
        if did > last_finish.get(step, 0)
    ]


def running_steps(transitions: list[dict[str, Any]]) -> list[str]:
    """Steps whose runner is still executing. Like active_steps, but a self-
    reported `blocked` (from_step set) also ends the run: a blocked step's runner
    has exited, so it is not running even though it stays "active" (recoverable by
    completing it). Used by the dispatch guard so a settled-blocked downstream
    step doesn't read as running and suppress a fresh upstream completion — while
    the recovery path (active_steps) still lets a blocked step be completed."""
    last_dispatch: dict[str, int] = {}
    last_finish: dict[str, int] = {}
    for t in transitions:
        if t["outcome"] == "dispatched":
            last_dispatch[t["to_step"]] = t["id"]
        elif t["from_step"] and t["outcome"] in ("done", "rework", "reassigned", "blocked", "skipped", "approval"):
            last_finish[t["from_step"]] = t["id"]
    return [
        step for step, did in last_dispatch.items()
        if did > last_finish.get(step, 0)
    ]


_WORKFLOW_STATUS_OVERRIDES = {"blocked", "closed"}


def workflow_derived_task_status(
    task: dict[str, Any],
    transitions: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> str:
    stored = task.get("task_status") or task.get("status") or ""
    if task.get("is_goal"):
        return stored
    if stored in _WORKFLOW_STATUS_OVERRIDES:
        return stored
    return stored
def active_step_assignees(transitions: list[dict[str, Any]]) -> dict[str, str]:
    last_dispatch, last_finish = last_dispatch_and_finish(transitions)
    return {
        step: assignee
        for step, (did, assignee) in last_dispatch.items()
        if did > last_finish.get(step, 0)
    }


def dispatched_since(
    transitions: list[dict[str, Any]], step: str, transition_id: int
) -> bool:
    return any(
        t["id"] > transition_id
        and t["outcome"] == "dispatched"
        and t["to_step"] == step
        for t in transitions
    )


def latest_inbound_completion_id(
    transitions: list[dict[str, Any]], step: str
) -> int:
    """Id of the most recent transition that (re)entered `step`: a forward `done`
    into it or a `rework` back into it. This marks the start of the step's current
    dispatch cycle. Keying the "already dispatched this pass" guard off it (rather
    than off the last rework anywhere) means a manual re-run or a rework-limit
    recovery that re-ran the upstream — neither of which records a `rework` — still
    lets the freshly-completed upstream re-dispatch this step instead of the guard
    treating an earlier cycle's dispatch as current."""
    return max(
        (t["id"] for t in transitions
         if t["to_step"] == step and t["outcome"] in ("done", "rework", "skipped")),
        default=0,
    )


def join_ready(
    target: str,
    cfg: dict[str, Any],
    back: set[tuple[str, str]],
    steps: dict[str, dict[str, Any]],
    transitions: list[dict[str, Any]],
) -> bool:
    # A rework target feeds back into a shared step but is not a parallel branch
    # of it, so it must not gate the join.
    rework_targets = {e["to"] for e in cfg["edges"] if e.get("rework")}
    predecessor_edges = [
        e for e in cfg["edges"]
        if e["to"] == target
        and (e["from"], e["to"]) not in back
        and e["from"] not in rework_targets
    ]
    required_preds = [
        edge["from"] for edge in predecessor_edges
        if steps[edge["from"]]["required"]
    ]
    if steps[target].get("type") == "join":
        required_preds = [edge["from"] for edge in predecessor_edges]
    arrived = {
        t["from_step"] for t in transitions
        if t["to_step"] == target
        and t["outcome"] in ("done", "skipped", "not_selected", "cancelled")
    }
    if steps[target].get("join_policy") == "any":
        successful = {
            t["from_step"] for t in transitions
            if t["to_step"] == target and t["outcome"] in ("done", "skipped")
        }
        return any(pred in successful for pred in required_preds)
    return all(pred in arrived for pred in required_preds)
