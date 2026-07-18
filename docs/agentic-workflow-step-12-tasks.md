# Agentic Workflow 步骤 12 任务拆分

| 文档属性 | 值 |
| --- | --- |
| 文档版本 | 1.0 |
| 状态 | In progress（发布 Gate 修复中，2026-07-18） |
| 规划日期 | 2026-07-17 |
| 来源规划 | `agentic-workflow-implementation-plan.md` 1.0 |
| 输入基线 | Step 1–9 Completed；Step 10–11 当前为 In progress，依赖项须逐项满足 |
| 对应范围 | 步骤 12：恢复、安全、性能、可观测性与产品接口 |
| 参考投入 | 8–14 person-weeks，约 55 person-days |

## 1. 阶段目标

将功能完整的单机 Workflow Runtime 收敛为可长期运行、可诊断、可保护、可容量规划并可通过稳定 API/UI 使用的产品。该阶段不改变核心状态语义，而是验证并强化恢复、安全、性能和运维边界。

## 2. 范围边界

### 2.1 本阶段负责

- 启动恢复、Dry-run/Apply Recovery、孤立事实检测和 Unknown 人工接管。
- Secret、Capability、Artifact ACL、外部副作用、文件/网络和 Script Sandbox。
- Run/Node/Attempt/Planner/Human/Token/Budget 的日志、指标、Trace 和诊断。
- 单机容量目标、Benchmark、Snapshot/Event/Queue/Timer/Foreach/Artifact 调优。
- Workflow/Run/Plan/Proposal/Human/Artifact API 和统一 Run View Model。
- Timeline/Graph/Data/Errors 共用 Event-derived 状态。
- 运维手册、升级/备份/恢复和发布 Gate。

### 2.2 本阶段不负责

- 跨区域高可用、共识、分布式事务或多租户 SaaS 控制平面。
- 新的 Workflow 控制结构或 Planner Action。
- 未经 ADR 的 Event 删除、历史压缩或破坏 Frozen 契约。
- 用历史估算替代 Handler 静态硬上限；动态估算只能进一步收紧或改善预留精度。

## 3. 固定设计决策

### 3.1 Recovery 只提交 Command

- Scanner/Repair 不直接写 projection；输出诊断或提交 system Command。
- Replay 只消费 Event/Snapshot，不调用 Handler、Planner、Policy外部服务或 Human。
- Apply 模式的每项修复有 idempotency key、Expected Version、actor 和审计事件。
- 无法自动证明安全的修复进入人工任务，不猜测外部结果。

### 3.2 安全默认拒绝

- Handler/Tool/Planner 使用显式 Capability；无声明即拒绝。
- Secret 只通过 Resolver 和 scoped reference；不进入 Event、日志、Prompt、Error details 或 Artifact metadata。
- Artifact 访问使用 capability/ACL，默认 run/node/item/subflow scope。
- 外部写、支付、生产变更必须有匹配 Approval Fact。

### 3.3 Observability 不成为状态源

- Log/Metric/Trace 是派生遥测，不能驱动状态转换。
- UI/API View Model 来自 Event/projection，不能从日志猜状态。
- Correlation 跨 Run、Node、Attempt、Planner、Human、Foreach、Subflow 保持 root chain。

### 3.4 性能优化受正确性约束

- 先声明硬件、规模、P95/P99/恢复目标，再 Benchmark。
- Snapshot、索引、批量和缓存不得改变 Event/Receipt/Plan Hash 或确定性顺序。
- 动态 Reservation Estimator 不得突破 Handler/Policy 硬上限。

### 3.5 Migration v9 条件

Step 12 不为凑版本强制建表。只有 Audit/API Idempotency/Security Policy/Metric Rollup 等确需持久化的新 Stable 契约获批后才创建 Migration v9；否则沿用 v1–v8 和外部遥测存储。

## 4. 前置门槛

### S12-G0：批准 SLO、安全威胁模型和发布范围

**状态**：In progress（2026-07-18）。已固定本地目标和接口边界；生产威胁模型、完整 SLO/演练及发布阻断项仍未全部通过。

1. 固定目标硬件和单机规模：活跃 Run、事件、Job/Timer、Foreach Item、Artifact 容量。
2. 固定启动恢复、命令、Queue、Timer、Replay 的 P95/P99/SLO。
3. 完成数据流/信任边界/威胁模型和 Capability Registry。
4. 固定 API actor/idempotency/version/error envelope。
5. 明确 UI 是只读 View + Command 提交，不直写数据库。
6. 决定是否需要 Migration v9及其唯一范围。
7. 批准发布阻断级安全、恢复和容量测试。

## 5. 任务总览

| 任务 | 内容 | 参考投入 | 依赖 |
| --- | --- | ---: | --- |
| S12-T01 | 固定 SLO、威胁模型、API 和运维 Contract | 4 pd | G0 |
| S12-T02 | 实现启动 Recovery Manager | 4 pd | T01、Step 3–11 |
| S12-T03 | 实现 Integrity/Repair Dry-run 与 Apply | 3.5 pd | T02 |
| S12-T04 | 实现 Unknown/孤立事实人工接管 | 3 pd | T02–T03、Step 10/11 |
| S12-T05 | 实现 Capability/Secret/Redaction 边界 | 4 pd | T01、Step 6/7 |
| S12-T06 | 实现 Artifact ACL 与 Human Identity/RBAC | 3.5 pd | T05、Step 11 |
| S12-T07 | 实现 Script/File/Network Sandbox | 5 pd | T05 |
| S12-T08 | 实现结构化日志、Trace 和 Metrics | 4 pd | T01 |
| S12-T09 | 实现 Why-waiting/Lineage/Policy 诊断 | 3 pd | T08、Step 8–11 |
| S12-T10 | 建立容量 Harness 和基线 | 4 pd | T01 |
| S12-T11 | 调优 Event/Snapshot/Queue/Timer/Foreach/Data | 4 pd | T10 |
| S12-T12 | 评估动态 Reservation Estimator | 2.5 pd | T10–T11、Step 10 |
| S12-T13 | 实现版本化 HTTP API | 4 pd | T01、T02–T09 |
| S12-T14 | 实现统一 Run View Model 与 UI 接口 | 3 pd | T09、T13 |
| S12-T15 | 发布故障演练、安全测试、手册与冻结 | 3.5 pd | T01–T14 |

## 6. 详细任务

### S12-T01：生产契约

定义 RecoveryReport/RepairAction、Capability、AuditRecord、API Error/Command Envelope、RunView、SLO 和 Metric 名称/单位/标签预算。

**验收**：所有发布 Gate 有量化阈值；高基数标签受控；API mutation 全部映射现有 Command。

### S12-T02：Recovery Manager

启动分页扫描未终结 Run、过期 Lease/Timer、缺 Job、可推进 Token/Join/Human/Foreach/Subflow/Plan，并提交幂等 system Command。

**验收**：任意 Attempt 阶段 kill 后可恢复；扫描有 cursor/limit/deadline；Replay 不外调。

### S12-T03：Integrity/Repair

整合 Event gap/hash、Snapshot、projection、Blob、Token、Join、Budget、Plan、Human、Foreach 和 Subflow 检查；提供 dry-run 与显式 apply。

**验收**：默认不修改；每项修复可审计/重放/回滚或明确不可回滚；大库流式扫描。

### S12-T04：人工接管

把 Unknown External/Planner、无法证明的孤立状态和预算/审批阻塞转换为有 scope 的 HumanTask；保存处置依据。

**验收**：人工不能伪造外部成功覆盖已有事实；处置产生新 Attempt/补偿/终止等合法 Command。

### S12-T05：Capability/Secret

实现 Capability issuance/delegation/revocation、Secret Resolver、日志/Prompt/Error/Artifact metadata 全路径脱敏和 fail-closed tests。

**验收**：Secret 值扫描覆盖成功/失败/异常/CLI stderr/Planner Raw Response；泄漏测试阻断发布。

### S12-T06：ACL/RBAC

实现 Artifact ACL、Human identity、role/permission、审批 scope 和审计查询。

**验收**：跨 Run/Item/Subflow 访问被拒；权限撤销后的新访问立即失败；历史审计保留 Actor。

### S12-T07：Sandbox

实现 Script/CLI 文件系统、网络、进程、CPU、内存、输出和时长策略；定义可信第一方 Handler 例外。

**验收**：逃逸、路径穿越、symlink、网络绕过、fork bomb 和输出炸弹测试；超限被取消并保留诊断。

### S12-T08：Observability

建立 root correlation trace、结构化日志和 Queue/Lease/Retry/Rework/Planner/Human/Budget/Timer/Artifact 指标。

**验收**：遥测失败不改变 Runtime；敏感字段脱敏；指标标签不会按 ID 无限增长。

### S12-T09：Diagnostics

实现 why waiting/running/failed、Route/Policy/Patch 原因、Artifact Lineage、Budget、Human 和 Recovery history 查询。

**验收**：任意未终结 Run 有一个可验证 waiting responsibility；诊断引用 Event/Version，不返回猜测。

### S12-T10：Capacity Harness

固定硬件和数据生成器，测试 Event append、Replay/Snapshot、unfinished scan、Job claim、Timer、Foreach、Artifact metadata/lineage。

**验收**：输出 P50/P95/P99、吞吐、CPU、内存、磁盘、数据规模和判定；无“只记录数字不判断”。

### S12-T11：性能调优

根据基线调整索引、分页、Snapshot 周期、批量、WAL/checkpoint 和保留策略；所有变更先做正确性 parity。

**验收**：达到 SLO；长 Timeline 不全量物化；Snapshot 有量化收益；无 Event/Receipt 语义变化。

### S12-T12：动态估算器

评估按历史 Attempt、输入规模、模型和 Handler 的 Reservation Estimator；保留静态 Upper Bound。

**验收**：离线回放误差/欠预留率/过预留率满足阈值；新估算器不能放宽硬上限，可关闭回退。

### S12-T13：HTTP API

实现 Workflow Draft/Validate/Publish/Version、Run、Event、Plan/Proposal/Policy、Human、Artifact API；统一 Actor、Idempotency Key、Expected Version 和错误格式。

**验收**：API 不直写 projection；重复请求返回原结果；权限、分页、限流和大小上限完整。

### S12-T14：Run View Model

从同一 Event/projection 生成 Overview、Timeline、Graph、Data、Errors；定义增量 cursor 和状态/原因 DTO。

**验收**：Timeline/Graph 状态一致；历史 PlanVersion 可切换；大图/长 Timeline 分页；UI 不自行推导状态机。

### S12-T15：发布收口

执行 kill/storm/corruption/disk-full/clock/provider/network 安全故障演练；完成备份恢复、升级、容量、告警、应急和数据保留手册。

**验收**：所有阻断 Gate 通过；全量 12 Step Completion Record、Stable Matrix、容量报告、威胁模型和运维手册获批。

## 7. 执行批次

| 批次 | 任务 |
| --- | --- |
| A | G0、T01 |
| B | T02、T05、T08、T10 |
| C | T03–T04、T06–T07、T09、T11–T12 |
| D | T13–T14 |
| E | T15 |

Recovery、安全、Observability、Capacity 四线可在契约后并行；API 依赖稳定 Command/Query；UI View 依赖 Diagnostics/API；最终发布演练必须串行收口所有线。

## 8. 建议代码布局

```text
recovery/{manager,integrity,repair}.py
security/{capabilities,secrets,acl,sandbox}.py
observability/{logging,tracing,metrics,diagnostics}.py
capacity/{workloads,benchmarks,reservation_eval}.py
api/{commands,queries,errors,routes}.py
application/run_view_service.py
docs/operations/{runbook,recovery,backup,security,capacity}.md
```

## 9. 完成定义

1. 任意执行阶段 kill 后可幂等恢复。
2. Repair 默认 dry-run，Apply 只提交审计 Command。
3. Unknown 和不可证明状态有人工接管闭环。
4. Secret/Capability/ACL/Sandbox fail closed并通过攻击测试。
5. 外部副作用始终受 Approval scope 保护。
6. 任意 Run 状态、Route、Retry、Patch、Budget 和 Artifact lineage 可解释。
7. 容量目标、硬件、P95/P99 和资源结果有判定。
8. Snapshot/索引/估算优化不改变确定性或硬上限。
9. API mutation 只提交 Command，具备身份/幂等/版本控制。
10. Timeline/Graph/Data/Errors 来自同一 Run View Model。
11. 全量故障、安全、性能和升级/备份恢复演练通过。
12. 12 Step 文档、Completion Record、Stable Matrix 和运维手册完整。

## 10. 主要风险与控制

| 风险 | 控制 |
| --- | --- |
| Recovery 直接修库制造新漂移 | Dry-run + audited system Command |
| 安全只保护成功路径 | 全路径 Secret scan 和故障测试 |
| Sandbox 被可信 Handler 绕过 | 显式 trust class/capability/审计 |
| 指标高基数拖垮单机 | 标签预算和 ID 进入日志/trace |
| Benchmark 无目标 | SLO Gate 先于 Harness |
| 性能优化破坏确定性 | Golden/replay/parity 前置 |
| API/UI 成为第二状态机 | Command-only mutation + shared RunView |
| 动态估算低估成本 | 静态 Upper Bound 保底和可回退 |

## 11. 开工检查清单

1. 固定单机硬件、规模和 SLO。
2. 批准威胁模型、信任等级和 Capability Registry。
3. 批准 RecoveryReport/RepairAction 与 apply 权限。
4. 批准 API actor/idempotency/version/error contract。
5. 决定 Migration v9 是否必要。
6. 固定安全攻击、故障演练和容量发布 Gate。
7. 固定 Run View Model 和 UI 不推导状态原则。

## 12. Delivery Record 与缺口（2026-07-18）

本节记录当前仓库事实，不代表 Step 12 或发布 Gate 已完成。

### 12.1 已交付映射

| 范围 | 实现证据 | 验证证据 |
| --- | --- | --- |
| Recovery 基础 | paginated/deadline dry-run；Apply 按 finding 精确提交 Expected Version、actor、idempotency/audit；不安全 Unknown 创建人工接管任务 | dry-run、精确 apply、人工接管幂等和分页测试 |
| API 基础 | 认证 callback 边界、1 MiB 请求上限、限流、游标分页、冲突检测；业务前落 pending receipt，崩溃后只允许已验证 reconciliation | replay/conflict/crash/rate/body/auth/reconcile 独立测试 |
| Sandbox 基础 | 不可信执行 fail closed；macOS `sandbox-exec` 文件/网络/fork 策略，Linux 要求 bwrap；流式输出上限 | 网络脚本、目录穿越、symlink、fork、输出炸弹和缺硬限制攻击测试 |
| 安全/观测基础 | Capability/ACL/Redaction、结构化日志、低基数 Metric Registry | 既有 advanced tests |
| 产品查询基础 | RunView、容量 harness、运维文档骨架 | 既有 advanced tests 与基线记录 |

### 12.2 未完成项与限制

1. Recovery 只覆盖当前注册的精确 finding；Plan/Token/Join/Job/Timer/Foreach/Subflow 的完整孤立矩阵和逐项 Repair Command Registry 尚未完成。
2. API 采用“业务前 pending receipt + 已验证 reconciliation”的 at-most-once 边界；通用业务 Command 与最终 HTTP Receipt 尚不能跨 Repository 在同一事务提交。不能把它描述成严格 exactly-once。
3. 认证目前是应用注入 callback，不是完整身份提供方/RBAC；限流是进程内实现，重启后不保留窗口。
4. macOS 无法由当前进程可靠执行硬内存限制；请求硬内存隔离时默认拒绝运行，生产不可信代码仍需外部容器/VM。它不是完整跨平台 Sandbox。
5. Capacity Harness、动态估算器、长 Timeline/Snapshot 调优尚未形成批准后的 P95/P99 发布报告。
6. UI 全流程、备份恢复、升级、disk-full/clock/provider/network 演练及完整安全扫描未完成。
7. 因以上缺口，S12-T15 和产品发布 Gate 均保持未完成；全量单元测试通过只是一项必要条件，不等于发布完成。
