# Agent Workflow Runtime UI API 契约

> 文档状态：U0 Contract Draft
> 消费方：[Agent Workflow UI 可实施设计](agent-workflow-ui-implementation.md)
> 适用范围：`src/orbit/workflow/` Runtime 的 Operating UI；不定义视觉布局。

## 1. 目标与铁律

本文件冻结 UI 开工前必须具备的 Read Model、Command、安全、部署和验证边界。U0 未通过前，不得让前端直接读取数据库、重放 Event、推导状态转换或硬编码当前 Actor 可执行的命令。

1. 服务端 Event/projection 是唯一状态源。
2. Read DTO 版本化、分页并携带 projection version。
3. UI 只提交后端返回的 `allowed_commands[]`，唯一例外见 §4.2.1 Bootstrap Command。
4. 所有 Read/Command 都先认证再授权；敏感读取可审计。
5. 每个 mutation 使用 `Idempotency-Key`、Expected Version 和稳定错误码。
6. Plan Definition 与 Runtime Overlay 是两个独立维度。

## 2. U0 Gate

| Gate | 内容 | 完成条件 |
|---|---|---|
| U0.1 | 冻结版本化 DTO、分页、游标和 Projection Version | Schema/Golden/Contract Tests 通过 |
| U0.2 | 统一认证、Read ACL、AllowedCommand、Command Envelope、Expected Version 和错误码 | 权限、幂等、冲突和敏感读取测试通过 |
| U0.3 | 挂载 `/api/v1` 与 `/workflow-ui` | `orbit serve` 启动后真实 HTTP smoke test 通过 |
| U0.4 | 容量、安全、故障和前后端契约 Gate | 长 Timeline、大 Graph、分页、崩溃窗口和无权限测试通过 |

U0.1–U0.4 全部通过后才能开始 UI U1。后续 §7 的每项后端能力都标注所属 Gate，避免与 UI 分期产生第二套口径。

## 3. DTO 契约（U0.1）

### 3.1 通用 Envelope

```json
{
  "schema_version": "1.0",
  "projection_version": 42,
  "data": {},
  "next_cursor": null
}
```

- Cursor 是不透明字符串；前端不得解析或自行计算。
- 同一分页序列固定排序，重复请求返回稳定结果。
- Projection Version 用于刷新和冲突提示，不代替 Aggregate Expected Version。

### 3.2 RunSummary

```text
run_id / display_name
workflow_id / workflow_version
status / updated_at
primary_responsibility / responsibility_count
budget_summary
requires_actor_action
projection_version
```

`requires_actor_action` 由后端根据当前 Actor 权限计算。Run 列表不得逐 Run 调用 `why()`，必须通过一次分页查询返回。

### 3.3 Responsibility 与 AllowedCommand

```json
{
  "responsibility_id": "human:h9",
  "kind": "human",
  "label": "法务审批",
  "status": "waiting",
  "expected_version": 3,
  "allowed_commands": [
    {
      "command": "human.submit.approve",
      "method": "POST",
      "href": "/api/v1/human-tasks/h9/submit",
      "target_aggregate_id": "human_task:h9",
      "payload_schema": "human-submit-approval/1.0",
      "expected_version": 3,
      "confirmation": "explicit"
    }
  ]
}
```

- `allowed_commands` 是当前 Actor、当前版本、当前 Policy 下的授权快照。
- Responsibility 顶层 `expected_version` 表示责任事实所属聚合的当前版本；每个 AllowedCommand 的 `expected_version` 针对该命令的 `target_aggregate_id`，是提交该命令时的权威版本，两者可以不同。例如同一责任项可以同时提供针对 HumanTask、Run 或 BudgetAccount 聚合的命令。
- UI 不根据 kind/status/role 拼装 endpoint 或补按钮。
- Command 提交后仍由 Kernel 重验权限和 Expected Version；DTO 不是长期授权凭证。

### 3.4 Run Detail DTO

使用共享语义、独立分页的端点：

```text
GET /api/v1/runs/{id}/summary
GET /api/v1/runs/{id}/timeline?after=&limit=
GET /api/v1/runs/{id}/graph?plan_version=
GET /api/v1/runs/{id}/data?kind=&after=&limit=
GET /api/v1/runs/{id}/errors?after=&limit=
GET /api/v1/runs/{id}/responsibilities
```

- Timeline Event DTO 包含 position、type、occurred_at、correlation_id 和已脱敏 payload。
- Errors 是独立完整投影，不能只过滤当前 Timeline 页。
- Data、NodeRun、Foreach Item 和长 Timeline 必须分页。
- Graph DTO 直接返回 GraphNode、BranchToken、JoinGroup、Retry Generation 和 Loop Counter 投影；UI 不消费 Event 重建它们。

### 3.5 Plan Definition、Runtime Overlay 与 Diff

- `plan_version=N` 只选择不可变 Plan Definition vN。
- NodeRun、Timeline 和 Data 默认仍是 Runtime Overlay: current。
- Plan Diff 由后端返回节点/边/映射差异以及 `template`、`planner_proposal`、`human_command`、`system_recovery` 来源事实。
- `as_of_global_position` 历史 Runtime Overlay 是 U5 之后的非 MVP，不属于 U0 或 UI U5 完成条件。

### 3.6 其他 DTO 与扩展边界

U0.1 还需冻结 MVP 使用的：

- InboxItem
- BudgetSummary（含 unit/currency）
- DecisionRecord
- Stable API Error Envelope

U0.1 只冻结扩展 DTO 必须复用的 Envelope、Cursor、Projection Version、ACL 和错误规则；不要求提前冻结 U6/U7 的全部业务字段。ForeachGroup/Item、SubflowLink、Artifact/Lineage 在 UI U6 前冻结，RecoveryFinding/RepairAction 在 UI U7 前冻结，不能反向阻塞 U1–U3 MVP。

## 4. Command 契约（U0.2）

### 4.1 Command Envelope

```json
{
  "command": "human.submit.approve",
  "expected_version": 3,
  "payload": {}
}
```

请求头至少包含：

```text
Authorization: ...
Idempotency-Key: <client generated UUID>
```

Budget、Workflow Publish、Human Submit 和后续命令统一 Expected Version 语义。

**Budget（已修正）**：`POST /api/v1/runs/{id}/budget` 要求 `expected_version`，
且该版本属于 `budget_account:<run_id>` 聚合，**不是 Run 的版本**——两者各自计数，
混用会在账户有过预留或用量上报后立刻冲突。Responsibility 广告的
`budget.add` AllowedCommand 已携带正确版本，客户端原样回传即可。

版本过期返回 `409 version_conflict`。幂等重放（相同 Key、相同金额）**不**校验版本：
该笔授予已经发生，账户版本必然已经前进，此时拒绝等于拒绝一条已成功命令的重试。

### 4.2 命令发现

UI 通过 `allowed_commands[]` 获得 method、href、payload schema、Expected Version 和确认策略。以下仅是领域命令类型，不是前端固定路由：

- human.submit
- budget.add
- approval.submit
- run.cancel
- recovery.takeover
- recovery.apply

#### 4.2.1 Bootstrap Command（唯一例外）

`allowed_commands[]` 是挂在某个既有聚合上的授权快照。**开始一个新 Run 时没有聚合可挂**——Run 还不存在，因此没有任何 Read Model 能广告它。

因此 `POST /api/v1/runs` 是契约中**唯一**允许客户端直接构造的 mutation 路径，称为 Bootstrap Command。它受同样的约束：认证、授权 scope、`Idempotency-Key`。它没有 Expected Version，因为不存在要比较的版本。

约束：

- 例外只有这一条。任何其他 mutation 端点被客户端硬编码都是违约。
- `tests/test_ui_assets.py::test_mutations_only_travel_through_allowed_commands`
  以「客户端字面量集合 == `{("POST", "/api/v1/runs")}`」的形式钉住，多一条就红。
- 这是**当前**状态，不是终局。Workflow Catalog 端点（见 §4.2.2）落地后，
  `start_run` 应作为 catalog 条目的 AllowedCommand 返回，本例外随之删除。

#### 4.2.2 Workflow Catalog（未实现）

消除 Bootstrap 例外需要一个可发现的工作流目录：列出已发布的 workflow 及其版本，
每条附带 `start_run` 的 AllowedCommand。当前 UI 让用户手工输入 `workflow_id`，
既是可用性问题，也是这条例外存在的原因。归属 Goal/Catalog UI 里程碑。

### 4.3 幂等与冲突

| 情况 | HTTP/Code | UI 行为 |
|---|---|---|
| 相同 Key + 相同请求且已有结果 | 原状态码 + 原响应 | 作为重放结果展示 |
| 相同 Key + 不同请求 | 409 `idempotency_conflict` | 阻止并报告客户端 Key 复用错误 |
| 原请求结果尚未确认 | 409 `command_in_progress` | 显示确认中，禁止自动重提 |
| Expected Version 过期 | 409 `version_conflict` | 拉取最新投影并要求用户重新确认 |
| 无认证/无权限 | 401/403 | 不展示敏感详情，不降级为匿名操作 |

当前 pending-receipt 提供 at-most-once 边界，不是通用 exactly-once。只有后端能够证明业务结果时才能 reconciliation；UI 不能猜测成功。

## 5. 认证、ACL 与审计（U0.2）

1. 所有 Read 和 Command 都执行 authenticate + authorize。
2. Run、Artifact、Human Form、Planner Raw Response 和 Ops 使用独立 Capability。
3. `capability_service` 必须真正接入 Read 路径，不能只保护 mutation。
4. Raw Response 默认不可见；专家读取也必须脱敏并写审计。
5. Artifact/Lineage 不允许凭 ID 枚举跨 Run 数据。
6. Ops Apply 只暴露已注册、可证明安全的 RepairAction，并要求二次确认。
7. Secret、submission token、Raw Response 不进入 URL、日志、analytics 或浏览器持久化存储。

## 6. 部署契约（U0.3）

单机默认：

```text
orbit serve
├─ /ui
├─ /workflow-ui
└─ /api/v1
```

Runtime 逻辑继续位于 `src/orbit/workflow/`，由 composition root 挂载；不复制到 `server.py`。U0.3 必须验证：

- 项目与 DB 路径一致
- 身份与授权一致
- 静态资源和 SPA fallback
- API/页面启动、关闭和错误传播
- 不需要额外 CORS 特例
- `orbit serve` 真实 HTTP smoke test

## 7. 后端展开清单与 U0 映射

本节是 U0 的展开清单，不是另一套阶段定义。

| 后端能力 | Gate | 当前状态 |
|---|---|---|
| 版本化 DTO、Stable Error、Opaque Cursor | U0.1 | 缺失 |
| RunSummary 分页与 Actor-aware 排序 | U0.1/U0.2 | 缺失 |
| Run Detail 独立分页 Read API | U0.1 | 当前仅原型聚合 RunView |
| Responsibility + AllowedCommand | U0.1/U0.2 | 当前 why() 无授权动作 |
| Read Auth/ACL/敏感读取审计 | U0.2 | 未闭合 |
| 统一 Command Envelope/Expected Version | U0.2 | 部分端点不一致 |
| Inbox 聚合 API | U0.1/U0.2 | 缺失 |
| Cancel/Approval/Takeover Command | U0.2 | 缺失 |
| Plan Diff 与来源事实 | U0.1 | 缺失 |
| Foreach/Subflow/Lineage 分页 DTO | UI U6；复用 U0 Envelope/ACL | 缺失，不阻塞 U1–U3 |
| Recovery/Integrity/Capacity API | UI U7；复用 U0 Auth/Command | 部分能力存在，不阻塞 U1–U3 |
| correlation_id Event DTO | U0.1 | 当前 RunView 未返回 |
| `/api/v1` + `/workflow-ui` 实际挂载 | U0.3 | 缺失 |
| Contract/权限/容量/故障测试 | U0.4 | 缺失 |
| SSE | UI U7 | 非 U0；可先轮询 cursor |
| 历史 Runtime Overlay | Post-U5 | 非 MVP |

## 8. 当前能力事实表

| 能力 | 当前状态 | 契约结论 |
|---|---|---|
| 单 RunView/why | 原型可用 | 需拆分页 DTO、补 correlation 和 ACL |
| Human submit/Budget add | 原型可用 | 需统一 Expected Version、错误码和 reconciliation |
| Run list/Inbox | 缺失 | U0/U1 新增，禁止 N+1 why |
| Plan Definition 切换 | 基础可用 | 不是历史 Runtime 快照 |
| Plan Diff/来源 | 缺失 | 后端生成并关联 Proposal/Patch/Actor |
| Foreach/Subflow | 表和基础服务存在 | Step 11 缺口与分页 API 完成后再做 UI U6 |
| Artifact scoped read | 未闭合 | 先完成 Read ACL 和审计 |
| Recovery/Ops | 部分实现 | 只暴露注册且安全的 Action |
| Sandbox/Capacity 发布能力 | 未完成 | 不作为 UI 已交付能力宣传 |
| `/api/v1` 实际挂载 | 缺失 | U0.3 阻断项 |

## 9. U0 验收测试

1. DTO Schema/Golden 和前后端 Contract Tests。
2. Read ACL：跨 Run/Artifact/Human/Raw Response/Ops 越权全部 fail closed。
3. AllowedCommand：不同 Actor、状态和版本得到正确动作；UI 不存在 fallback 推导。
4. Mutation replay、idempotency conflict、command in progress 和 version conflict。
5. 崩溃发生在业务 Command 与 Receipt 边界时不自动重复执行。
6. Timeline/Data/Errors/Run/Inbox 分页稳定，无 N+1 why。
7. 长 Timeline、大 Graph、大 Foreach Item 页满足容量阈值。
8. `/workflow-ui` 与 `/api/v1` 在真实 `orbit serve` 进程下启动、访问和关闭。
9. API payload、日志和错误响应无 Secret/submission token 泄漏。

## 10. 完成定义

U0.1–U0.4 全部通过，且本文件的“当前状态”与代码事实一致后，UI U1 才能开始。通过 U0 只代表前端具备安全、稳定的实施入口，不代表 Step 10–12、Foreach/Subflow、Ops、Sandbox、容量或产品发布 Gate 已完成。
