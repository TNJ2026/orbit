# Agentic Workflow 步骤 10 任务拆分

| 文档属性 | 值 |
| --- | --- |
| 文档版本 | 1.0 |
| 状态 | In progress（发布 Gate 修复中，2026-07-18） |
| 规划日期 | 2026-07-17 |
| 来源规划 | `agentic-workflow-implementation-plan.md` 1.0 |
| 输入基线 | Step 1–9 Completed；Planner Proposal Protocol Stable |
| 对应范围 | 步骤 10：Policy、动态 ExecutionPlan、最小 HumanTask、Budget |
| 参考投入 | 8–13 person-weeks，约 52 person-days（任务表合计 51.5 pd，含 S10-G0） |

## 1. 阶段目标

将 Step 9 的结构化 Proposal 转换为受 Policy、预算、权限和乐观并发保护的 PlanPatch；只允许修改 Agentic Region 中尚未进入 ready 的计划。补齐 approval/input 最小 HumanTask 和运行时预算闭环，形成 Agentic Workflow MVP。

```text
ActionProposal
  -> Schema + Policy + Budget + Graph validation
  -> PlanPatch(base_plan_version)
  -> immutable ExecutionPlanVersion N+1
  -> Kernel executes committed plan only
  -> HumanTask/Budget may place Run in explicit waiting
```

## 2. 范围边界

### 2.1 本阶段负责

- S10-G0：拆分 Runtime Kernel 事务编排，保持唯一入口和行为不变。
- ExecutionPlanVersion、PlanPatch、Agentic Region 和 PolicyDecision 契约。
- Patch Schema/语义/Policy/预算验证、乐观并发和不可变 Plan 提交。
- Completion Proposal 的确定性验证。
- 最小 HumanTask：approval/input、一次性提交、基础身份、跨重启恢复。
- 外部副作用 Approval Gate。
- Budget Account/Reservation/Ledger、流式用量、Unknown 结算和耗尽策略。
- Migration v7、Recovery、Diagnostics、故障注入和 Agentic MVP E2E。

### 2.2 本阶段不负责

- Assignee/Role/Form/Reminder/Escalation/多人会签；属于 Step 11。
- Foreach、Subflow 和一次 Patch 创建任意宽度动态 DAG；属于 Step 11。
- 历史用量动态成本估计；属于 Step 12 Capacity。
- Script Sandbox、生产 RBAC、完整网络/文件 Policy；属于 Step 12。
- 修改 running/succeeded/failed 或已经 ready 的节点。

## 3. 开工前固定的设计决策

### 3.1 Plan 版本脊柱

- WorkflowRun 仍绑定不可变 WorkflowVersion。
- Run 启动得到 ExecutionPlan v1；每个已接受 Patch 生成不可变 vN+1。
- NodeRun/Token/Job 记录 source PlanVersion；历史执行事实不随新 Plan 改写。
- Runtime 只读取 committed PlanVersion；Patch draft/validated 不可执行。
- Patch 只作用于 Agentic Region 的 pending 节点和边。

### 3.2 Patch 原子性与并发

- Patch 固定包含 proposal_id、run_id、base_plan_version、reason、requested_changes 和 content hash。
- PolicyDecision、Budget Reservation、PlanVersion、Event、projection 和 Receipt 在一个 UoW 提交。
- 并发 Patch 使用 base_plan_version CAS；只有一个胜者，失败者重新规划。
- 相同 Patch ID/Hash 返回原结果；相同 ID 不同内容为 Integrity Violation。

### 3.3 Policy 与执行隔离

- Policy Validator 是确定性纯函数，输入为 Proposal、Plan、Catalog、Capabilities、Budget Snapshot 和 Approval Facts。
- Planner 不能声明自己已获授权、已满足预算或已完成目标。
- Policy Decision 保存 rule version、输入 hash、逐条结果和 reject reason。
- 外部写 Job 只能在 Approval Event 已提交且 capability scope 匹配后创建。

### 3.4 HumanTask 唯一模型

- 只有 `human_tasks`，不创建 InteractionRequest 第二模型。
- M4 子集只有 approval/input、approve/reject/provide_input、一次性 submission token、Expected Version、Actor Identity。
- Step 11 通过增量字段扩展同一模型。

### 3.5 Budget 运行时语义

- Run Budget 保存 total/reserved/consumed/remaining/version；Reservation 按 Attempt/Planner Attempt 唯一。
- 启动前原子预留；静态估算来自 Handler Resource Profile Upper Bound。
- UsageReporter 使用累计带 sequence 快照，流式结算幂等且不允许回退。
- 实际 consumed 可超过 total；超支必须被记录，不能拒绝现实账单。
- 耗尽后停止新普通 Job，并按 Policy cancel/finish-current/fail/wait-for-budget。

## 4. 前置门槛

### S10-G0：拆分 Runtime Kernel 事务编排

**状态**：In progress（2026-07-18）。公共入口、`KernelContext` 和显式 Router 已完成；内部实现仍集中在 `kernel_families.py`，尚未按 Command Family 完成物理拆分和完整 parity Gate。

1. `RuntimeKernel.handle` 保留唯一 Command/UoW/Receipt/Expected Version 入口。
2. 按 Run/Node、Graph、Durable Job/Timer、PlanPatch、HumanTask、Budget Command Family 拆分。
3. Handler 通过显式 KernelContext 共用同一 UoW，禁止嵌套事务。
4. Event 构造和 projection 更新按聚合收敛；无事件写例外必须登记。
5. 拆分前后 Event ID、Receipt、Replay、fault matrix、Memory/SQLite parity、Token 守恒不变。
6. 增加循环导入和 Repository 边界测试。

S10-G0 完成前不得加入 PlanPatch、HumanTask 或 Budget 命令。

## 5. 任务总览

| 任务 | 内容 | 参考投入 | 依赖 |
| --- | --- | ---: | --- |
| S10-G0 | 拆分 Runtime Kernel Command Family | 5 pd | Step 8 |
| S10-T01 | 固定 PlanVersion/PlanPatch/Policy Contract | 3 pd | G0、Step 9 |
| S10-T02 | 实现 Migration v7 与 Repository | 3 pd | T01 |
| S10-T03 | 实现 PlanPatch Schema 与语义 Validator | 3 pd | T01–T02 |
| S10-T04 | 实现 Policy Validator 和 Decision Facts | 3.5 pd | T01、T03 |
| S10-T05 | 实现 ExecutionPlanVersion Compiler/Store | 3 pd | T02–T04 |
| S10-T06 | 实现 Patch CAS、Commit 和幂等 | 3 pd | T05 |
| S10-T07 | 实现 Agentic Region Runtime | 3 pd | T05–T06、Step 8 |
| S10-T08 | 实现 Completion Proposal Validator | 2 pd | T04、T07 |
| S10-T09 | 固定最小 HumanTask Contract | 2.5 pd | T01 |
| S10-T10 | 实现 HumanTask Repository/Commands/Recovery | 3.5 pd | T02、T09 |
| S10-T11 | 实现 Approval Gate 与外部副作用阻断 | 2.5 pd | T04、T10 |
| S10-T12 | 实现 Budget Account/Reservation/Ledger | 4 pd | T01–T02 |
| S10-T13 | 接入 UsageReporter 与静态 Reservation | 3 pd | T12、Step 6 |
| S10-T14 | 实现 Budget Exhaustion/Human 增补闭环 | 3 pd | T10、T12–T13 |
| S10-T15 | Recovery、Diagnostics、故障、E2E 与冻结 | 4.5 pd | T01–T14 |

## 6. 详细任务

### Kernel 拆分交付细则（S10-G0）

先做无行为变化重构；建立 Command Router、KernelContext、Family Handler Protocol 和 source boundary tests。

**验收**：Step 1–9 全量测试与 Golden 不变；每条命令仍只有一个顶层事务；`kernel.py` 只保留入口、公共校验和分派。

### S10-T01：动态计划契约

定义 ExecutionPlanVersion、PlanPatch union、AgenticRegion、PolicyDecision、CompletionProposal、稳定 ID、状态机、Command/Event、Schema 和错误码。

**验收**：Patch 操作 exhaustive；pending-only 和 base version 字段不可省略；Canonical/Golden 通过。

### S10-T02：Migration v7

创建 `execution_plan_versions`（或增量扩展既有 plan store）、`plan_patches`、`policy_decisions`、`human_tasks`、`budget_accounts`、`budget_reservations`、`budget_ledger_entries`。

**验收**：Migration 1–6 不变；外键/唯一/CAS/扫描索引完整；Memory/SQLite parity。

### S10-T03：Patch Validator

验证 ID/引用、图闭合、可达性、循环上限、端口/Schema、Agentic Region、pending-only、节点/深度/迭代限制。

**验收**：任何非法 Patch 在写 Plan 前失败；错误带 JSON Path、rule 和稳定 code。

### S10-T04：Policy Validator

实现 Handler/Capability Allowlist、Artifact/Secret Permission、外部副作用、Completion Requirement 和预算规则；输出逐条 Decision Fact。

**验收**：纯函数、无 Repository/Clock/Agent；相同输入产生相同决策；deny 优先且 fail closed。

### S10-T05：Plan Compiler/Store

在 base Plan 上应用已验证 Patch，重用 Step 8 Graph 索引/Policy/Hash 规则，生成不可变 vN+1。

**验收**：不修改旧版本；相同 base+patch 生成相同 Plan Hash；新图通过完整静态验证。

### S10-T06：Patch Commit

实现 validate/approve/commit/reject Command、base version CAS、Proposal consumed、Budget Reservation 和 Event/Receipt 原子提交。

**验收**：并发只有一个版本胜者；重复 Command 返回原 PlanVersion；失败不留下半个 Decision/Reservation。

### S10-T07：Agentic Region Runtime

让 Kernel 调度 committed 动态节点，维护 source PlanVersion、pending/ready 边界和 Planner 再决策触发点。

**验收**：Planner 不能修改 ready/active/history；静态 Graph Token/Join/Completion 语义复用而非复制。

### S10-T08：Completion Proposal

验证必需 Terminal/Output/Artifact、活动责任、预算和 Policy，再允许完成 Run。

**验收**：Planner 的 finish 不是命令；缺产物、活动 Token/Job/Human/Timer 时不能成功。

### S10-T09：最小 HumanTask Contract

定义 approval/input、状态机、submission token、Expected Version、Actor、payload/form value、Command/Event、Deadline 预留字段和 Step 11 扩展边界。

**验收**：一个模型覆盖 Planner request、外部审批和预算追加；不存在 InteractionRequest 表。

### S10-T10：HumanTask Runtime

实现 create/approve/reject/provide_input/cancel、一次性 token、恢复扫描和 Run waiting reason。

**验收**：跨重启等待；重复提交只推进一次；非法 Actor/Version 不改变状态。

### S10-T11：Approval Gate

为外部写 Capability 绑定 approval scope、request hash 和过期/撤销状态；Job materialization 前 Kernel 强制检查。

**验收**：无批准时不创建副作用 Job；批准不能跨 Run/Node/Capability 重用。

### S10-T12：Budget Ledger

实现 Account、Reservation、Ledger Entry、reserve/report/settle/release/add-budget 的原子版本化命令。

**验收**：并发预留不超卖；实际超支仍入账；重复 Usage sequence 不重复消费；账本可从 Event 重建。

### S10-T13：Usage 与 Reservation

把 Step 6 UsageReporter 接入持久化 Ledger；用 Handler Upper Bound、Node Limit、Remaining Budget 最小值预留。

**验收**：无可计算上限默认拒绝；Planner/Agent/Tool/Unknown 使用同一 Run Budget；失败和取消释放剩余预留。

### S10-T14：耗尽闭环

停止新调度，按 Policy cancel 或允许收尾，进入 failed/waiting_for_budget；HumanTask 支持追加、拒绝或终止。

**验收**：运行中超支不会丢账；活动 Attempt 和新 Job 行为明确；追加预算后幂等恢复。

### S10-T15：收口

覆盖 Kernel 拆分 parity、Patch 并发、Policy 属性、Human race、Budget kill points/超支、Approval、Recovery、Planner MVP 和 Eval 回归。

**验收**：Step 1–9 全量回归；Agent 可完成开放任务且不能越权；M4 Completion Record 和 Stable Matrix 完成。

## 7. 执行批次

| 批次 | 任务 |
| --- | --- |
| A | G0、T01 |
| B | T02–T05、T09、T12 |
| C | T06–T08、T10–T11、T13 |
| D | T14 |
| E | T15 |

可并行：Policy 与 Human Contract；Plan Store 与 Budget Ledger；Agentic Runtime 与 Human API。不可绕过：G0；Patch Validator 先于 Commit；Budget reserve 先于计费 Job；Approval Event 先于副作用 Job。

## 8. 建议代码布局

```text
runtime/kernel.py                 # thin entry/router
runtime/commands/{run_graph,durable,plan,human,budget}.py
runtime/kernel_context.py
domain/{plan_patch,policy,human,budget}.py
policy/validator.py
planner/plan_compiler.py
persistence/{plans,human,budget}.py
application/{plan,human,budget}_service.py
```

## 9. 完成定义

1. Kernel 已拆分且唯一入口/单 UoW 不变量保持。
2. Patch 只能修改 Agentic Region pending 部分。
3. Policy、Budget、Approval 和 Plan commit 原子且可审计。
4. 并发 Patch 只有一个 PlanVersion 胜者。
5. Runtime 只执行 committed Plan。
6. Completion Proposal 不能绕过确定性完成条件。
7. HumanTask 跨重启、一次性提交且没有第二模型。
8. 外部副作用没有 Approval 不会创建 Job。
9. 预算预留、流式记账、Unknown、超支和追加闭环完整。
10. Replay 不调用 Planner/Policy外部服务。
11. Migration v7、故障矩阵、MVP E2E 和 Planner Eval 通过。
12. Step 11 可增量扩展 Human/Plan，不重写基础模型。

## 10. 主要风险与控制

| 风险 | 控制 |
| --- | --- |
| Kernel 拆分改变事务语义 | Golden/Receipt/fault parity Gate |
| Planner 通过 Patch 越过历史边界 | Agentic Region + pending-only + source plan version |
| Policy 与 Commit 间 TOCTOU | 同 UoW、输入 hash、base version CAS |
| 审批被跨范围复用 | scope/request hash/actor/version 绑定 |
| 预算只在规划时检查 | Kernel reserve + streaming ledger + exhaustion state |
| 实际费用超预留无法入账 | consumed 允许超过 total并触发耗尽 |
| Human 出现两套等待机制 | 单一 human_tasks 模型 |

## 11. 开工检查清单

1. 先完成 S10-G0。
2. 批准 Patch union、Agentic Region 和 PlanVersion CAS。
3. 批准 Policy rule registry 和 deny precedence。
4. 批准 HumanTask 最小状态机及 Step 11 扩展字段。
5. 批准 Budget Reservation 来源、Usage sequence 和超支语义。
6. 固定 Migration v7 和 Event/Command/fault matrix。
7. 固定 Agentic MVP 与 Planner Eval 通过阈值。

## 12. Delivery Record 与缺口（2026-07-18）

本节记录当前仓库事实，不代表 Step 10 已完成。

### 12.1 已交付映射

| 范围 | 实现证据 | 验证证据 |
| --- | --- | --- |
| 契约升级 | PlanPatch/Policy/Human/Budget Schema 与 Stability Matrix；ADR-002 记录 PlanPatch Draft → Stable | Frozen/Stable 契约测试与全量 Gate |
| Migration v7 | PlanPatch、PolicyDecision、单一 HumanTask、Budget Account/Reservation/Ledger | Migration 回归 |
| Patch 基础 | pending-only 校验、图闭合/可达/宽深限制、PlanVersion CAS、不可变版本和幂等 | 确定性、CAS、非法 Patch 测试 |
| Policy 基础 | deny precedence、缺事实 fail closed、Decision Facts | 独立 Policy 测试 |
| Human 基础 | 单一模型、一次性提交、Expected Version、竞态收敛 | 独立 Human race 测试 |
| Approval 基础 | 外部写在 Job materialization 前校验 run/node/capability/request hash 和有效期 | 精确 scope 拒绝/放行测试 |
| Budget 基础 | Account/Reservation/Ledger、累计 Usage sequence、并发 reserve、超支记账和 add-budget 幂等 | 独立 Budget 并发/重复/超支测试 |
| Kernel G0 基础 | `kernel.py` 薄入口、`KernelContext`、显式 Router | 既有 Runtime 回归 |

### 12.2 未完成项

1. S10-G0 尚未把 `kernel_families.py` 按 Run/Graph/Durable/Plan/Human/Budget 物理拆分，也没有完整的 Memory/SQLite Kernel parity 和每个 kill point 矩阵。
2. PolicyDecision、Budget Reservation、PlanVersion、Event、projection 和 Receipt 尚未被证明在所有 Patch 路径中由同一个 UoW 原子提交。
3. 静态 Reservation 与每个 Job/Planner Attempt 启动的强制绑定、取消/失败释放和 Unknown 结算仍缺完整 Kernel E2E。
4. Agentic Region 的持续 Planner 触发、动态节点恢复和 Completion Proposal 责任检查尚缺完整开放任务 E2E/Eval。
5. 外部写 Approval Gate 已在 materialization 边界强制执行，但静态入口节点如何先进入审批等待仍需补全运行时编排。
6. S10-T15 的完整故障注入、容量、Planner Eval 与发布冻结未完成。因此 Step 10 保持 `In progress`。
