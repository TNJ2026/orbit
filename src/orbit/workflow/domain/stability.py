"""Contract stability levels for the 1.0 domain baseline."""

from enum import Enum
from types import MappingProxyType


class ContractStability(str, Enum):
    FROZEN = "frozen"
    STABLE = "stable"
    DRAFT = "draft"


CONTRACT_STABILITY = MappingProxyType(
    {
        "state_machines": ContractStability.FROZEN,
        "event_envelope": ContractStability.FROZEN,
        "error_categories": ContractStability.FROZEN,
        "identifiers": ContractStability.FROZEN,
        "idempotency": ContractStability.FROZEN,
        "transaction_invariants": ContractStability.FROZEN,
        "dsl_core": ContractStability.STABLE,
        "workflow_ir_core": ContractStability.STABLE,
        "handler_result": ContractStability.STABLE,
        "handler_sdk": ContractStability.STABLE,
        "handler_manifest": ContractStability.STABLE,
        "handler_execution_registry": ContractStability.STABLE,
        "handler_usage_reporting": ContractStability.STABLE,
        "port_data_policy": ContractStability.STABLE,
        "value_store_contracts": ContractStability.STABLE,
        "artifact_contracts": ContractStability.STABLE,
        "artifact_backend_port": ContractStability.STABLE,
        "data_repository_ports": ContractStability.STABLE,
        "artifact_access_capability": ContractStability.STABLE,
        "input_manifest": ContractStability.STABLE,
        "data_commit_manifest": ContractStability.STABLE,
        "ports": ContractStability.STABLE,
        "usage_snapshot": ContractStability.STABLE,
        "budget_accounting_invariants": ContractStability.STABLE,
        "runtime_commands": ContractStability.STABLE,
        "runtime_events": ContractStability.STABLE,
        "execution_plan_v1": ContractStability.STABLE,
        "execution_plan_v1_2": ContractStability.STABLE,
        "static_graph_contract_1_2": ContractStability.STABLE,
        "graph_policy": ContractStability.STABLE,
        "graph_decision_facts": ContractStability.STABLE,
        "graph_runtime_decisions": ContractStability.STABLE,
        "graph_persistence_v5": ContractStability.STABLE,
        "planner_provider_port": ContractStability.STABLE,
        "planner_unknown_replay_semantics": ContractStability.STABLE,
        "planning_context": ContractStability.STABLE,
        "planner_attempt": ContractStability.STABLE,
        "action_proposal_v1": ContractStability.STABLE,
        "runtime_kernel_port": ContractStability.STABLE,
        "durable_execution_records": ContractStability.STABLE,
        "durable_execution_ports": ContractStability.STABLE,
        "durable_commands": ContractStability.STABLE,
        "durable_events": ContractStability.STABLE,
        "planner_action": ContractStability.STABLE,
        "action_proposal": ContractStability.STABLE,
        "plan_patch": ContractStability.STABLE,
        "agentic_region": ContractStability.STABLE,
        "policy_decision": ContractStability.STABLE,
        "human_task": ContractStability.STABLE,
        "budget_ledger": ContractStability.STABLE,
        "foreach_scope": ContractStability.STABLE,
        "subflow_link": ContractStability.STABLE,
        "dynamic_dag_limits": ContractStability.STABLE,
        "capability_security": ContractStability.STABLE,
        "api_command_envelope": ContractStability.STABLE,
        "run_view_model": ContractStability.STABLE,
        "cost_estimation": ContractStability.DRAFT,
        "budget_exhaustion_policy": ContractStability.STABLE,
    }
)
