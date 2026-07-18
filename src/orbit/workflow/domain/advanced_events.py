"""Event catalog for dynamic plan, human, budget and control-plane facts."""

ADVANCED_EVENT_VERSIONS = {
    name: 1 for name in (
        "plan_patch_committed", "plan_patch_rejected",
        "human_task_created", "human_task_claimed", "human_task_submitted",
        "human_task_cancelled", "human_task_escalated",
        "budget_account_opened", "budget_reserved", "budget_usage_reported",
        "budget_reservation_settled", "budget_reservation_released", "budget_added",
        "foreach_group_created", "foreach_item_transitioned", "foreach_aggregated",
        "subflow_link_created", "subflow_link_transitioned",
        "recovery_action_applied", "capability_issued", "capability_revoked",
    )
}
