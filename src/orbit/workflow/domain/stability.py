"""Contract stability levels for the 1.0 domain baseline.

Only contracts the Runtime still has. The table used to also carry the
event-sourced execution engine's vocabulary — durable commands and events,
the planner ports, budget ledger, foreach and subflow scopes — and those
went with the engine. A stability promise about a contract nothing implements
is not a promise kept, it is one nobody can break.
"""

from enum import Enum
from types import MappingProxyType


class ContractStability(str, Enum):
    FROZEN = "frozen"
    STABLE = "stable"
    DRAFT = "draft"


CONTRACT_STABILITY = MappingProxyType(
    {
        "event_envelope": ContractStability.FROZEN,
        "error_categories": ContractStability.FROZEN,
        "identifiers": ContractStability.FROZEN,
        "idempotency": ContractStability.FROZEN,
        "dsl_core": ContractStability.STABLE,
        "workflow_ir_core": ContractStability.STABLE,
        "handler_result": ContractStability.STABLE,
        "handler_sdk": ContractStability.STABLE,
        "handler_manifest": ContractStability.STABLE,
        "handler_execution_registry": ContractStability.STABLE,
        "handler_usage_reporting": ContractStability.STABLE,
        "port_data_policy": ContractStability.STABLE,
        "artifact_contracts": ContractStability.STABLE,
        "artifact_backend_port": ContractStability.STABLE,
        "artifact_access_capability": ContractStability.STABLE,
        "input_manifest": ContractStability.STABLE,
        "data_commit_manifest": ContractStability.STABLE,
        "ports": ContractStability.STABLE,
        "usage_snapshot": ContractStability.STABLE,
        "static_graph_contract_1_2": ContractStability.STABLE,
        "graph_policy": ContractStability.STABLE,
        "graph_decision_facts": ContractStability.STABLE,
        "durable_execution_records": ContractStability.STABLE,
        "api_command_envelope": ContractStability.STABLE,
    }
)
