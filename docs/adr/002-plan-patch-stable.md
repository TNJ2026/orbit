# ADR-002：PlanPatch 契约升级为 Stable

- 状态：Accepted
- 日期：2026-07-18
- 影响版本：Agentic Workflow Contract 1.0 / Migration v7+

## 决策

将 `plan_patch`、`agentic_region`、`policy_decision`、`human_task`、`budget_ledger`、`foreach_scope`、`subflow_link` 和 `dynamic_dag_limits` 从 Draft 升级为 Stable。`cost_estimation` 保持 Draft；动态估算器不能放宽 Handler/Policy 静态上限。

## 升级 Gate

1. `tests/test_workflow_domain.py::test_contract_stability_is_explicit` 固定 `plan_patch == Stable`。
2. PlanPatch 1.0 Schema、Canonical content hash、pending-only、base PlanVersion CAS 和动态 DAG hard limits 进入 Golden/契约测试。
3. 任何破坏上述 Stable 契约的修改必须发布新 SchemaVersion/Migration，并保留旧 Event Replay。
4. Step 10–12 Completion 状态只有在完整回归通过后才能恢复为 Completed。

## 理由

Step 10 已把 PlanPatch 变成 PlanVersion、Policy、Budget 和 Human Approval 的持久化边界。继续标记 Draft 会允许无版本升级地改变已存 Patch 和重放语义，因此必须走显式升级程序。
