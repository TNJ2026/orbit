# Agentic Workflow Domain Contracts

| 属性 | 值 |
| --- | --- |
| 契约版本 | 1.0 |
| 状态 | Step 7 Stable baseline |
| ADR | `docs/adr/001-self-built-durable-kernel.md` |

本目录包含全新 Agentic Workflow Runtime 的领域契约，与旧 Workflow Engine 隔离。到 Step 7 已实现定义、持久化、确定性 Kernel、Durable Worker、Handler Runtime，以及 Value/Artifact 数据层；Planner、HTTP 与 UI 仍不在当前基线内。

## 术语表

| 术语 | 身份与职责 | 生命周期 | 可变性 | 所有者 | 持久化边界 |
| --- | --- | --- | --- | --- | --- |
| WorkflowDefinition | Workflow 的逻辑身份和草稿容器 | 创建至删除 | 可编辑 | Definition Plane | 步骤 2 |
| WorkflowIR | DSL 解析、规范化和语义校验后的内存表示 | 单次编译 | 不可变 | Compiler | 不单独持久化 |
| WorkflowVersion | 发布后的 IR、能力、Schema 和 Policy 快照 | 永久 | 不可变 | Definition Plane | 步骤 2 |
| WorkflowRun | 某 WorkflowVersion 的一次业务运行 | created 至终态 | 仅通过 Kernel Event 演进 | Runtime Kernel | 步骤 3 |
| ExecutionPlan | 某 Run 当前已经提交的可执行计划 | v1 至 vN | 版本不可变 | Runtime Kernel | 步骤 3 |
| PlanPatch | Planner 对 Pending Plan 的修改提案 | proposed 至 accepted/rejected | Draft 提案不可原地修改 | Planner/Policy | 步骤 10 |
| NodeRun | 某节点的一次业务执行实例 | pending 至终态 | 仅通过 Kernel Event 演进 | Runtime Kernel | 步骤 3 |
| Attempt | NodeRun 的一次实际执行 | created 至终态 | 仅通过 Kernel Event 演进 | Worker/Kernel | 步骤 3 |
| BranchToken | 一条运行分支的完成责任 | active 至终态 | 仅通过 Kernel Event 演进 | Runtime Kernel | 步骤 3 |
| RunEvent | 已发生的不可撤销事实 | 永久 | 只追加、不可修改 | Event Store | 步骤 3 |
| Value | 通过 Port 传递的小型内联 JSON 数据 | 产生至 Run 清理 | 不可变 | Producing NodeRun | 步骤 7 |
| Artifact | 大型或持久化内容及其元数据引用 | 产生至保留策略清理 | 内容不可变，更新创建新 Artifact | Artifact Store | 步骤 7 |
| UsageSnapshot | Attempt 某 Sequence 的累计用量 | Attempt 执行期间至审计清理 | 每个快照不可变 | Handler/Runtime | 步骤 10 |
| BudgetAccount | Run 的总额、预留和实际消费账本 | Run 创建至审计清理 | 版本化不可变快照 | Runtime Kernel | 步骤 10 |
| BudgetReservation | Attempt 执行前占用的预算额度 | reserved 至 settled/released | 不可变 | Runtime Kernel | 步骤 10 |

## 模型脊柱

```text
Workflow DSL
  -> Canonical Workflow IR
  -> immutable WorkflowVersion
  -> WorkflowRun
  -> ExecutionPlan v1
  -> accepted PlanPatch
  -> ExecutionPlan v2 ... vN
  -> NodeRun
  -> Attempt
```

Kernel 后续只能执行已提交的 ExecutionPlanVersion。WorkflowRun 永久绑定 WorkflowVersion；NodeRun 永久记录其来源 PlanVersion。

## 版本语义

- `SchemaVersion`：契约格式版本，例如 `1.0`。
- `Revision`：从 1 开始的 Workflow、Plan、Event Sequence 和 Attempt 序号。
- `AggregateVersion`：从 0 开始的乐观锁版本；0 表示 Aggregate 尚不存在。
- `DefinitionHash`：Canonical JSON 的 SHA-256 摘要。

## 稳定性

- Frozen：状态机、Event Envelope、错误、ID、幂等和事务不变量。
- Stable：核心 IR/DSL 边界、HandlerResult、Port、UsageSnapshot 和 Budget 记账不变量。
- Draft：PlannerAction、ActionProposal、PlanPatch、Agentic Region、成本估算和预算耗尽 Policy。

具体登记见 `domain/stability.py`。

修改政策：

- Frozen：1.x 内只能增加不改变已有语义的辅助 API；字段、状态、Hash、Envelope 或不变量的破坏性变化必须发布新的 Major Contract Version，并保留旧 Event 的 Upcaster/Reducer 路径。
- Stable：允许在下一个 Minor Contract Version 增加可选字段；删除字段或改变既有含义仍需要 Major Version。
- Draft：可以在归属步骤内修改，但必须携带 Draft Version，不能写入标记为 Stable 的持久化结构。
- Draft 进入 Stable 前必须有真实场景原型、Schema、失败语义、Golden fixture、契约测试和评审记录。
- 任何稳定性变化都必须更新 `domain/stability.py`、本文、Fixture 和版本变更记录。

## 状态转换契约

所有状态转换遵守同一冻结规则：

- Command：`transition_<machine>`。
- Event：`<machine>_transitioned`。
- 前置条件：当前状态等于声明源状态，且 Command Expected Version 等于 Aggregate 当前版本。
- 幂等范围：`aggregate_id + idempotency_key`。
- 重复的同语义 Command 返回第一次提交的 Event ID，不追加 Event。
- 相同 Idempotency Key 携带不同语义时拒绝为冲突。

完整状态矩阵由 `domain.states.transition_matrix()` 导出，并锁定在 Golden fixture 中。每条合法转换均可通过 `domain.transitions.transition_contract()` 获得 Command、Event、前置条件和幂等规则。

## 错误与失败策略

错误 Code 不能使用任意字符串，必须登记在 `ERROR_CODE_REGISTRY`，并与固定 Category 匹配。

| Category | Retry | Rework | 人工介入 | 终止 |
| --- | --- | --- | --- | --- |
| validation_error | 否 | 否 | 否 | 是 |
| policy_rejected | 否 | 否 | 是 | 否 |
| transient_error | 是 | 否 | 否 | 否 |
| permanent_error | 否 | 是 | 是 | 是，由 Policy 选择 |
| timeout | 是 | 否 | 是 | 否 |
| cancelled | 否 | 否 | 否 | 是 |
| lost | 是 | 否 | 是 | 否 |
| unknown_external_result | 否 | 否 | 是 | 否 |

## 乐观并发和重复 Command

- `AggregateVersion(0)` 表示 Aggregate 尚不存在。
- 新建 Aggregate 的 Command 使用 Expected Version 0。
- 非重复 Command 的 Expected Version 与当前版本不一致时拒绝，不产生 Event。
- Runtime 按 Idempotency Key 保存 Command Fingerprint 和首次产生的 Event ID。
- 同 Key、同 Fingerprint 返回 `replay_prior_result`；同 Key、不同 Fingerprint 返回 Idempotency Conflict。
- Fingerprint 不包含 Command ID 和 Issued At，但包含 Command Type、Aggregate、Correlation、Expected Version、Actor 和 Payload。

## Correlation 和 Causation

- Correlation ID 必须显式存在于 Command，并由 Command 产生的所有 Event 原样继承。
- 一条业务链默认使用 Root WorkflowRun ID 作为 Correlation ID。
- NodeRun、Attempt、HumanTask 和 Artifact 等不同 Aggregate 的 Event 共享 Root Run Correlation ID。
- Event Causation ID 指向直接产生该 Event 的 Command ID。
- 只有根 StartRun Command 可以默认以自身 Aggregate ID 作为 Correlation ID；所有下游 Command 必须传入已有 Correlation ID。

## Replay 规则

Replay Reducer 只能接收旧 State 和已记录 Event，并返回新 State。Replay 不得调用时钟、随机数、Planner、Handler、Tool、HTTP、Artifact Writer 或任何持久化写操作。

所有外部结果、时间和随机值必须先成为 Event，才能参与 Replay。

`workflow.testing.guarded_replay()` 在契约测试中同时使用源码审计和运行期 Patch，阻止文件、网络、进程、时钟、随机数和 UUID 等常见副作用来源。

## 关键状态决策

- `unknown_external_result` 是 Attempt 终态。迟到结果只能审计，不能改变状态；继续处理必须创建新的 Attempt，或通过 HumanTask 记录人工确认。
- Rework 不是 NodeRun 状态。Rework 是 Graph 路由行为，会创建目标节点的新 NodeRun，并保留旧 NodeRun 作为不可变历史。
- Budget Settlement 必须记录已经发生的真实消费，即使 Consumed 超过 Total。负 Remaining 是合法账本事实，并触发 WorkflowRun 进入 `budget_exhausted`；记账层不能拒绝现实支出。

## 最小线性事件流

```text
StartRun command          correlation=run:001
  -> run_started          aggregate=run:001
  -> node_run_created     aggregate=node_run:001 correlation=run:001
  -> attempt_started      aggregate=attempt:001  correlation=run:001
  -> attempt_succeeded    aggregate=attempt:001  correlation=run:001
  -> node_run_succeeded   aggregate=node_run:001 correlation=run:001
  -> run_succeeded        aggregate=run:001      correlation=run:001
```

完整 Envelope 示例锁定在 `tests/fixtures/workflow_contracts/v1/linear-event-flow.json`。

## 数据与不可变性

- Command/Event Payload 在构造时转换为深度不可变 JSON 数据。
- 所有时间必须携带时区，并按 UTC Canonical JSON 输出。
- JSON Object Key 必须是字符串。
- NaN 和 Infinity 不是合法契约值。
- 所有引用对象、状态和记账对象均为不可变值对象。

## Step 1 非目标

- 不解释或编译 Workflow DSL。
- 不创建数据库表。
- 不保存或调度 Event、Job、Lease 和 Timer。
- 不调用 Agent 或 Tool。
- 不实现动态 PlanPatch。
- 不接入旧 Workflow 配置或运行状态。
