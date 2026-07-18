# ADR-001：采用自研本地单机 Durable Kernel

| 属性 | 值 |
| --- | --- |
| 状态 | Accepted |
| 日期 | 2026-07-17 |
| 决策版本 | 1.0 |

## 背景

Agentic Workflow 需要不可变 WorkflowVersion、运行期 ExecutionPlan、动态 PlanPatch、事件回放、持久化 Timer、人工等待和 Artifact 血缘。当前产品的首要运行边界是本地单机、单项目、低部署成本和离线可用。

## 决策

第一阶段采用 Python + SQLite 自研本地单机 Durable Kernel。

本决策允许系统直接控制：

- WorkflowVersion 与动态 ExecutionPlan 的组合语义。
- 单机事务、Event、Snapshot、Job、Lease 和 DurableTimer。
- Planner Proposal、Policy、Budget 和 HumanTask 的持久化边界。
- 本地 Artifact 和项目级隔离。

## 范围限制

本分支不实现：

- 跨区域一致性。
- 多主数据库。
- 高吞吐分布式队列。
- 跨数据中心 Worker 调度。
- 对任意不可信第三方 Handler 的生产级沙箱。

## 放弃方案

在第一阶段不基于外部 Durable Execution 服务实现。原因是额外服务部署与本地离线目标冲突，并且运行中动态 ExecutionPlan 的表达仍需额外适配层。

## 风险

- Job、Lease、Timer、幂等和恢复需要自行实现与故障注入验证。
- 单机数据库限制吞吐和横向扩展。
- 长期事件演进需要 Upcaster、Golden Replay 和 Snapshot 版本策略。

## 重新评估条件

出现以下任一条件时重新执行 Build-vs-Buy ADR：

- 需要多节点或跨区域运行。
- 单机 Job、Timer 或 Event 吞吐达不到容量目标。
- 运维目标允许部署外部 Durable Execution 服务。
- 动态 ExecutionPlan 可以稳定映射到候选引擎。
- 自研恢复和故障注入成本超过产品可接受范围。
