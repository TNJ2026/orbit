# Agent Workflow Runtime UI API 契约

> 文档状态：U0 Contract Implemented（静态图产品范围）
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

#### 4.2.1 Workflow Catalog（已实现）

`GET /api/v1/workflows` 列出已发布 workflow 的最新版本，并按当前 Actor 权限为每项
返回 `run.start` AllowedCommand。新 Run 虽然还没有聚合，命令仍可以挂在不可变的
WorkflowVersion catalog entry 上，因此不再需要 Bootstrap 例外。

`tests/test_ui_assets.py::test_mutations_only_travel_through_allowed_commands`
钉住客户端中硬编码 mutation 字面量集合为空；包括 `start_run` 在内的所有写操作都
只能执行服务端给出的 method、href、target 和 expected version。

#### 4.2.3 Recovery Apply（逐 finding）

`POST /api/v1/recovery/apply` 接收 `action_ids: string[]` —— 操作者**勾选**的那些 finding，
不接收分页参数。对整个扫描结果一把梭是错的形状：操作者判断的是他们看过的那一份列表，
重新扫描会把他们没看过的新 finding 也一起执行掉。空列表和缺字段一律拒绝。

`action_id` 形如 `code:entity:expected_version`，**它本身就是 CAS token**。
实体在扫描之后变动过，其 action_id 随之改变，因此该项返回 `stale` 而不是拿一个
操作者从未见过的版本去执行。

每项独立报告 outcome：

| outcome | 含义 |
|---|---|
| `applied` | 已执行 |
| `stale` | 当前扫描不再报告该 finding（实体已变动或已消失） |
| `unsafe` | `safe_to_apply=false`，转人工接管 |
| `failed` | 执行抛错，带异常类型 |

单项失败不影响其余项——半执行状态比完整报告的部分失败更难排查。
**每一项都写审计**，包括没执行的：「为什么这条没恢复」是操作者的下一个问题。

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
| 版本化 DTO、Stable Error、Opaque Cursor | U0.1 | 已实现 |
| RunSummary 分页与 Actor-aware 排序 | U0.1/U0.2 | 已实现 |
| Run Detail 独立分页 Read API | U0.1 | 已实现 Timeline、Errors、Data |
| Responsibility + AllowedCommand | U0.1/U0.2 | 已实现 |
| Read Auth/ACL/敏感读取审计 | U0.2 | 普通 Run Read 已闭合；Raw/Blob 未暴露 |
| 统一 Command Envelope/Expected Version | U0.2 | 已实现 |
| Inbox 聚合 API | U0.1/U0.2 | 已实现 |
| Cancel/Approval/Takeover Command | U0.2 | Cancel/Human/Recovery 已实现 |
| Plan Diff 与来源事实 | U0.1 | 已实现 |
| Foreach/Subflow/Lineage 分页 DTO | UI U6；复用 U0 Envelope/ACL | 缺失，不阻塞 U1–U3 |
| Recovery/Integrity/Capacity API | UI U7；复用 U0 Auth/Command | 已实现并有浏览器 Apply E2E |
| correlation_id Event DTO | U0.1 | 当前 RunView 未返回 |
| `/api/v1` + `/ui` 实际挂载 | U0.3 | 已实现 |
| Contract/权限/容量/故障测试 | U0.4 | 已实现 |
| SSE | UI U7 | 非 U0；可先轮询 cursor |
| 历史 Runtime Overlay | Post-U5 | 非 MVP |

## 8. 当前能力事实表

| 能力 | 当前状态 | 契约结论 |
|---|---|---|
| 单 RunView/why | 原型可用 | 需拆分页 DTO、补 correlation 和 ACL |
| Human submit/Budget add | 原型可用 | 需统一 Expected Version、错误码和 reconciliation |
| Run list/Inbox | 已实现 | 分页 Read Model，无 N+1 why |
| Plan Definition 切换 | 基础可用 | 不是历史 Runtime 快照 |
| Plan Diff/来源 | 已实现 | 后端生成版本差异 |
| Foreach/Subflow | 表和基础服务存在 | Step 11 缺口与分页 API 完成后再做 UI U6 |
| Artifact scoped read | 已实现元数据与 Lineage | Blob key/content 不进入普通 Run Read |
| Recovery/Ops | 已实现 | finding 逐项广告并确认 Apply |
| Sandbox/Capacity 发布能力 | 已实现自动化 Gate | 仍需发布时执行人工 Gate |
| `/api/v1` 实际挂载 | 已实现 | 与 `/ui`、`/mcp` 同源 |

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
