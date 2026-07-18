# Agentic Workflow 步骤 9 任务拆分

| 文档属性 | 值 |
| --- | --- |
| 文档版本 | 1.0 |
| 状态 | Completed（2026-07-17） |
| 规划日期 | 2026-07-17 |
| 来源规划 | `agentic-workflow-implementation-plan.md` 1.0 |
| 输入基线 | Step 1–8 Completed；Static Graph Contract 1.2 Stable |
| 对应范围 | 步骤 9：Agent Planner 协议、Replay 与 Eval |
| 参考投入 | 5–8 person-weeks，约 34 person-days |

## 1. 阶段目标

把非确定性的模型调用封装成可持久化、可审计、可恢复的 Planner Job。Planner 只能根据已授权的 PlanningContext 提交结构化 ActionProposal，不能直接修改 Run、Plan、Job、Token、Value 或 Artifact。

```text
recorded PlanningContext
  -> durable Planner Attempt
  -> raw response persisted first
  -> parse and validate ActionProposal
  -> accepted/rejected Proposal fact
  -> Step 10 policy/patch boundary
```

Replay 只消费已记录事实，不调用模型、不重新解析原始响应，也不因代码或 Prompt 更新改变历史结论。

## 2. 范围边界

### 2.1 本阶段负责

- PlanningContext、PlannerAction、ActionProposal 和 Planner Attempt 契约。
- Planner Provider Port、模型适配器和版本化 Prompt/Capability Manifest。
- Durable Planner Job、Retry、Timeout、Unknown Result 和迟到响应隔离。
- 原始响应先持久化、Proposal 解析、Schema 校验、去重和审计事件。
- Migration v6：`planner_attempts`、`planner_proposals` 及必要索引。
- Planner Replay 纯函数语义、Recovery、Diagnostics 和费用/Token 统计接口。
- 固定 Eval 数据集、确定性验收器、基线报告和回归 Gate。

### 2.2 本阶段不负责

- 接受 PlanPatch、修改 ExecutionPlan 或创建动态节点；属于 Step 10。
- Policy Allowlist、预算强制执行、审批和 HumanTask；属于 Step 10。
- Foreach、Subflow、完整人工任务和动态并行；属于 Step 11。
- 生产 Provider 凭据管理、网络 Sandbox 和完整 Metrics 平台；属于 Step 12。
- 让 Planner 直接创建 BranchToken、Job、Timer 或 Artifact。

### 2.3 Step 10 移交接口

Step 9 输出不可变 Proposal Fact。`accepted` 只表示协议和 Schema 合法，不表示 Policy 已批准或计划已提交。Step 10 必须再次执行 Policy、预算、图闭合和 `base_plan_version` 校验。

## 3. 开工前固定的设计决策

### 3.1 Planner 是外部执行，不是 Reducer

- 每次模型调用都有独立 Planner Attempt、Job、Lease/Fence、Idempotency Key 和 Deadline。
- Planner 输出只有在原始响应成功持久化后才能解析。
- Replay、Snapshot 和 Recovery 不重新调用 Provider。
- Planner 超时后结果未知时进入 `unknown`；新 Attempt 使用新 ID，旧响应只能进入审计记录。

### 3.2 PlanningContext 是授权快照

- Context 固定保存 schema/version/hash、Run/Plan version、Goal、Graph Summary、Data Manifest、Capabilities、Remaining Limits 和相关 Event 摘要。
- Context 只包含 Value 摘要、Artifact Metadata/Capability，不内联未授权 Blob 或 Secret。
- 同一 Attempt 永远绑定同一 Context Hash、Prompt Hash、Model ID 和 Capability Manifest Hash。
- Context Builder 不读取模型历史对话作为隐式状态。

### 3.3 Proposal 两阶段语义

1. Protocol acceptance：JSON/Schema/引用/Proposal ID 合法。
2. Policy acceptance：留给 Step 10。

自由文本、Markdown 包裹 JSON、多动作混合、未知字段和未注册 Action 一律拒绝，不能“尽量解析”后执行。

### 3.4 Unknown 与费用

- Provider 未明确支持结果反查时，客户端 Idempotency Key 不能用于查询结果。
- Unknown Attempt 的已知使用量按事实记账；未知部分按预留值或保守上限记账。
- 自动恢复只能废弃旧 Attempt 并新建 Attempt，接受重复计费。
- 迟到响应保存为 `late_response_recorded`，不得产生 Proposal。

### 3.5 契约稳定性

- PlanningContext、PlannerAttempt、ActionProposal、Planner Event 在本阶段 Eval 完成前保持 Draft。
- Provider Port、Unknown 隔离、Replay 不调用模型、原始响应先落盘属于 Stable 语义。
- 本阶段仅允许一次受控 Schema 修订；冻结前重写开发期 Fixture。

## 4. 前置门槛

### S9-G0：批准 Planner Protocol 与 Eval Gate

**状态**：Completed（2026-07-17）。

必须确认：

1. Step 9 的 accepted Proposal 不等于 Step 10 的 approved Patch。
2. 原始响应先于解析结果持久化。
3. Unknown 通过新 Attempt 恢复，旧 Attempt 永不复活。
4. Replay 永不调用 Provider或重新解析。
5. Context 不携带 Secret 或未授权 Artifact 内容。
6. Eval 任务、验收器、模型/Prompt 固定规则和基线比较阈值在实现前可执行。
7. Migration v6 只创建 Planner 表，不提前创建 Step 10 Policy/Human/Budget 表。

## 5. 任务总览

| 任务 | 内容 | 参考投入 | 依赖 |
| --- | --- | ---: | --- |
| S9-T01 | 固定 Planner Contract、Schema、ID 和事件序列 | 3 pd | G0 |
| S9-T02 | 实现授权 PlanningContext Builder | 3 pd | T01、Step 8 |
| S9-T03 | 实现 Prompt/Capability/Model 版本绑定 | 2 pd | T01–T02 |
| S9-T04 | 定义 Provider Port 和 Fake/真实 Adapter | 3 pd | T01、Step 6 |
| S9-T05 | 实现 Durable Planner Job、Lease、Timeout | 3 pd | T03–T04、Step 5 |
| S9-T06 | 实现原始响应存储和结构化解析 | 2.5 pd | T01、T05 |
| S9-T07 | 实现 Proposal 校验、去重和两阶段状态 | 2.5 pd | T06 |
| S9-T08 | 实现 Retry、Unknown、迟到响应隔离与费用事实 | 3 pd | T05–T07 |
| S9-T09 | 实现 Migration v6、Repository 与 UoW | 2.5 pd | T01、T07 |
| S9-T10 | 实现 Planner Event Reducer、Replay 与 Recovery | 2.5 pd | T08–T09 |
| S9-T11 | 建立 Eval Harness、固定任务集和验收器 | 3 pd | T02–T07 |
| S9-T12 | 提供 Proposal/Attempt 查询和诊断 DTO | 1.5 pd | T09–T10 |
| S9-T13 | 故障注入、Eval 基线、E2E 与 Stable 冻结 | 2.5 pd | T01–T12 |

## 6. 详细任务

### S9-T01：Planner Contract

**状态**：Completed（2026-07-17）。

定义 PlanningContext、PlannerAction union、ActionProposal、PlannerAttempt、RawResponseRef、ValidationResult、状态机、Command/Event、错误码、Schema、Golden 和稳定 ID。

**验收**：所有字段可 Canonical JSON；Action 使用 `oneOf + discriminator`；未知字段 fail closed；事件序列能区分 requested/started/received/parsed/accepted/rejected/unknown/late。

### S9-T02：PlanningContext Builder

**状态**：Completed（2026-07-17）。

从 Graph Summary、Data Manifest、Capability Catalog 和限制事实构建不可变 Context；定义截断、排序、大小上限和授权过滤。

**验收**：相同事实生成相同 Hash；Context 不含 Secret 值、Blob 内容或 Repository 对象；超限返回稳定诊断。

### S9-T03：版本绑定

**状态**：Completed（2026-07-17）。

为 Prompt Template、System Instruction、Capability Manifest、Model、Provider 和 Context Builder 建版本/Hash，并记录在 Attempt。

**验收**：任一组成变化都会改变 Planner Request Fingerprint；历史 Attempt 可解释当时使用的完整配置。

### S9-T04：Provider Port

**状态**：Completed（2026-07-17）。

定义 start/cancel/result/usage 接口、结构化输出能力和 Provider Error 分类；提供确定性 Fake Adapter 和至少一个受信 Adapter。

**验收**：Adapter 不访问 Runtime Repository；取消按 execution_ref 隔离；输出、stderr/diagnostic 和用量有硬上限。

### S9-T05：Durable Planner Job

**状态**：Completed（2026-07-17）。

复用 Job/Lease/Fence/DurableTimer，增加 Planner Job 类型、Deadline、续租和 crash-safe 状态转换。

**验收**：同一 Attempt 只有一个有效 Fence；进程在调用前后终止不会重复提交 Proposal；恢复只调度未终结 Job。

### S9-T06：Raw Response 与 Parser

**状态**：Completed（2026-07-17）。

先保存原始响应 checksum/size/provider request ID，再执行严格 JSON 解析和 ActionProposal Schema 校验。

**验收**：数据库提交失败时不解析；自由文本和宽松 JSON 被拒绝；原始响应受大小、脱敏和访问权限约束。

### S9-T07：Proposal 状态与去重

**状态**：Completed（2026-07-17）。

实现 parsed、protocol_accepted、protocol_rejected、consumed 状态；Proposal ID 和 content hash 去重；明确与 Step 10 Policy 状态分离。

**验收**：相同 Proposal 重复提交只保留一份有效事实；相同 ID 不同内容为 Integrity Violation。

### S9-T08：Retry 与 Unknown

**状态**：Completed（2026-07-17）。

实现 Provider 分类 Retry、Backoff、Unknown、费用保守结算、新 Attempt、迟到响应审计和最大决策次数。

**验收**：Unknown 不自动接受迟到 Proposal；新 Attempt 与旧 Attempt 隔离；重复费用可查；Retry 上限后产生 escalation fact。

### S9-T09：Migration v6

**状态**：Completed（2026-07-17）。

创建 `planner_attempts`、`planner_proposals` 和必要索引。Step 9 将 Provider Structured Output 硬限制为 1 MiB，并在 `planner_attempts` 保存原文与 checksum；超过上限的响应在进入持久化协议前拒绝。若 Step 12 容量数据证明需要接收更大响应，再通过新 Migration 和 Planner 专用 Artifact producer 扩展，不能借用业务 Attempt 身份。

**验收**：Migration 1–5 不变；Event/Attempt/Proposal/Receipt 同事务；Memory/SQLite adapter parity。

### S9-T10：Replay 与 Recovery

**状态**：Completed（2026-07-17）。

实现 Planner Reducer、Snapshot state、unfinished scan 和恢复命令；已记录 parsed/accepted fact 不重新解析。

**验收**：Replay Guard 主动阻断 Provider、Clock、Network 和 Parser；重复恢复不创建重复 Attempt/Proposal。

### S9-T11：Eval Harness

**状态**：Completed（2026-07-17）。

固定任务、Capabilities、输入 Artifact、模型/Prompt、预算和确定性验收器；输出成功率、无效/拒绝/重复、决策数、Token、费用、耗时、人工率和无进展率。

**验收**：同一录制响应离线回放结果一致；基线可版本化比较；指标有明确分母和失败阈值。

### S9-T12：Application/Diagnostics

**状态**：Completed（2026-07-17）。

提供 Attempt、Proposal、Context Hash、模型/Prompt、费用、Unknown、Reject Reason 和“为什么等待 Planner”查询。

**验收**：API DTO 不暴露 Raw Secret；调用方不能通过 Query 层改变状态。

### S9-T13：测试与冻结

**状态**：Completed（2026-07-17）。

覆盖 Contract/Golden、Context 授权、Parser 负例、Provider Fake、kill points、Unknown/late race、Replay guard、Migration、Eval 和开放式任务 E2E。

**验收**：Step 1–8 全量回归；Eval 基线获批准；最终 Schema/事件/Provider Port 标记 Stable，ActionProposal 保持 Step 10 可消费的稳定版本。

## 7. 执行批次

| 批次 | 任务 | 结果 |
| --- | --- | --- |
| A | G0、T01–T03 | 契约、Context 和版本指纹 |
| B | T04–T06、T09 前半 | Provider、Durable Job、Raw Response、Migration |
| C | T07–T10 | Proposal、Unknown、Replay、Recovery |
| D | T11–T12 | Eval、查询与诊断 |
| E | T13 | 故障、E2E、基线和冻结 |

可并行：T02 与 T04 Fake Adapter；T09 Migration 与 T11 Eval 数据集；T12 DTO 与 T13 测试基建。不可并行：Raw Response 持久化必须先于 Parser；Unknown 语义固定前不得接真实 Provider。

## 8. 建议代码布局

```text
src/orbit/workflow/
├── domain/planner.py
├── planner/context.py
├── planner/protocol.py
├── planner/eval.py
├── handlers/planner_provider.py
├── runtime/planner_commands.py
├── runtime/planner_recovery.py
├── persistence/planner.py
└── application/planner_service.py
```

## 9. 完成定义

1. Planner 只能产生结构化 Proposal Fact，不能写 Runtime projection。
2. Context 授权、排序、截断和 Hash 确定性可验证。
3. 原始响应总是先于解析结果持久化。
4. Replay/Recovery 不调用 Provider或 Parser。
5. Unknown、Retry、迟到结果和费用语义完整闭环。
6. Proposal 去重不依赖模型或 Provider 幂等能力。
7. Migration v6、Memory/SQLite parity 和 kill-point matrix 通过。
8. Eval 有固定数据集、确定性验收器、基线与回归阈值。
9. Step 10 可以仅凭 Proposal/Context/PlanVersion 执行 Policy，不读取模型对话。
10. Completion Record、Stable Matrix、实际投入和 Step 10 移交完成。

## 10. 主要风险与控制

| 风险 | 控制 |
| --- | --- |
| Replay 重调模型 | 已记录 Event + 外部调用 Guard |
| 自由文本被宽松解析 | 严格 JSON Schema，禁止修复式 Parser |
| Secret/Blob 进入 Context | Capability 过滤、摘要和大小上限 |
| Unknown 被当失败重试覆盖 | 原 Attempt 终态 + 新 Attempt + late audit |
| Eval 只测格式不测任务质量 | 固定开放任务 + 确定性产物验收器 |
| Prompt/模型漂移不可追溯 | 全组成版本/Hash 绑定 Attempt |
| Planner 成本无限增长 | 决策/Attempt 上限和 Usage 事实；强制预算由 Step 10 接管 |

## 11. 开工检查清单

1. 批准 Action union、Proposal Schema 和事件序列。
2. 固定 Context 授权字段和大小上限。
3. 固定 Provider Port、Unknown 与 late response 语义。
4. 固定 Migration v6 和 Raw Response 存储策略。
5. 建立 Fake Provider、Replay Guard 和 kill-point harness。
6. 在接真实模型前批准 Eval 数据集和验收器。
7. 确认 Step 10 是 Proposal 的唯一消费/执行边界。

## 12. Completion Record

**完成日期**：2026-07-17  
**结论**：S9-G0 与 S9-T01–T13 全部完成。Planner 的非确定性调用已转换为持久化 Attempt/Proposal/Event 事实，Step 10 可以在不读取模型会话的情况下消费 protocol-accepted Proposal。

### 12.1 已交付

- Planner 1.0 Domain：PlanningContext、七种 PlannerAction、ActionProposal、PlannerAttempt/Proposal 状态、稳定 ID、Schema、Golden 和错误码。
- 授权 Context Builder：Graph Summary/Data Manifest/Capabilities/Limits/Event 摘要白名单、稳定排序、Hash 和 256 KiB 上限。
- Provider Port：Fake Adapter、受信 Callable Adapter、取消接口、Provider Response/Usage 和错误分类。
- Durable Planner：request/claim/renew/execute、Lease Token/Fence/Deadline、Raw-first 两事务、严格 JSON Parser、Proposal 去重。
- Unknown/Retry：新 Attempt、迟到响应隔离、保守 Usage、Transient Retry、耗尽 escalation fact。
- Migration v6：`planner_attempts`、`planner_proposals`、索引、SQLite/Memory Repository 和 UoW。
- Replay/Recovery：Planner Event Catalog、RunView Reducer、过期 Lease -> Unknown、已落盘 Response 独立解析，恢复期间不调用 Provider。
- Eval/Diagnostics：固定 Eval Fixture、确定性 Harness、成功/无效/Policy/重复/决策/Token/费用/耗时/人工/无进展指标和查询 DTO。

### 12.2 实现决策与规划偏差

- Planner 调用使用专用 `planner_attempts` Durable Lease/Fence，而不是复用要求 `node_run_id` 外键的业务 `jobs` 表。两者复用同一 SQLite UoW、Event Store、Lease authority、Expected Version、Recovery 和 fault 语义；该选择避免为了调用 Planner 伪造业务 NodeRun。
- Raw Response 使用受限内联存储而不是 Step 7 Artifact：Structured Output 最大 1 MiB，先提交原文/checksum再解析。更大响应被协议拒绝；未来若开放必须新增 Planner Artifact producer 和容量验证。
- Lease Renewal 延续 Step 5 已批准的无事件 projection 例外，不提升 aggregate version；其他 Planner 状态变化全部有 Event。

### 12.3 Stable Matrix

以下契约标记 Stable：

- `planning_context`
- `planner_action`
- `action_proposal` / `action_proposal_v1`
- `planner_attempt`
- `planner_provider_port`
- `planner_unknown_replay_semantics`

PlanPatch、Agentic Region、Cost Estimation 和 Budget Exhaustion Policy 继续保持 Draft，由 Step 10 固定。

### 12.4 验证与移交

- 覆盖完整状态转换矩阵、Schema/Golden、Context 授权、严格 Parser、Raw-first kill point、claim rollback、Lease Renewal、Unknown/late、Transient Retry/escalation、Recovery、Replay side-effect guard、Migration v6、Memory/SQLite parity 和 Eval 基线。
- Step 10 只消费 `protocol_accepted` Proposal，并负责 Policy、Budget、Approval、PlanPatch CAS 和 `consumed` 状态；Step 9 的 accepted 不表示获准执行。
- Planner Provider、Parser 和 Eval 不得进入 Reducer；Step 10 Replay 继续只消费记录事实。
