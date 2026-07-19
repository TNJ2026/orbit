# Runtime UI 原型落地规划

> 文档状态：Executed
> 基线日期：2026-07-19
> 执行状态：P0–P9 已完成；动态能力 Gate 已由生产可达链路和浏览器/API 投影关闭
> 原型基线：[`prototypes/runtime-ui.html`](../../prototypes/runtime-ui.html)
> 生产入口：`orbit serve` → `/ui`
> 相关契约：[`agent-workflow-ui-implementation.md`](agent-workflow-ui-implementation.md)、[`agent-workflow-ui-api-contract.md`](agent-workflow-ui-api-contract.md)
> 适用范围：将已恢复的 Runtime UI HTML 原型逐步实现为可交付、可审计的生产 UI；本文件不改变 Runtime 领域语义。

## 1. 目标

以恢复后的 HTML 原型作为信息架构、视觉语言和交互意图的基准，在现有 `/ui` 上完成 Runtime 产品界面。最终界面需要同时满足：

1. 覆盖原型的 Home、Goals、Workflows、Runs、Inbox、Artifacts、Agents、Ops、Settings 和 New Goal 向导。
2. 所有状态来自服务端 Read Model；所有写操作来自服务端当前 Actor 的 `allowed_commands[]`。
3. 保留原型的 Mission Control 风格、主从布局、Why-first 运行详情和 Goal 导向入口，但不复制原型中的 mock 数据与内联行为。
4. 静态图 Runtime 能力先完整交付；Planner、Foreach、Subflow 等尚未闭环的动态能力只显示经服务端声明的真实状态，不做伪实现。
5. 中文、英文、键盘操作、响应式布局、错误恢复和大数据量性能均进入发布 Gate。

### 1.1 非目标

- 不把 HTML 原型直接挂到生产路由。
- 不在浏览器中重放 Event、推导 Join/Retry 状态或建立第二套工作流状态机。
- 不为了还原原型而新增 Goal 领域聚合；Goal 是用户视角下的 Run/Workflow 组合视图。
- 不在本轮实现通用低代码工作流编辑器、任意 DAG 画布或历史时点 Runtime Overlay。
- 不在后端尚未可达时用静态卡片宣称 Planner、Foreach、Subflow 已可用。

## 2. 不可破坏的实现原则

| 原则 | 实施约束 |
|---|---|
| 服务端投影是唯一事实源 | 页面不得直读数据库、解析日志或从 Event 自建状态。 |
| 命令由服务端授权 | 按钮只渲染 `allowed_commands[]`；method、href、payload schema、Expected Version 原样使用。 |
| Actor 感知 | 列表排序、待办数量、可见字段和可执行动作必须使用当前 Actor 的服务端结果。 |
| Definition 与 Overlay 分离 | Workflow/Plan Definition 版本和当前 Runtime Overlay 在 UI 上明确标识，不能伪装成历史快照。 |
| 渐进式替换 | 在现有 `/ui` 内按页面替换，不保留两个长期并行的生产 UI。原型只作为验收参照。 |
| 无构建优先 | 延续当前原生 ES Module 架构；只有当组件复杂度和性能证明确有需要时再引入构建链。 |
| 安全默认 | Token、Secret、Planner Raw Response 不写入 URL、浏览器持久化、日志或 analytics。 |
| 能力显式降级 | 用 capability/empty-state 表达后端未提供的功能，不构造演示数据。 |

## 3. 当前基线与差距

### 3.1 已有生产能力

- `/ui` 已具备 Run 列表、Run 详情、Timeline、Data、Plan、Inbox、部分 Ops 和 New Run 入口。
- `/api/v1` 已挂载，已有分页、稳定错误、Expected Version、幂等键和 AllowedCommand 基础契约。
- HumanTask、Budget、Cancel、Recovery 等部分命令已有浏览器或 API 测试。
- Plan Definition、当前 Overlay 和 Plan Diff 已有初步界面。
- 中英文词条、基础响应式样式和静态资源测试已存在。

### 3.2 开工前必须收口的问题

| 编号 | 问题 | 影响 | 收口标准 |
|---|---|---|---|
| B1 | 只读 Actor 在 responsibilities/inbox 中仍可能收到写命令 | UI 会展示最终 403 的按钮 | Read Model 生成命令前完成 Actor 授权，权限矩阵测试覆盖。 |
| B2 | Inbox 目前主要是 HumanTask | 与原型的统一责任收件箱不一致 | **P0 只冻结契约**：定形 human、budget exhausted、unknown/recovery 的 `InboxItem`、Actor-aware count 与 golden；实现归 API-3/P5。 |
| B3 | RunSummary 信息不足或排序语义未完全稳定 | Home/Goals/Runs 需要 N+1 或前端推断 | **P0 只冻结契约**：定形 wait reason、budget、requires_actor_action、搜索/过滤、稳定排序与 golden；实现归 API-1/P2。 |
| B4 | Human JSON 输入异常可能逃逸异步回调 | 对话框无可定位错误 | 表单内捕获、定位字段、保留输入并允许重试。 |
| B5 | UI 契约文档仍含旧 `/workflow-ui`、旧详情端点描述 | 实现人员可能按过期路由开发 | 以实际 `/ui` 和当前 API 为基线修订姊妹文档。 |
| B6 | 无增量 live 更新、虚拟列表 | 长 Timeline/Data 与高频状态变化体验差 | 先基于 cursor 轮询，达到阈值使用虚拟列表；SSE 后续替换传输层。 |
| B7 | favicon 404 等浏览器控制台噪音 | 掩盖真实前端错误 | 发布 E2E 要求核心流程控制台零 error。 |

## 4. 原型到生产页面映射

| 原型页面 | 原型意图 | 当前生产状态 | 目标交付 | 关键后端依赖 |
|---|---|---|---|---|
| Home | 总览、注意事项、Goal 进度、快捷入口 | 缺失 | Actor-aware dashboard：待处理、活跃/等待/失败、最近 Goal、最近 Artifact、快捷创建 | Dashboard Summary 或由有限的分页摘要组合；禁止逐 Run 查询 |
| Goals | Goal 列表 + 选中 Goal 详情和执行计划 | 缺失 | Run 的目标化视图；支持状态/Owner/Workflow 过滤，右侧 Why-first 摘要 | 扩展 RunSummary、Goal display metadata、responsibility summary |
| Workflows | Workflow 目录与选型 | 目录仅用于 New Run | 目录卡片、版本、状态、输入摘要、查看定义、从授权命令启动 | Workflow Catalog、Definition Read、`run.start` AllowedCommand |
| Runs | 运行列表和深度详情 | 部分完成 | 搜索/过滤/排序、Why 横幅、Overview/Timeline/Plan/Graph/Data/Errors | RunSummary、Responsibility、Graph、独立分页 DTO |
| Inbox | 跨 Run 待办与直接处理 | 仅 HumanTask 为主 | Human、Budget、Unknown/Recovery 统一队列；处理后从新投影确认结果 | Inbox 聚合、Actor-aware AllowedCommand、稳定错误语义 |
| Artifacts | 跨 Run 产物浏览 | Run 内 Data/lineage 部分存在 | 当前 Actor 可见的跨 Run 列表、类型/来源筛选、预览元数据、Lineage、授权下载 | Artifact Summary、ACL、Lineage、受控 Blob read |
| Agents | Agent 注册和活动状态 | 缺失 | 注册表只读视图：handler/capability、注册状态、版本、最近 attempt 聚合；没有心跳事实时不显示 online/healthy | Agent/Handler 注册事实、按 handler 聚合的最近 attempt |
| Ops | Runtime 健康、恢复、容量 | 部分 recovery/health | Recovery、Integrity、Capacity、Durable 四区；危险操作二次确认 | Ops ACL、Recovery Registry、integrity/capacity/durable DTO |
| Settings | 语言、主题和运行参数入口 | 仅语言等局部能力 | 外观/语言/刷新频率、本地无敏感偏好；服务端设置只读或授权修改 | Runtime capability/config Read Model（若允许修改则返回命令） |
| New Goal | 四步选择 Workflow、输入、Review、Start | New Run 对话框较简 | 保留四步向导；目录加载失败与 workflow 无效分开提示；Review 显示真实版本和输入 | Workflow Catalog、JSON Schema/输入描述、`run.start` AllowedCommand |

## 5. 目标信息架构与路由

保持单一 `/ui` SPA，并使用可复制、可后退的 hash 路由：

```text
/ui/#/home
/ui/#/goals
/ui/#/goals/{run_id}
/ui/#/workflows
/ui/#/workflows/{workflow_id}
/ui/#/runs
/ui/#/runs/{run_id}/{tab}
/ui/#/inbox
/ui/#/artifacts
/ui/#/artifacts/{artifact_id}
/ui/#/agents
/ui/#/ops/{section}
/ui/#/settings
```

默认入口为 Home。旧的 `#/runs`、`#/runs/{id}` 链接保持兼容并重定向到新路由形状。URL 不包含 submission token、Secret、命令 payload 或敏感筛选值。

### 5.1 页面层级

```text
App Shell
├─ Discover：Home / Goals / Workflows
├─ Operate：Runs / Inbox / Artifacts
├─ Admin：Agents / Ops / Settings
└─ Global：Search / Actor / Locale / Theme / New Goal
```

小屏幕将左侧导航折叠为 drawer；Run/Goal 主从布局改为列表 → 详情两级导航，不能仅靠横向压缩保留双栏。

## 6. 前端结构规划

在不引入构建链的前提下，将当前单体 `app.js` 拆成职责清晰的 ES Modules：

```text
src/orbit/static/workflow-ui/
├─ index.html
└─ assets/
   ├─ app.js                 # boot 与组合
   ├─ router.js              # hash 路由、返回、深链
   ├─ api.js                 # read、cursor、AllowedCommand 执行
   ├─ state.js               # 仅临时 UI 状态，不保存领域状态
   ├─ i18n.js
   ├─ format.js
   ├─ components/
   │  ├─ app-shell.js
   │  ├─ data-state.js       # loading/empty/error/stale
   │  ├─ command-dialog.js
   │  ├─ responsibility.js
   │  ├─ virtual-list.js
   │  └─ plan-view.js
   ├─ views/
   │  ├─ home.js
   │  ├─ goals.js
   │  ├─ workflows.js
   │  ├─ runs.js
   │  ├─ run-detail.js
   │  ├─ inbox.js
   │  ├─ artifacts.js
   │  ├─ agents.js
   │  ├─ ops.js
   │  └─ settings.js
   └─ styles/
      ├─ tokens.css
      ├─ shell.css
      ├─ components.css
      └─ views.css
```

拆分按阶段进行，不先做一次性大重写。`api.js` 继续是唯一网络边界；View 不直接 `fetch`。组件接收 DTO，不接收数据库字段或原始 Event 表行。

## 7. 分阶段交付计划

工期为单名前端主力与后端协作的工程估算，不含尚未完成的 Planner/Foreach/Subflow Runtime 本体。每阶段可独立合并，但必须通过本阶段 Gate 才进入下一阶段。

### P0：基线与契约收口（3–5 人日）

交付：

- 冻结本规划、页面清单，以及 B2/B3 的 DTO、查询语义和 golden。**P0 的 golden 指冻结的 JSON Schema 文件加样例 fixture 的序列化校验（fixture validate against frozen schema），不打真实端点**；真实端点的 golden 归各 API 工作包。B2/B3 的投影实现分别归 API-3/P5 与 API-1/P2，不计入 P0。
- 修复 B1、B4、B5；为当前 Human token/命令流程补齐错误态。
- 新增 UI capability 描述，页面可区分“空数据”“无权限”“服务未提供”。
- 修订两份 UI 姊妹文档的部署路由和实际端点。
- 拍板视觉回归基建：使用现有 Python Playwright + 固定 Chromium 版本截图，基线纳入 `tests/visual_baselines/`；固定时区、locale、字体、数据 fixture 和 viewport，关闭 animation/transition，以 `max_diff_pixel_ratio=0.001` 吸收抗锯齿抖动。基线只能通过显式 update 命令更新，并在 PR 中审阅图片 diff。

Gate：B2/B3 schema/golden 通过但不要求投影已实现；权限矩阵、401/403/409、目录不可用、Human 输入错误均有 API + 浏览器测试；视觉测试能在固定 Chromium 下稳定复现；页面不出现未经授权命令。

### P1：Design System 与 App Shell（3–4 人日）

交付：

- 从原型提取颜色、间距、字体、状态、卡片、表格、按钮和对话框 token。
- 建立 Discover、Operate、Admin 三组导航结构并实现 Actor、语言、主题、响应式 drawer；对应页面交付前不显示空分组或死入口，因此 P1 只显示已有页面所在的 Operate/Admin，Discover 随 P2/P3 页面启用。预留全局搜索的路由/布局槽位；API-1 搜索可用前不展示可操作的搜索入口。
- 建立 loading/empty/error/stale/pending 五种通用状态和 toast/live-region。
- 拆出 router、i18n、command dialog 与基础 CSS。

Gate：中英文无截断；键盘可遍历导航/对话框；状态不只依赖颜色；360px、768px、1280px 视口通过视觉回归。

### P2：Home、Goals 与 Runs 列表（4–6 人日）

交付：

- Home dashboard 对齐原型的 attention、Goal progress、recent activity。
- Goals 主从视图复用 RunSummary 和 Why 摘要，不新增客户端领域状态。
- Runs 支持搜索、状态/责任筛选、Actor-action 优先排序和稳定 cursor 分页。
- 接通全局搜索：调用 API-1 的 `q`/filter 查询并导航到服务端结果，不在浏览器扫描全量数据。

Gate：首屏无 N+1；分页稳定；无权限 Actor 看不到写按钮；空/慢/失败/大列表可定位且可恢复。

执行记录（2026-07-19）：API-1 已交付 Actor-aware dashboard、RunSummary 2.0、`q/status/responsibility/active` 服务端查询、action-first 稳定 cursor；Run 创建事件与投影持久化 `goal/display_name`。生产 UI 已启用 Home、Goals 主从/深链、Runs 筛选和全局搜索，查询均经 `api.js`，不存在浏览器全量扫描。API、JS module、Browser E2E 和三档视口/双主题视觉回归通过；P2 新增 Goals/Runs 桌面双主题基线。Home 的 Goal progress 以真实状态聚合呈现，不推算未由 Runtime 提供的百分比。

### P3：Workflows 与四步 New Goal（3–5 人日）

交付：

- Workflow Catalog 卡片和详情抽屉，显示真实 latest version、定义摘要和启动权限。
- 四步向导：Select Workflow → Inputs → Review → Start。
- 输入基于后端 schema/描述渲染；不支持的 schema 降级为受校验 JSON 编辑器。
- Catalog 网络失败、无权限、空目录、workflow 已失效分别提示。

Gate：启动命令只来自 Catalog 的 `run.start`；重复点击保持幂等；409 后刷新并要求重新确认；启动成功导航到新 Run。

执行记录（2026-07-19）：API-2 已交付包含名称、说明、labels、入口节点 input schema、图摘要与 Actor-aware `run.start` 的 Catalog，以及按不可变版本读取 Definition 的详情端点。生产 UI 已启用 Workflows 卡片/详情和 Select Workflow → Inputs → Review → Start 四步 New Goal；简单 schema 生成字段，复杂或未知 schema 降级为完整 JSON 对象编辑并在本地与 Runtime 双重校验。最终提交前重新读取 Catalog；workflow 消失、权限丢失或版本变化均阻止 mutation，版本变化/409 会关闭旧复核并要求重新开始。API、JS、Browser E2E、打包和 Workflows/向导双主题视觉回归覆盖相应 Gate。

### P4：Run 详情与静态 Graph（6–9 人日）

交付：

- Why 横幅、预算、责任和授权动作成为首屏。
- Overview、Timeline、Plan、Graph、Data、Errors 分离加载与独立错误边界。
- Plan Definition 版本切换、Current Overlay 标识和 Diff。
- 静态图先实现线性 plan outline；存在并行/join 投影时使用局部 Graph。
- Timeline/Data 使用 cursor 增量加载和虚拟列表。

Gate：前端不重放 Event；Definition vN 不被标为历史运行快照；长 Timeline/大 Value 不撑爆页面；直接深链和刷新可用。

执行记录（2026-07-19）：API-4 已交付 Run Summary、Why/预算/责任首屏，以及独立的 Timeline、Plan、Graph、Data、Errors 投影端点。Plan Definition、Current Overlay 和版本 Diff 分开读取并携带明确的 plan version；Graph 使用服务端静态布局与 branch/join/counter 投影，客户端不重放事件。Timeline/Data 采用稳定 cursor 增量加载，并以有界虚拟窗口限制 DOM；大 Value 按摘要和 `size_bytes` 截断。Run 深链、刷新、错误边界、计划定义/叠加分离和数据血缘已由 API、JS module 与 Browser E2E 覆盖；双语 P4 文案已补齐。

### P5：Inbox 与 Steering（4–6 人日）

交付：

- 聚合 Human、Budget、Unknown/Recovery，角标与列表来自同一 Actor-aware 投影。
- 通用 Command Dialog 按 payload schema 收集输入，提交后显示 pending，直到新投影确认。
- Human 一次性 token、追加预算、取消、Recovery takeover/apply 使用统一冲突处理。
- 会签、deadline、责任来源与目标 Run 可追溯。

Gate：无硬编码 mutation URL；命令完成前不乐观显示成功；token 不进入 URL/storage/log；重复、过期、无权限和部分失败均有 E2E。

执行记录（2026-07-19）：API-3 已将 Human、预算耗尽、未知结果和 Recovery finding 合并为同一稳定 cursor 的 InboxItem 2.0 投影，列表和 Actor-aware `action_count` 来自同一份结果。Human AllowedCommand 采用与 HumanTask token 服务一致的 participant/assignee/claimer/creator 权限，按 approval/input kind 广告决定语义；安全 finding 提供 Apply，非安全 finding 提供显式 takeover。Run cancel 的 Expected Version 现在来自 Run 聚合事件序列，而不是会被 Human/Budget 投影推进的 `workflow_runs.aggregate_version`，从而保证服务端广告的取消命令可直接执行。前端显示 deadline、quorum、来源和目标 Run，支持分页，并在命令投影确认前显示 pending、逐项报告 Recovery 部分失败。token 输入默认遮挡且不会进入 URL 或 localStorage；未知 payload schema 拒绝提交。Browser E2E 已通过真实 HTTP 覆盖相同 idempotency key 的重复投递只生效一次、过期 Human recovery、命令广告后权限被撤销仍在提交时返回 403、旧 Budget command 返回 409 后刷新，以及 finding 已被其他操作者应用后旧选择显示逐项 stale/部分失败；P5 Gate 已收口。

### P6：Artifacts 与 Lineage（3–5 人日）

交付：

- 当前 Actor 可见集合内的跨 Run Artifact 列表、筛选、元数据预览和生产者/消费者 lineage；列表查询本身必须经过 `artifact_acl`。
- 大值只展示摘要、类型、`size_bytes` 和显式加载操作；文本预览有上限。
- 下载/读取经过 ACL；已获 Artifact 元数据访问权的 Actor 可以区分 Blob 缺失与完整性失败，但未授权 Artifact 和不存在 Artifact 的公开响应不可区分。

Gate：列表只返回当前 Actor 经 `artifact_acl` 授权的集合；直接按 ID 访问使用同一 ACL，未授权与不存在对调用方返回不可区分的响应，不能借此枚举其他 Run 的 Artifact；大 Artifact 不进入 DOM；lineage 深链和错误态有 API/E2E 测试。

执行记录（2026-07-19）：API-5 已交付 Actor-scoped 跨 Run Artifact 列表、`q/run_id/content_type` 筛选、详情、lineage、文本预览和下载端点。列表、直接 ID、lineage、Run Data 的 Artifact 分支与内容读取均先经过 `artifact_acl`；未授权、未提交与不存在统一返回相同 `artifact_not_found`，只有已授权 Actor 可以看到 `blob_missing`、完整性失败、非文本或超出 64 KiB 预览上限等具体错误。Run owner 作为持久 Artifact subject 写入，后续 Artifact commit 与初始 read grant 同一 UoW，启动时已提交的 ingress Artifact 会补授予。下载使用同一文件描述符先分块校验、rewind、再分块发送，保持 O(1) 内存并在发送响应头前给出完整性结论；断连也保证关闭描述符。ACL grant 与 metadata/lineage/content read 均留下审计，拒绝记录只保存 Artifact 引用哈希。生产 UI 已增加 Artifacts 导航、分页筛选、卡片、元数据详情、显式预览/下载和 lineage 深链刷新；大内容不进入列表 DTO 或 DOM。API 不可枚举矩阵、Blob 缺失、预览上限、流式下载、迁移和 Browser 深链测试通过，Artifacts 桌面双主题视觉基线已纳入回归。

### P7：Agents、Ops 与 Settings（4–7 人日）

交付：

- Agents 展示注册 Handler/Agent、capability、注册状态和按 handler 聚合的最近 attempt（时间、结果、run）；启动时 discovery 不是心跳，没有 `last_seen` 前不得标记 online/healthy。
- Ops 完成 Recovery、Integrity、Capacity、Durable；只为已注册且安全的 action 显示 Apply。
- Settings 提供 locale/theme/刷新间隔等无敏感偏好；服务端配置默认只读。
- cursor polling 提供 live 更新；后端 SSE 就绪后保持 View API 不变替换传输层。

Gate：Ops 独立 ACL 和二次确认；逐 finding 结果可审计；刷新无需修库；控制台无 error；Agents 仅显示注册事实与可追溯 attempt 聚合，不把注册或最近执行冒充实时健康。

执行记录（2026-07-19）：API-6 已为 Handler Catalog 增加显式 `registration_status`、`registration_only` 状态语义，以及从 durable job/attempt 投影的最近 attempt（run、node、结果、时间）；没有 heartbeat/`last_seen`，生产页面明确声明不能据此判断 online/healthy。Ops 使用独立 `runtime.ops.read/write` scope，Recovery 只向具备 Ops write 的 Actor 广告服务端 AllowedCommand，继续通过显式确认、action-id CAS、逐 finding 结果和审计执行；Integrity 使用有界 SQLite quick-check，Capacity 只展示配置 Worker 与当前 durable queue 计数，并把没有持久容量报告显式标为 unavailable，Durable 展示 jobs/timers/leases/unknown-result 持久事实。Settings 的 locale/theme/刷新间隔保存在浏览器，服务端配置只读且受 Ops read 保护。`/api/v1/live` 提供 opaque change cursor；客户端隐藏或有对话框时暂停轮询，只在游标变化后复用现有 View API 刷新。Agents/Ops/Settings 路由、双语、API ACL/cursor、Browser E2E、console audit 与桌面双主题视觉基线均已覆盖。

### P8：动态能力界面（已完成）

只有对应 Runtime 与 Read Model 完成后才能启用：

| 能力 | 启用条件 | UI 交付 |
|---|---|---|
| Planner Decision | Planner proposal/attempt/policy/budget 可生产到达，Decision DTO 冻结 | 结构化 proposal、accepted/rejected、成本、来源；不默认展示 raw response |
| 动态 Plan Patch | Patch 的来源、约束、diff 和可变边界已记录 | vN→vN+1 diff、来源徽章、锁定已执行节点 |
| Foreach | materialization、item retry/budget/unknown、恢复与分页 API 闭环 | 虚拟 item 网格、进度、失败策略、item scope |
| Subflow | 创建/传播/恢复竞态、ACL transfer 与链接 API 闭环 | 父子 breadcrumb、传播策略、独立子 Run 详情 |
| 历史 Overlay | Snapshot + Event 的服务端历史投影 API 完成 | `as_of_global_position` 时间点回看；不由浏览器重建 |

Gate：每项必须有生产可达 E2E；仅有数据库表或 application service 手工造数据不算完成。

执行进展（2026-07-19）：历史 Overlay 已由服务端按
`as_of_global_position` 重放；静态 `agentic` 节点可生产创建 Planner attempt，
`finish/fail` 通过 Kernel fence 收口，`dispatch` 只能替换声明的 pending
占位节点并经过精确 Handler、端口、能力、Policy 与 PlanVersion CAS 校验。
Planner Decision API 不返回 raw response、lease 或 action 输入值。静态
`foreach` 以固定版本子 Run 执行 item，已覆盖并发上限、真实 worker、重启恢复
与稳定聚合；静态 `subflow` 已覆盖同事务启动/链接、终态恢复与父取消传播。
Planner dispatcher 现在依据静态 agentic 节点必填的 `cost_microunits` 在调用前
预留 Run 预算，provider usage 回来后累计报账并结算；没有剩余预算不会调用模型，
进程恢复会收口已落响应但未结算的 reservation。以上链路均有
published-workflow E2E。Foreach 现在为每个 item 从父 Run 原子预留预算，创建
独立子 Run 账户，并在成功、失败或取消时按子账户实际消耗结回父账户；转移与
child Run 创建处于同一事务。Subflow Artifact 输入依据父 Run 既有 read ACL
计算授权主体交集并传给子 Run，返回 Artifact 同步进入 link scope；未经授权的
主体无法借 transfer 扩权，正反路径均有 E2E 与审计。Foreach item API 与 UI
已使用 cursor 分页，
浏览器以最多 200 行的有界虚拟网格持续加载大列表。Foreach/Subflow capability
均返回真实可用状态。至此 P8 Gate 关闭。

### P9：发布加固（3–5 人日）

交付：

- 全页面双语、可访问性、响应式和视觉回归。
- 真实 `orbit serve` 包安装 smoke；刷新、重启、旧链接和 SPA fallback 验证。
- 容量测试、故障注入、浏览器 console/network 审计。
- 移除已被替代的旧 View/CSS 与未引用词条；更新 release notes 和操作手册。

Gate：满足 §10 Definition of Done，静态图范围才可标记 Runtime UI 完成；P8 能力单独声明状态。

执行记录（2026-07-19）：九个一级页面和四步向导的中英文、浅/深主题、键盘焦点恢复、移动导航、360px 横向溢出及全页面 console error 已由真实 Chromium 覆盖；视觉基线覆盖 Shell 三档 viewport/双主题、九个一级页面桌面双主题，以及向导、Run Detail、空 Inbox 和 503 错误态。容量用例分页读取 1,000 Runs 和 10,000 Timeline events，并固化大 Value 在进入 DOM 前截断且保留 `size_bytes`；断网、503、409、权限竞态、重启恢复和 stale cursor 均有定位/恢复路径。clean-install Gate 从 wheel 安装到空 venv，启动安装产物中的 `orbit serve` 并读取 `/ui`、SPA 入口、打包 ES module 和真实 `/api/v1`。旧 `.DS_Store`、被替代词条和过期打包描述已移除，release notes 与操作手册已同步。当时的静态范围验证为 Python 全套 800 tests、Node client modules 20 tests、compile/JSON/diff clean 全部通过；其后 P8 动态 Gate 的关闭记录见上一节。

## 8. 后端 Read/API 工作包

前后端可以并行，但 UI 页面不得先绑定临时表结构。

| 工作包 | 最小输出 | 消费页面 |
|---|---|---|
| API-1 Dashboard/RunSummary | Actor-aware count、wait reason、budget、requires_actor_action、opaque cursor；Run 查询至少支持 `q`、`status`、`responsibility`，其中 `q` 匹配规范化的 display name、run id、workflow id，过滤与排序在分页前执行 | Home、Goals、Runs、Global Search |
| API-2 Workflow Catalog | version、summary/input schema、Definition read、`run.start` command | Workflows、New Goal |
| API-3 Responsibility/Inbox | 多责任类型聚合、Actor-aware AllowedCommand、deadline/quorum | Home、Run、Inbox |
| API-4 Run Detail | summary/timeline/errors/data/plan/graph 独立分页与 projection version | Run Detail |
| API-5 Artifact | 当前 Actor 经 `artifact_acl` 授权的 cross-run summary；直接 ID read 使用同一 ACL 和不可枚举错误语义；lineage、受控 content read | Artifacts、Run Data |
| API-6 Registry/Ops | handler/agent 注册事实；按 handler 从现有 attempts 投影最近 attempt 的时间、结果和 run；不提供虚构 heartbeat/last_seen；另含 integrity/capacity/durable、repair actions | Agents、Ops |
| API-7 Capability | 页面/功能可用性与降级原因 | App Shell、所有 empty state |

每个工作包必须提供 schema/golden、权限矩阵、分页稳定性和错误 envelope 测试。前端不得通过 endpoint 404 猜测 capability。阶段估算已包含对应实现：API-1 计入 P2，API-2 计入 P3，API-3 计入 P5，API-4 计入 P4，API-5 计入 P6，API-6/7 计入 P7/P1；这些工作包不是需要在 P0 额外完成的一轮实现。

## 9. 测试与验收策略

### 9.1 自动化层级

| 层级 | 覆盖 |
|---|---|
| Static asset | 所有资源可打包、无内联 mutation、词条 key 完整、路由 fallback 正确 |
| JS module | router、format、cursor、dialog validation、command conflict、virtual list |
| API contract | schema/golden、Actor ACL、AllowedCommand、401/403/409、opaque cursor |
| Browser E2E | 两种语言；New Goal；Run Why；Human/Budget/Recovery；Artifact lineage；Ops apply |
| Visual regression | Home、Goals、Runs、Inbox、向导；三档视口；浅色/深色；关键空/错状态 |
| Accessibility | keyboard、focus restore、dialog trap、landmark、live region、contrast、非颜色状态 |
| Capacity/fault | 10k Timeline、1k Runs、大 Value、慢请求、断网、重启、stale projection |
| Package smoke | 从构建产物启动 `orbit serve`，访问 `/ui` 和真实 `/api/v1` |

### 9.2 视觉回归基建

- **工具**：复用 `tests/test_browser_e2e.py` 的 Python Playwright/Chromium 环境；用 Playwright 截图断言覆盖页面或稳定组件区域，不引入第二套浏览器驱动。
- **基线位置**：PNG 基线提交到 `tests/visual_baselines/`，按页面、主题和 viewport 命名，例如 `home-dark-1280x800.png`。
- **确定性**：使用固定 fixture、UTC 时间、固定 locale/字体/Chromium；隐藏 cursor，禁用 animation、transition 和随机 ID/相对时间。
- **参考平台**：字体栅格化差异跨 OS 远超容差，基线只在单一参考平台生成（基线元数据记录 OS/Chromium 版本）；非参考平台运行视觉测试自动 skip 并提示，不阻断也不静默通过。引入 CI 后参考平台切换为固定容器镜像，切换时整套基线一次性重生成。
- **容忍策略**：固定 CI 环境下 `max_diff_pixel_ratio=0.001`；文本和布局变化不得通过放宽全局阈值掩盖，应缩小断言区域或稳定数据源。
- **更新策略**：默认测试只比较；基线更新必须使用显式 update 开关，生成的 expected/actual/diff 图片作为 PR artifact，修改后的 baseline 随代码审阅并提交。
- **职责**：P0 建好最小 harness 和一张原型基线；P1 扩到 Shell、三档 viewport 和双主题；P9 覆盖全部关键页面、empty/error/dialog 状态。

### 9.3 原型一致性验收

原型不是像素级规范。每个页面使用以下顺序评审：

1. 信息层级和用户任务是否与原型一致。
2. 视觉 token、密度、主从布局和状态语言是否一致。
3. 交互是否由真实投影和 AllowedCommand 驱动。
4. 原型中不满足安全、可访问性、响应式或 Runtime 契约的行为，以生产约束为准，并在 PR 中记录差异。

## 10. Definition of Done

静态图 Runtime UI 只有同时满足以下条件才可宣布完成：

- 原型的 9 个一级页面和 4 步向导均由真实数据驱动，不存在长期 mock 数据。
- 当前 Actor 不可执行的动作不显示；服务端仍在提交时重验权限和版本。
- 所有写操作经 `allowed_commands[]`、Idempotency-Key 和 Expected Version。
- Home/Goals/Runs/Inbox 的责任数量和排序来自同一 Actor-aware 语义。
- Run 第一屏回答“为什么在等、我能做什么”，并明确 Plan Definition 与 Runtime Overlay。
- Timeline、Data、Artifact 和长列表分页/虚拟化；无首屏全量历史读取。
- 401、403、409、404、422、503、断网和 stale projection 有可定位、可恢复反馈。
- 中英文、浅/深色、键盘和三档视口通过自动化与人工 Gate。
- Secret、token、raw response 不进入 URL、storage、console 或 analytics。
- `orbit serve` 从发布包启动后所有核心 E2E 通过，浏览器控制台零 error。
- 文档、release notes、页面 capability 和代码事实一致。
- Planner、Foreach、Subflow、历史 Overlay 分别标记真实完成度；未满足 P8 Gate 的能力不计入完成率。

## 11. 建议提交序列

为降低回归和评审成本，按以下边界提交：

1. `docs(ui): freeze prototype delivery plan and route baseline`
2. `fix(ui-api): make summaries and commands actor-aware`
3. `refactor(ui): introduce shell router and design tokens`
4. `feat(ui): add home goals and workflow catalog`
5. `feat(ui): deliver goal wizard and run detail`
6. `feat(ui): unify inbox steering and artifact lineage`
7. `feat(ui): add agents ops settings and live refresh`
8. `test(ui): add visual accessibility capacity and package gates`
9. 动态能力按 Planner、Foreach、Subflow 分别提交，不与静态 UI 发布混合。

每个提交只在相关自动化通过后合并；不把大规模 CSS 重写、API 形状变更和新页面堆在同一个提交中。

## 12. 估算与里程碑

不含 P8 动态 Runtime 能力，P0–P7 加 P9 合计约 **33–52 人日**。建议以三次可发布里程碑管理：

| 里程碑 | 阶段 | 可交付结果 |
|---|---|---|
| M1：可发现 | P0–P3 | 完整 Shell、Home/Goals/Workflows、真实 New Goal |
| M2：可理解与介入 | P4–P5 | Why-first Run、Plan/Graph、统一 Inbox 和安全 Steering |
| M3：可审计与发布 | P6–P9 | Artifact、Agents/Ops/Settings、性能/可访问性/发布 Gate |

P8 作为独立里程碑，在 `docs/agentic-workflow-implementation-plan.md` 对应 Runtime Gate 关闭后排期。若只有一名前端工程师，按 P0→P9 顺序执行；若前后端并行，优先并行 API-1/2/3 与 P1，但不要并行开发依赖未冻结 DTO 的 View。
