# Agentic Workflow 步骤 11 任务拆分

| 文档属性 | 值 |
| --- | --- |
| 文档版本 | 1.0 |
| 状态 | In progress（发布 Gate 修复中，2026-07-18） |
| 规划日期 | 2026-07-17 |
| 来源规划 | `agentic-workflow-implementation-plan.md` 1.0 |
| 输入基线 | Step 1–9 Completed；Step 10 当前为 In progress，依赖项须逐项满足 |
| 对应范围 | 步骤 11：完整 Human、Foreach、Subflow、动态并行 |
| 参考投入 | 8–12 person-weeks，约 48 person-days（任务表合计 47.5 pd） |

## 1. 阶段目标

把 Step 10 的串行 Agentic MVP 扩展为通用工作流：完整人工协作、集合并行、子流程复用和受限动态 DAG，同时复用既有 BranchToken、Join、Budget、Approval、PlanVersion 和 Completion 语义。

## 2. 范围边界

### 2.1 本阶段负责

- 在同一 `human_tasks` 模型上增加 Assignee、Role、Form、Deadline、Reminder、Escalation、Delegation、多人会签和撤回。
- ForeachGroup/Item、Item Scope、并发限制、单项 Retry、失败策略和确定性聚合。
- Subflow 固定版本、父子 Run、输入输出 Mapping、取消/失败传播和 Artifact Visibility。
- PlanPatch 一次创建受限小型动态 DAG；pending-only 修订和动态 Join。
- Migration v8、Recovery、Diagnostics、Eval、故障注入和通用 Workflow E2E。

### 2.2 本阶段不负责

- 跨区域分布式协调或跨数据库 Join。
- 无限集合、无限递归、无界动态 DAG 或运行时自修改 active 节点。
- 企业级身份目录、完整 RBAC、Sandbox 和生产告警；属于 Step 12。
- 自定义脚本直接访问 Runtime Repository。

## 3. 固定设计决策

### 3.1 HumanTask 是唯一人工等待模型

- Step 10 表原地增量扩展，不创建 v2 表或 InteractionRequest。
- HumanTask 与 Approval Gate 通过 kind/policy 区分，共享状态机、submission token、Expected Version 和审计。
- 多人会签的参与者集合在任务激活时冻结；变更必须产生新 revision/event。

### 3.2 Foreach 确定性

- Item Key 从输入顺序/显式 key 派生，必须唯一且稳定。
- Item ID 绑定 Run、Group、key、source checksum 和 PlanVersion。
- 并发完成顺序不影响聚合；输出按原输入 index/key 排序。
- Fail-fast 只停止尚未开始的 Item；迟到结果受 Lease/Fence 处理。
- Item Scope 隔离 Value/Artifact/Secret、预算、Retry 和事件关联。

### 3.3 Subflow 边界

- 子 Run 启动时绑定固定 WorkflowVersion 和初始 PlanVersion。
- Parent/Child 通过显式 correlation 和 SubflowLink 连接，不共享隐式 Repository 状态。
- 父子输入输出必须经过 Mapping/Schema/Artifact Visibility。
- 取消传播方向、失败映射和递归深度由 Policy 明确声明。

### 3.4 动态 DAG

- 一个 Patch 可以增加多个节点/边，但必须满足节点数、宽度、深度、并发和迭代硬上限。
- Patch 内 DAG 先完整验证再原子提交；不能逐节点暴露半成品。
- 动态图复用 Step 8 Token/Join/Completion，不建立另一套 Planner Graph Runtime。
- 已 ready/active/history 部分不可修改。

## 4. 前置门槛

### S11-G0：批准 Scope、传播和动态 DAG 上限

**状态**：In progress（2026-07-18）。核心契约和上限已有实现，但传播矩阵、分页运行时和故障 Gate 尚未闭合。

1. 冻结 HumanTask 扩展状态/角色/会签语义。
2. 冻结 Foreach Item identity、顺序、失败和预算 scope。
3. 冻结 Subflow version/correlation/cancel/failure/artifact 规则。
4. 冻结动态 DAG 节点、宽度、深度、并发和 Patch 大小上限。
5. 确认所有新控制结构复用 Kernel 唯一入口和 Step 8/10 事实。
6. Migration v8 只增量扩展 v7，不复制 Human/Plan/Budget 表。

## 5. 任务总览

| 任务 | 内容 | 参考投入 | 依赖 |
| --- | --- | ---: | --- |
| S11-T01 | 固定 Human/Foreach/Subflow/Dynamic DAG Contract | 4 pd | G0 |
| S11-T02 | 实现 Migration v8 与 Repository | 3.5 pd | T01 |
| S11-T03 | 扩展 Human Assignment/Role/Form/Permission | 3 pd | T01–T02 |
| S11-T04 | 实现 Human Deadline/Reminder/Escalation | 3 pd | T03、Step 5 |
| S11-T05 | 实现会签、委派、撤回与竞态 | 3.5 pd | T03–T04 |
| S11-T06 | 实现 Foreach Compiler、Group/Item Scope | 3.5 pd | T01–T02 |
| S11-T07 | 实现 Foreach Scheduler 与并发上限 | 3 pd | T06、Step 5 |
| S11-T08 | 实现 Item Retry/Failure/Budget Policy | 3 pd | T06–T07、Step 10 |
| S11-T09 | 实现确定性 Foreach Aggregate | 2.5 pd | T07–T08、Step 7/8 |
| S11-T10 | 实现 Subflow Link、固定版本和 Mapping | 3.5 pd | T01–T02 |
| S11-T11 | 实现父子失败/取消/恢复传播 | 3 pd | T10 |
| S11-T12 | 实现 Artifact Visibility 和递归限制 | 2.5 pd | T10–T11、Step 7 |
| S11-T13 | 实现多节点 Dynamic DAG PlanPatch | 3.5 pd | T01、Step 10 |
| S11-T14 | 实现动态 Join/Completion/Planner Eval | 2.5 pd | T09、T13 |
| S11-T15 | Diagnostics、故障、容量、E2E 与冻结 | 3.5 pd | T01–T14 |

## 6. 详细任务

### S11-T01：Contract

定义完整 HumanTask、Assignment、Form、Reminder/Escalation、Approval Quorum、ForeachGroup/Item、ItemScope、SubflowLink、PropagationPolicy、DynamicDagLimits、Command/Event/Schema/ID。

**验收**：所有集合有稳定排序；所有传播方向和终态明确；无隐式共享状态或无界字段。

### S11-T02：Migration v8

增量扩展 `human_tasks`；创建 `human_task_participants`、`foreach_groups`、`foreach_items`、`subflow_links` 及必要索引；扩展 PlanPatch 支持 multi-node DAG。

**验收**：Migration 1–7 不变；人任务仍是同一主表；Memory/SQLite parity；大 Group 扫描分页。

### S11-T03：Human Assignment/Form

实现 Assignee/Role/Permission、结构化 Form Schema、校验、Actor capability 和 claim/release。

**验收**：未授权 Actor 无法读取敏感表单或提交；Form 错误带路径；任务跨重启保持 owner/revision。

### S11-T04：Deadline/Reminder/Escalation

统一使用 DurableTimer；实现 deadline、重复 reminder 去重和 escalation policy。

**验收**：Timer fire 与人工提交只有一个事务胜者；重复 fire/reminder 不重复通知或推进。

### S11-T05：会签/委派/撤回

实现 all/any/n_of_m quorum、稳定参与者、delegation chain、withdraw 和 revision。

**验收**：提交顺序不改变 quorum；同一参与者只计一次；委派不扩大权限；完成后迟到提交被拒绝。

### S11-T06：Foreach Contract/Compiler

从已提交 Value 生成 Group/Item、稳定 key/index、Item Scope 和静态 body Plan。

**验收**：相同输入 checksum 生成相同 Item ID；重复 key、超限和不可迭代输入在创建前失败。

### S11-T07：Foreach Scheduler

实现分页 materialization、并发 slot、ready queue、公平推进和 recovery continuation。

**验收**：任何时刻 active Item 不超过上限；进程重启不丢 slot、不重复 Item；大集合不一次物化到内存。

### S11-T08：Item Policy/Budget

实现单项 Retry、fail-fast/continue/partial-success、Item Budget Reservation 和 Unknown。

**验收**：失败只影响声明 scope；fail-fast 不覆盖已提交结果；Group/Run 预算一致且不超卖。

### S11-T09：Aggregate

按输入 index/key 稳定聚合结果、错误摘要和 Artifact refs，复用 Join/Input Assembly/Lineage。

**验收**：完成顺序全排列得到相同 checksum；partial result 明确标记；Secret 不被展开。

### S11-T10：Subflow Link

实现固定子 WorkflowVersion、父子 Mapping、Correlation、Plan source 和启动原子性。

**验收**：父事件与 child Run/link 同事务或可恢复 outbox；子版本不会随最新版本漂移。

### S11-T11：传播与恢复

实现 parent cancel、child fail/succeed/unknown、等待和恢复扫描；传播命令幂等。

**验收**：重复传播不重复终结；父取消与子完成竞态有稳定胜者；孤立 link 可诊断/修复。

### S11-T12：Artifact/递归边界

实现父子 Artifact Visibility/capability transfer、Secret scope、最大嵌套深度和递归 Workflow Policy。

**验收**：未声明 Artifact 不可见；capability 最小化；深度/递归在启动子 Run 前拒绝。

### S11-T13：Dynamic DAG Patch

扩展 Patch union，一次提交多个节点/边/Join；验证 DAG、Limits、Capability、Budget 和 pending-only。

**验收**：半个 DAG 永不执行；Patch 原子；动态 Token ID/PlanVersion 稳定；不能修改 ready 节点。

### S11-T14：动态 Join/Eval

复用静态 Join/Completion，建立 Planner 动态并行、失败恢复和 Plan Revision Eval。

**验收**：动态完成顺序不改变结果；Planner 不生成无界宽度/深度；Eval 有成功/成本/无效 DAG 指标。

### S11-T15：收口

覆盖 Human race、Timer、Foreach 排列/容量、Subflow cancel/failure、Artifact ACL、Dynamic DAG 并发、Recovery 和端到端场景。

**验收**：Step 1–10 全量回归；Migration v8/故障矩阵/容量目标通过；Completion Record 和 Stable Matrix 完成。

## 7. 执行批次

| 批次 | 任务 |
| --- | --- |
| A | G0、T01–T02 |
| B | T03、T06、T10、T13 前半 |
| C | T04–T05、T07–T09、T11–T12、T13 后半 |
| D | T14 |
| E | T15 |

四条主线 Human、Foreach、Subflow、Dynamic DAG 可在 Contract/Migration 后并行；T14 必须等待 Foreach 聚合与 Dynamic Patch；T15 统一做跨结构组合故障。

## 8. 建议代码布局

```text
domain/{human,foreach,subflow,dynamic_plan}.py
runtime/commands/{human,foreach,subflow}.py
runtime/{foreach_scheduler,subflow_recovery}.py
persistence/{human,foreach,subflow}.py
application/{human,foreach,subflow}_service.py
planner/dynamic_dag_validator.py
```

## 9. 完成定义

1. HumanTask 单模型支持完整人工生命周期和确定性会签。
2. 所有人工提交具备身份、权限、一次性 token 和 Expected Version。
3. Foreach Item identity/scope/并发/失败/预算可恢复。
4. Foreach 聚合与完成顺序无关。
5. Subflow 固定版本、Mapping、Correlation、取消/失败传播完整。
6. Artifact/Secret 不跨越声明边界。
7. 动态 DAG 原子提交、有硬上限且只改 pending。
8. 动态图复用 Token/Join/Completion，无第二 Runtime。
9. Migration v8、故障、容量、Eval 和通用 E2E 通过。
10. Step 12 可基于统一事实增加安全、观测和产品接口。

## 10. 主要风险与控制

| 风险 | 控制 |
| --- | --- |
| 人工模型分裂 | 同表增量、同状态机和命令入口 |
| Foreach 完成顺序污染聚合 | 稳定 index/key + permutation tests |
| 大集合耗尽内存 | 分页 materialization/scan/aggregate |
| 父子取消形成双终态 | Expected Version + idempotent propagation |
| Artifact 跨子流程泄漏 | 显式 visibility/capability transfer |
| 动态 DAG 绕过静态限制 | 完整 Patch validator + hard limits |
| Planner 制造并发爆炸 | width/concurrency/budget policy |

## 11. 开工检查清单

1. 批准 Human 扩展状态机和 quorum。
2. 批准 Foreach Item identity、scope、failure 和 aggregate。
3. 批准 Subflow version、correlation 和传播矩阵。
4. 批准 Artifact/Secret transfer 和递归策略。
5. 批准 Dynamic DAG limits 和 Patch atomicity。
6. 固定 Migration v8、fault points 和容量目标。

## 12. Delivery Record 与缺口（2026-07-18）

本节记录当前仓库事实，不代表 Step 11 已完成。

### 12.1 已交付映射

| 范围 | 实现证据 | 验证证据 |
| --- | --- | --- |
| Migration v8 | 原地扩展 `human_tasks`；增加 participant、Foreach Group/Item、Subflow Link | Migration 回归 |
| Human 基础扩展 | Assignment/Form/Deadline、稳定参与者、any/all/n-of-m quorum | 会签顺序与提交竞态测试 |
| Foreach 基础 | source checksum、PlanVersion、key/index 稳定 ID；并发槽、fail-fast、稳定聚合 | identity、重复 key、并发、fail-fast 测试 |
| Subflow 基础 | 固定 WorkflowVersion、显式 Link/Mapping、递归限制 | 版本固定与递归拒绝测试 |
| Dynamic DAG 基础 | multi-node/multi-edge Patch 静态原子校验与循环拒绝 | DAG cycle 测试 |

### 12.2 未完成项

1. HumanTask 尚缺完整 Role/Permission、delegation、withdraw、reminder/escalation DurableTimer 和敏感 Form 读取授权闭环。
2. Foreach 尚未实现真正分页 materialization；当前创建路径仍会物化输入集合。Item Retry、Budget/Unknown、恢复 continuation 和大集合容量 Gate 未完成。
3. Subflow 的父事件/child Run/link 原子启动或 outbox、完整取消/失败竞态、孤立 link 修复、Artifact/Secret capability transfer 未完成。
4. Dynamic DAG 尚缺 Kernel 级执行 E2E、动态 Join/Completion 全排列和 Planner 动态并行 Eval。
5. S11-T15 的 fault matrix、容量与跨 Human/Foreach/Subflow/DAG 组合场景未完成。因此 Step 11 保持 `In progress`。
