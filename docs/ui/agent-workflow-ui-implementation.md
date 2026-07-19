# Agent Workflow UI 可实施设计

> 文档状态：Implemented UI/UX Baseline（静态图产品范围；动态 Foreach/Subflow/Agentic UI 后续）
> 定位：把 [Future Agent Workflow UI 设计理念](Future%20Agent%20Workflow%20UI%20设计理念.md) 的愿景，落成对齐 Step 1–12 durable runtime 的可实施设计。
> 姊妹文档：[agent-workflow-ui-api-contract.md](agent-workflow-ui-api-contract.md)（Runtime UI API 契约与 U0 Gate）。
> 铁律（沿用 Step 12）：**UI 只渲染 Event-derived 状态，只提交后端明确授权的 Command，绝不自建第二状态机，绝不从日志或 Event 自行投影状态。** 每个 mutation 使用统一 Command Envelope，并携带 `Idempotency-Key` + `Expected-Version`；暂不满足该契约的端点必须先在 U0 修正。

## 0. 本设计与理念文档的差异

理念文档抓对了六个转变（目标驱动、AI 规划、动态调整、行为轨迹、Artifact 中心、自我优化），但缺三块骨干、四个真实 widget、零后端映射。本文补齐：

| 理念文档缺口 | 本文补 | 后端依据 |
|---|---|---|
| 无 Why-waiting | §4 首屏必备 | `DiagnosticsService.why` |
| 无控制/Steering 面 | §6 命令面 | `/api/v1` mutation + HumanTask/Budget/Approval |
| 无 Budget 可见性 | §4.3 预算仪表 | `budget_accounts` ledger |
| Foreach/Subflow 未提 | §5.4/§5.5 专用 widget | `foreach_groups/items`、`subflow_links` |
| 版本演进停在示意 | §5.3 PlanVersion diff | 不可变 `execution_plans` 链 |
| Decision 面板疑似渲染模型原话 | §5.2 只渲染结构化 Proposal | Planner raw-first 两阶段 |
| Graph 全进高级模式（过度） | §5.1 线性 list / 并行 graph 混合 | parallel route + join 拓扑 |

## 1. 系统边界：两个 orbit，别混

| | 旧引擎 | 新 runtime（本文对象） |
|---|---|---|
| 代码 | `src/orbit/server.py` | `src/orbit/workflow/` |
| UI | 8848/ui 任务看板（保留给存量） | 本设计（新建） |
| 数据源 | SQLite 直读 | `RunViewService` + `DiagnosticsService` + `/api/v1` |

新 UI 不把 Runtime 逻辑写回 `server.py`，但必须在 composition root 中挂载新 API 和静态资源。单机产品默认部署形态：

```text
orbit serve
├─ /ui              现有项目/Goal UI
├─ /workflow-ui     新 Runtime UI
└─ /api/v1          新 Runtime API
```

如果未来拆为独立进程，必须另行解决项目发现、DB 路径、认证、CORS 和生命周期；不能把“新 UI 不碰 server.py”理解为无需集成。Authoring 与 Operating 通过不可变 `WorkflowVersion` 连接，并共享统一身份、项目和权限边界。

## 2. 三层信息架构

```text
Authoring（设计）   ──发布 WorkflowVersion──▶   Operating（运行/介入）
   目标→计划→确认                                  Run 列表 / Run 详情 / 收件箱
        │                                                    │
        └── 已有两份设计文档 ────────────────────────────────┘
                                              本文重点：Operating
```

- **Authoring**：接 [prompt-authoring](../workflow-prompt-authoring-design.md) + [list-view](../workflow-list-view-design.md)，不重复。
- **Operating**：本文主体。用户在这里看 run、在关键点介入。

## 3. Operating 顶层导航

```text
┌ Orbit Runtime ─────────────────────────────────────────────┐
│ [Runs]  [Inbox ③]  [Artifacts]  [Ops]                        │
└─────────────────────────────────────────────────────────────┘
```

- **Runs**：run 列表 + 详情（§4、§5）
- **Inbox ③**：跨 run 聚合的待人工处理项，角标 = 待办数（§6.1）——这是"控制"的入口，理念文档漏的。
- **Artifacts**：跨 run artifact 浏览 + lineage（§5.6）
- **Ops**：recovery / integrity / capacity（§7，专家）

## 4. Run 列表 + Run 详情首屏（Why-first）

### 4.1 Run 列表

```text
状态  Run              Workflow        等待原因         预算    更新
● 运行 content #a3f2   content-publish  —               62%    2m前
⏸ 等待 research #b81   market-research  human:法务审批   40%    5m前
⚠ 阻塞 deploy #c04     ci-deploy        budget 耗尽      100%   1m前
✓ 完成 report #d12     weekly-report    —               31%    1h前
```

字段由新的分页 `RunSummary` Read Model 一次返回，禁止为每个 Run 单独调用 `DiagnosticsService.why` 形成 N+1 查询：

```text
run_id / display_name / workflow_id / workflow_version
status / updated_at
primary_responsibility / responsibility_count
budget_summary
requires_actor_action
projection_version
```

列表按“当前 Actor 需要介入的排前面”排序，而不是把所有 waiting-human 都视为当前用户待办；排序和权限判断由后端完成。

### 4.2 Run 详情：Why 横幅是第一屏

```text
┌ Run: content #a3f2 ─────── content-publish v1 · plan v3 ──────────┐
│ ⏸ 为什么在等？                                                     │
│   法务审批 (human:h9, waiting)              [批准] [驳回] [详情]     │
│   预算 62%（reserved 12% · consumed 50%）                          │
├────────────────────────────────────────────────────────────────────┤
│ Overview │ Timeline │ Graph │ Data │ Errors │      Plan: v3 ▾       │
```

Why 横幅最终来自版本化 Responsibility Read Model。当前 `DiagnosticsService.why(run_id)` 只提供责任事实和 Budget 数值，尚无 `allowed_commands`、Actor 权限或 exhaustion policy，必须在 U0 扩展：

| UI | why() 字段 |
|---|---|
| 等待项列表 | `responsibilities[]`（kind ∈ human/job/timer/planner/foreach/subflow） |
| 每项的行动按钮 | 只能渲染后端返回的 `allowed_commands[]`，禁止 UI 根据 kind、状态或角色自行推导 |
| 预算条 | `budget.{total,reserved,consumed,remaining}` |

**设计原则**：任何非终态 run，首屏第一眼回答"等什么、我能做什么"。终态 run 此横幅收起。

### 4.3 预算仪表（信任的核心）

三段条：`consumed`（实心）| `reserved`（斜纹）| `remaining`（空）。超支时 remaining 为负，红色溢出段。耗尽态显示后端返回的 exhaustion policy（cancel/finish-current/fail/wait-for-budget）和授权的 [追加预算] Command。数据来自 Budget Read Model；单位/currency 由后端提供，不能把 microunits 直接格式化为美元。

## 5. Run 详情五 tab（共享版本化 Read Model）

五个 Tab 必须来自同一套 Event/projection 语义，但不能长期依赖一个不断膨胀的响应。当前 `RunViewService.get` 可作为原型首屏；U0 后拆为独立分页 DTO：

```text
GET /api/v1/runs/{id}/summary
GET /api/v1/runs/{id}/timeline?after=&limit=
GET /api/v1/runs/{id}/graph?plan_version=
GET /api/v1/runs/{id}/data?kind=&after=&limit=
GET /api/v1/runs/{id}/errors?after=&limit=
GET /api/v1/runs/{id}/responsibilities
```

`summary` 可以嵌入各 Tab 首屏摘要；Timeline、Errors、Data、NodeRun、Foreach Item 必须分别分页。当前实现的 `errors` 只从本页 Timeline 计算，不能作为完整错误列表；Values/Artifacts/NodeRuns 当前全量读取，也不能作为容量目标下的最终接口。

### 5.1 Graph tab：线性 list / 并行 graph 混合

**默认列表**（[list-view 设计](../workflow-list-view-design.md)）渲染 `graph.nodes` + `graph.plan`：编号大纲，rework 回边文字标注，运行态叠加状态点。

**并行/join 局部升 graph**：检测到 parallel route 或 join 节点（`plan` 里入度>1 的 join、多出边 parallel），该段落显示"[在画布查看]"，局部二维渲染 branch token + join 进度。理念文档说"graph 全进高级模式"是过度——并行拓扑 list 表达不了，必须混合。

运行态叠加：node status、branch token、join 组、retry generation 和 loop counter 必须由后端投影为版本化 `GraphNodeView`、`BranchTokenView`、`JoinGroupView` 等 DTO。前端不得重放 Timeline Event 或自行实现 Join/Retry Reducer；每个 DTO 应携带 projection version 或 source event position。

### 5.2 Agent Decision 面板（渲染结构化 Proposal，不是模型原话）

理念文档 §9 的 Decision 面板画得像渲染模型自由文本——**纠偏**：后端 Planner 是"raw response 先落盘 → 严格解析成结构化 ActionProposal"。UI 只渲染**已记录的结构化决策事实**：

```text
Planner Decision (attempt p7)
  Context:   context_hash 9f3a · plan v2 · budget remaining 38%
  Proposal:  add node "deep_research" before "write"   [accepted]
  Cost:      1,240 tokens · 20,000 cost-microunits (unit: API)
  Reject 原因（若 rejected）: policy: capability tool.web not allowed
```

数据源：版本化 Decision Read Model，由后端组合 `planner_proposals`、`planner_attempts`、PolicyDecision、Budget 和 PlanPatch。**绝不让 UI 解析模型文本**。Raw response 默认不可见；即使专家模式也必须具备专用 Capability、完成 Secret redaction 并记录敏感读取审计。

### 5.3 Plan 版本演进（自我优化的真正难点）

顶部 `Plan: v3 ▾` 切换器。切到 vN 展示的是不可变 **Plan Definition vN**；NodeRun、Timeline、Data 默认仍是 **Runtime Overlay: current**。当前 API 不能把它描述为完整历史时点快照。真正的 `as_of_global_position` 历史 Runtime Overlay 明确划为 **U5 之后的非 MVP 能力**；若未来实现，必须由后端通过 Snapshot + Event 构造，前端不能自行重放。

版本 diff 视图：

```text
v2 → v3   (planner p7, accepted)
  + deep_research   (planner 新增, pending→ready)
  ~ write.input     (mapping 改)
  节点来源徽章：🤖 planner 加 / 👤 人加 / 📋 模板原生
```

Plan Diff 优先由后端生成稳定 DTO，避免大图前端重复加载和算法漂移。节点来源不能由“是否位于 Agentic Region”推断；必须来自已记录事实：`template`、`planner_proposal`、`human_command` 或 `system_recovery`，并关联 proposal_id、patch_id 和 actor。只能修改 pending 部分，ready/active/history 不可变——UI 上这些节点标锁。

### 5.4 Foreach：item 网格

```text
▣ 处理 42 篇文档  (并发 4 · 38/42 完成 · 2 失败 · fail-policy: continue)
  ┌──┬──┬──┬──┬──┐
  │✓ │✓ │● │✓ │⚠ │  ← 每格一 item，点开看 item scope
  └──┴──┴──┴──┴──┘
  聚合：按 index 排序（完成顺序无关）
```

数据来自专用 Foreach Read API。大集合必须分页查询和虚拟渲染。注意：分页 materialization、Item Retry/Budget/Unknown 和完整恢复仍是 Step 11 缺口，U6 不能在这些能力完成前宣称闭环。

### 5.5 Subflow：父子 run 导航

面包屑 `content #a3f2 › subflow: translate › child #e91`。子 run 是独立详情页（有自己的五 tab + why）。父子通过 Subflow Read Model 连接，取消/失败传播方向按 link 的 propagation policy 展示。完整传播竞态、恢复和 Artifact capability transfer 仍是 Step 11 缺口，UI 不能仅凭表中 link 推断已完成传播。

### 5.6 Timeline / Data / Errors

- **Timeline**：版本化 Event DTO（position/type/occurred_at/payload/correlation_id），游标 `next_cursor` 增量加载，绝不全量。当前 RunView Event DTO 尚未返回 correlation_id，须在 U0 补齐。
- **Data**：分页 Value/Artifact DTO。Artifact 点开使用受 ACL 保护的 Lineage API 展示 producer/consumer/derived links。当前 Read API 尚未闭合认证与 ACL；完成 U0 前不能宣称 scoped read，也不能开放按 ID 查询。
- **Errors**：`errors[]`（type 以 failed/rejected/unknown 结尾或带 code 的事件）。每条链到对应节点 + route/policy 原因。

## 6. Steering：命令面（理念文档缺的"控制"）

所有介入都是提交 Command，走 `/api/v1` 幂等端点。当前 pending-receipt 提供 at-most-once 边界，但不能表述为通用 exactly-once：崩溃后可能进入 `command_in_progress`，只有后端能够证明业务结果时才能 reconciliation。UI 表单必带 `Idempotency-Key`（前端生成 UUID）+ `Expected-Version`（从当前投影读）；Budget 等尚未接收 Expected Version 的端点必须在 U0 修正。

### 6.1 Inbox：跨 run 人工待办

```text
Inbox
  ⚖ 法务审批   content #a3f2 · deadline 2h    [批准] [驳回]
  📝 补充信息  research #b81 · form           [填写]
  💰 预算耗尽  deploy #c04                     [追加] [终止]
  ❓ 结果未知  api-call #f2 (unknown)          [人工接管]
```

聚合所有 run 的 `human_tasks`（waiting/claimed）+ budget 耗尽 + unknown attempt。提交走 `POST /api/v1/human-tasks/{id}/submit`（submission_token + decision + expected_version）。一次性 token：提交后按钮失效；会签显示"2/3 已批"。

### 6.2 命令类型参考

下表只描述产品需要支持的命令类型，不是 UI 的固定端点路由表。运行时实际可见的按钮、method、endpoint、payload schema、Expected Version 和确认策略，全部以后端 `allowed_commands[]` 为准；未返回的命令绝不能由前端自行补出。

| 命令类型 | 用户意图 | Domain 约束 |
|---|---|---|
| human.submit | 批准、驳回或补充输入 | 一次性 submission token、Actor、Expected Version、Form/Quorum |
| budget.add | 追加预算 | 幂等、Expected Version、单位明确、权限校验 |
| approval.submit | 批准外部副作用 | scope/request hash 绑定，不能跨 run/node/capability 重用 |
| run.cancel | 取消 Run | Kernel 全局收敛、迟到结果受 Fence 保护 |
| recovery.takeover | 人工接管 Unknown | 产生新 Attempt/补偿/终止，不能伪造外部成功 |
| recovery.apply | 应用安全修复 | dry-run 预览、逐 finding Expected Version、Actor 和审计 |

完整 `AllowedCommand` DTO、Command Envelope 和 endpoint 语义由 [Runtime UI API 契约](agent-workflow-ui-api-contract.md) 冻结。

### 6.3 幂等/版本冲突的 UI 反馈

- 相同 Key + 相同请求：服务直接重放原响应，不返回冲突。
- 409 `idempotency_conflict`：相同 Key 被用于不同请求，阻止操作并提示客户端错误；不能展示旧结果冒充本次成功。
- 409 `command_in_progress`：原请求结果尚未确认，显示“正在确认”，轮询状态或进入受控 reconciliation，禁止自动重提。
- 409 `version_conflict`：Expected Version 过期，提示“状态已变化”，拉取最新投影后由用户重新确认。

## 7. Ops 视图（专家）

- **Recovery**：`RecoveryManager.scan` dry-run 列 findings（每项带 safe_to_apply）→ 勾选 → apply（逐 finding 带 expected_version + 审计）。unknown/orphan 不自动执行，转 §6 人工接管。
- **Integrity**：db-check 报告（event gap / projection drift / blob 一致性）。
- **Capacity**：P50/P95/P99、队列深度、worker/lease/timer 健康。
- **Durable**：job/lease/timer 表，worker heartbeat——普通用户看不到。

Ops UI 只能展示后端已注册的精确 RepairAction；当前 Recovery 仅覆盖部分 finding，不能把所有扫描结果都渲染成可执行 Apply。所有 Ops Read/Command 都要求独立 Capability、Actor 审计和二次确认。

## 8. 视觉方向（采纳理念文档，具体化）

IDE + Mission Control（VS Code / Linear / GitHub Actions 参考）。深色画布 + 极简卡片 + live activity。具体约束：

- **状态表达**：运行=蓝、等待=琥珀、阻塞/失败=红、完成=绿、跳过=灰；必须同时使用文字、图标和 ARIA label，不能只靠颜色。
- **卡片信息密度**：Step 卡片显示"目标/Agent/产出/策略"（理念文档 §5 对），但技术 ID（node_run_id 等）折进详情抽屉。
- **Live**：Timeline/Inbox 用 SSE 或轮询增量（`after` 游标），不整页刷。
- **主题**：支持浅色、深色和跟随系统；Dark Canvas 是视觉方向，不是唯一可用主题。
- **主从布局**：列表 + 右侧详情面板，方向键切换；不可逆操作使用就地确认或确认面板，不做未经投影确认的乐观成功。

## 9. 后端契约依赖

具体 DTO、API、认证、ACL、Command Envelope、错误语义、现状事实和 U0 Gate 已拆至 [Runtime UI API 契约](agent-workflow-ui-api-contract.md)。本文只保留 UI 所需的四条边界：

1. U0 未通过前不得让前端直接读取表、解析 Event 或硬编码可执行 Command。
2. 所有 Read Model 都版本化并分页；所有可执行动作都来自 `allowed_commands[]`。
3. Plan Definition 与 Runtime Overlay 分离；U5 只做 Definition 切换和 Diff。
4. `as_of_global_position` 历史 Runtime Overlay 是 U5 之后的非 MVP，不阻塞 U1–U7。

## 10. UI 分期

| 阶段 | 界面交付 | 后端依赖 |
|---|---|---|
| U1 | Run 列表、Summary、只读 Timeline | U0 Gate 全部通过 |
| U2 | Why 横幅、Responsibility、预算仪表 | Responsibility/AllowedCommand DTO |
| U3 | Inbox、Human/Budget Steering | Inbox 与 mutation API |
| U4 | Graph 混合视图、BranchToken/Join/Retry 叠加 | Graph Projection DTO + list-view |
| U5 | Plan Definition 切换与 Diff | Plan Diff API；不含历史 Runtime Overlay |
| U6 | Foreach 网格、Subflow 导航、Artifact Lineage | Step 11 Runtime 缺口 + 专用分页 API |
| U7 | Ops 与 Live 更新 | Recovery Registry、Ops ACL、SSE |
| Post-U5 | `as_of_global_position` 历史时点回看 | Snapshot + Event 历史投影 API；非 MVP |

U1–U3 是产品 MVP：能看 Run、能理解责任、能安全介入。U4 之后才完整表达受约束的计划演进。

## 11. UI 质量 Gate

1. 服务端 projection 是唯一真相；不可逆 Command 在新 projection 到达前只显示 pending。
2. Timeline/Data 使用虚拟列表，Graph 超过阈值默认折叠，首屏不下载完整历史或 Foreach Items。
3. 状态同时使用文字、图标和颜色；键盘导航、焦点恢复、ARIA、浅/深色对比度进入验收。
4. 前端不持久化 Secret、submission token 或 Raw Response；敏感字段不进入 URL、日志或 analytics。
5. 预算使用后端返回的 unit/currency，不硬编码 `$`。

## 12. 总结

保留 Mission Control、Why-first、Steering、Budget、结构化 Decision 和局部 Graph 方向。UI 的职责是让用户理解、控制和审计一个在 Policy、Budget、Approval 和不可变 PlanVersion 约束下演进的系统；后端契约和工程 Gate 由姊妹文档独立维护，避免实现约束淹没界面信息层级与交互设计。
