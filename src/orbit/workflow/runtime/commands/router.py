"""Exhaustive command-family routing without opening transactions."""

from __future__ import annotations


FAMILIES = {
    "start_run": "run_node", "schedule_node": "run_node",
    "start_attempt": "run_node", "complete_attempt": "run_node",
    "fail_attempt": "run_node", "cancel_run": "run_node",
    "cancel_node": "run_node", "advance_graph": "graph",
    "advance_foreach": "foreach",
    "submit_human_task": "human",
    "apply_planner_proposal": "planner",
    "reject_planner_proposal": "planner",
    "apply_subflow_result": "subflow",
}


def command_family(command_type: str) -> str:
    try:return FAMILIES[command_type]
    except KeyError:raise ValueError(f"unknown command family for {command_type}") from None


class CommandRouter:
    def __init__(self, owner) -> None:self.owner=owner
    def dispatch(self, context):
        command_family(context.command.command_type)
        return getattr(self.owner, f"_{context.command.command_type}")(context.uow, context.command, context.events)
