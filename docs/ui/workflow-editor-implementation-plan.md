# Workflow 编辑功能实施规划

> 文档状态：P0–P5 已交付（2026-07-20）
> 基线日期：2026-07-20
> 适用范围：Orbit Runtime `/ui`、`/api/v1`、Workflow DSL 与 SQLite 持久化
> 相关文档：[`runtime-ui-prototype-delivery-plan.md`](runtime-ui-prototype-delivery-plan.md)、[`workflow-prompt-authoring.md`](workflow-prompt-authoring.md)、[`agent-workflow-ui-api-contract.md`](agent-workflow-ui-api-contract.md)

## 1. 决策摘要

Orbit 的 Workflow 编辑语义定义为：

> 从一个不可变的已发布 WorkflowVersion 创建持久化 Draft，在 Draft 上编辑、保存和校验；确认后发布为新的不可变 WorkflowVersion。

禁止原地修改、覆盖或删除已发布版本。已经创建的 Run 继续引用启动时固定的 `workflow_id + workflow_version + definition_hash`；新 Goal 默认选择最新已发布版本。因此编辑和发布新版本不会改变正在运行或历史 Run 的行为。

首个可发布版本采用“DSL 编辑器 + 持久化 Draft + 服务端校验 + 发布新版本”，不把拖拽 DAG 画布设为首发依赖。结构化节点、Agent、边和 Policy 编辑在同一 Draft 模型上增量交付。

## 2. 目标与非目标

### 2.1 目标

1. 从最新或指定历史版本创建可恢复的编辑草稿。
2. 支持刷新浏览器或重启 Runtime 后继续编辑，无需修库。
3. 编辑过程始终使用生产 DSL 编译器、Handler Catalog 和 Schema Catalog 校验。
4. 发布时生成新版本，并用乐观并发阻止覆盖别人已经发布的更新。
5. 所有写操作来自服务端 `allowed_commands[]`，携带幂等键和 Expected Version。
6. 错误能定位到字段、JSON Path、节点或边，并保留用户尚未通过校验的输入。
7. 中文、英文、键盘操作、窄屏布局、错误恢复和视觉回归进入发布 Gate。

### 2.2 非目标

- 不修改或删除 `workflow_versions` 中的历史记录。
- 不让浏览器实现第二套 DSL 编译器或图语义校验器。
- 首发不实现任意 DAG 拖拽、端口连线、自动布局和多人实时协同编辑。
- 首发不自动合并两个并发发布分支。
- 不允许 DSL、浏览器或生成模型提供可执行命令；Handler 仍只能按 sealed registry 中的名字和版本引用。
- 不把未保存 Draft 用作 Run 的定义；Run 只能启动已发布版本。

## 3. 当前代码事实与缺口

### 3.1 已有能力

| 能力 | 当前事实 |
|---|---|
| 不可变版本 | 数据库触发器拒绝 `workflow_versions` 的 UPDATE 和 DELETE。 |
| 新版本发布 | `SQLiteWorkflowVersionStore.publish` 事务内分配下一版本。 |
| 内容幂等 | 相同 Definition Hash 返回已有版本，即使 Expected Version 已过期。 |
| 乐观并发 | 新内容只有在 `expected_latest_version` 等于当前最新版本时才能发布。 |
| 无状态校验 | `POST /api/v1/workflows/validate` 编译 source，但不保存。 |
| 发布 API | `POST /api/v1/workflows/{workflow_id}/versions` 编译、核对 ID 后发布。 |
| 版本读取 | Workflow Catalog 可读取最新或指定版本的 canonical IR。 |
| 局部编辑 | 生成工作流对话框可以修改 Agent、重新校验并发布。 |

### 3.2 实质缺口

| 缺口 | 影响 |
|---|---|
| 没有 `workflow_drafts` | 刷新、崩溃或重启后编辑内容丢失。 |
| Detail 不返回原始 DSL source | canonical IR 与作者 DSL 不是同一种契约，不能直接往返编辑。 |
| Workflow Detail 只广告 `run.start` | UI 没有服务端授权的 Edit/Create Draft 入口。 |
| Validate/Publish 仍围绕一次性 source body | 缺少 Draft revision、自动保存、恢复与审计。 |
| 无专用冲突错误 | UI 只能把发布冲突当通用 409，无法提供明确恢复路径。 |
| UI 只有目录和生成弹窗 | 没有可深链、可返回、可恢复的 Editor 页面。 |

## 4. 不可破坏的领域原则

| 原则 | 实施约束 |
|---|---|
| Published Version 不可变 | 编辑永远发生在 Draft；发布永远 INSERT 新版本。 |
| Runtime 只运行已发布定义 | `run.start` 不接受 `draft_id` 或未发布 source。 |
| 服务端是事实源 | Draft revision、校验状态、Definition Hash 和发布版本均来自服务端。 |
| 命令由服务端授权 | Save、Validate、Publish、Discard 按钮只读 `allowed_commands[]`。 |
| 编译器是唯一校验权威 | 浏览器只做 JSON 解析、必填和尺寸等即时提示，不判断图语义。 |
| Source 与 IR 分离 | 作者编辑 DSL source；服务端编译为 canonical IR 并计算 Definition Hash。 |
| 冲突不静默覆盖 | 草稿 revision 冲突和发布 base version 冲突均返回 409，不做 last-write-wins。 |
| 失败保留输入 | 校验或发布失败不能清空、替换或丢弃 Draft source。 |

## 5. 用户流程

### 5.1 编辑最新版本

1. 用户在 Workflows 详情点击服务端广告的“编辑”。
2. Runtime 为当前 Actor 创建或恢复该 Workflow 的活动 Draft。
3. Draft 记录 `base_version`，初始 source 来自该版本的作者 DSL。
4. 用户编辑，UI 自动保存并显示 `Saving / Saved / Conflict / Offline`。
5. 用户执行 Validate；服务端保存结构化 diagnostics 和校验后的 Definition Hash。
6. 只有 Draft 最新 revision 已校验且 source 未再变化时，服务端才广告 Publish。
7. Publish 以 `base_version` 作为 `expected_latest_version`，成功即发布 `base_version + 1`（CAS 语义下不存在其它成功结果）；Draft 标记为 `published`。
8. UI 返回 Workflow Detail，并显示新版本和 Definition Hash。

### 5.2 从历史版本派生

指定历史版本不显示“修改历史”，而显示“从此版本创建草稿”。创建出的 Draft 仍属于同一个 `workflow_id`，但记录被选择的 `base_version`。发布前若最新版本已经高于 base，必须进入发布冲突流程。

**与 one-active 约束的碰撞路径**（每 Actor 每 Workflow 只有一个活动 Draft）：

- 请求创建的 `base_version` 与现有活动 Draft 的 base 相同 → 恢复该 Draft（等价于 Edit）；
- `base_version` 不同 → 返回 `409 draft_already_active`，响应携带现有 Draft 的
  `draft_id`、`base_version` 和 `updated_at`；UI 给出两个显式选择：“继续现有草稿”
  或“废弃后从 vN 新建”。服务端绝不静默丢弃或改基现有草稿。

### 5.3 冲突恢复

当用户从 v3 创建 Draft，而期间已有 v4 发布：

- Publish 返回 `409 workflow_version_conflict`；
- 响应包含 `base_version=3`、`latest_version=4`、最新版本 Definition Hash；
- UI 保留当前 Draft，并提供“查看最新版本”“复制当前草稿”“基于 v4 新建草稿”；
- 首发不自动 rebase 或 merge。

## 6. 持久化模型

新增 migration 和 `workflow_drafts`：

```sql
CREATE TABLE workflow_drafts (
    draft_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflow_definitions(workflow_id),
    base_version INTEGER NOT NULL CHECK (base_version >= 1),
    actor TEXT NOT NULL,
    source_format TEXT NOT NULL CHECK (source_format IN ('json', 'yaml')),
    source_text TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    validation_status TEXT NOT NULL CHECK (
        validation_status IN ('dirty', 'valid', 'invalid')
    ),
    validated_source_hash TEXT,
    validated_definition_hash TEXT,
    diagnostics_json TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    status TEXT NOT NULL CHECK (status IN ('active', 'published', 'discarded')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_version INTEGER,
    CHECK (draft_id LIKE 'workflow_draft:%')
);

CREATE UNIQUE INDEX workflow_drafts_one_active
ON workflow_drafts(workflow_id, actor)
WHERE status = 'active';
```

Migration 14 增加 `workflow_draft_revisions`，持久化 Agent 指令、前后 source/hash、
Definition Hash、尝试次数和 `pending/accepted/rejected/undone` 决策状态。每个 Draft
最多一个 pending 候选。

### 6.1 模型语义

- 每个 Actor 对每个 Workflow 最多一个活动 Draft；再次点击 Edit 默认恢复它。
- `revision` 是 Draft Aggregate 的 Expected Version，每次 Agent 修订、发布或废弃递增。
- `source_hash` 对当前 source 计算；只有通过编译的 Agent 修订可以改变 source。
- `validated_source_hash` 必须等于 `source_hash` 时，`valid` 才能用于发布。
- `diagnostics_json` 保存编译器诊断，不保存异常 traceback。
- Discard 为状态迁移而不是物理 DELETE，保留审计事实。
- source body 上限首发定为 256 KiB；超过返回 413。
- 校验包含在 Agent 修订漏斗中；模型输出未通过编译时不会改变 Draft。
- Agent 修订产物统一为 JSON source；`source_format` 只有 `json`/`yaml`。
- **无变化发布**：内容幂等意味着发布与 base 完全相同的 Draft 会返回既有版本号
  （`published_version == base_version`，不产生新版本）。这是成功而非错误；UI 提示
  “内容与 vN 相同，未产生新版本”，Draft 照常标记为 `published`。

## 7. Source 读取与旧版本兼容

Workflow Detail 必须区分：

```json
{
  "definition": {},
  "source": "{...}",
  "source_format": "json",
  "source_available": true
}
```

Draft 初始化 source（**首发只有两级**）：

1. 使用目标 `workflow_versions.source_text`；
2. source 缺失 → `source_unavailable`：Workflow 仍可运行和查看，但不广告编辑命令，
   不伪装为可编辑。

canonical IR → DSL 导出器**显式移出首发**：UI 与 API 发布的版本都持久化
source_text，缺失 source 的只有早期由测试或 CLI 以 `source_text=None` 发布的
版本，降级路径足以覆盖。导出器若日后引入，必须满足 round-trip Gate（导出的
DSL 经当前编译器编译后 Definition Hash 与原版本一致），且不能把
`canonical_ir_json` 直接当作 DSL source 返回——这两条作为该独立工作包自己的
Gate，不阻塞本计划。

首发 UI 只读展示 source。Agent 修订会输出格式化 JSON；若目标版本是 YAML，首次修订后
不保留 YAML 注释和排版。

## 8. API 与 AllowedCommand 契约

### 8.1 路由

```text
POST /api/v1/workflows/{workflow_id}/drafts
GET  /api/v1/workflow-drafts/{draft_id}
POST /api/v1/workflow-drafts/{draft_id}/revise
POST /api/v1/workflow-drafts/{draft_id}/accept
POST /api/v1/workflow-drafts/{draft_id}/reject
POST /api/v1/workflow-drafts/{draft_id}/undo
POST /api/v1/workflow-drafts/{draft_id}/publish
POST /api/v1/workflow-drafts/{draft_id}/discard
```

所有写路由继续经过 `ApiCommandExecutor`，要求 Actor、scope、`idempotency-key` 和 `expected_version`。

Revise 使用 `POST .../revise`，负载只接受自然语言 `instruction` 和
`expected_version`；客户端不能提交替换 source。

### 8.2 命令广告

Workflow Detail 在 Actor 有写权限且 source 可用时广告：

```json
{
  "command": "workflow.draft.create",
  "method": "POST",
  "href": "/api/v1/workflows/workflow:demo/drafts",
  "target_aggregate_id": "workflow:demo",
  "expected_version": 4,
  "payload_schema": "workflow-draft-create/1.0"
}
```

Draft Detail 根据当前状态广告以下命令子集：

- `workflow.draft.revise`
- `workflow.draft.accept`
- `workflow.draft.reject`
- `workflow.draft.undo`
- `workflow.draft.publish`
- `workflow.draft.discard`

`workflow.draft.revise` 只生成候选，不改变当前 Draft source；pending 时只广告
Accept/Reject。Accept 只能采用服务端已编译的候选，不能携带 source。Source、Metadata、
Node、Edge 和 Policy 在 UI 中都是只读投影；`save` 与 `validate` 不再作为 HTTP
路由或 AllowedCommand 暴露。最近一次 accepted 修订在当前 source 未继续变化时可 Undo。

Publish 只有在以下条件同时成立时才广告：

- `validation_status == valid`；
- `validated_source_hash == source_hash`；
- Actor 有发布权限；
- Draft `status == active`。

### 8.3 DTO

```json
{
  "draft_id": "workflow_draft:...",
  "workflow_id": "workflow:demo",
  "base_version": 4,
  "source_format": "json",
  "source": "{...}",
  "source_hash": "sha256:...",
  "validation_status": "invalid",
  "validated_definition_hash": null,
  "diagnostics": [],
  "revision": 7,
  "status": "active",
  "updated_at": "...",
  "allowed_commands": []
}
```

### 8.4 稳定错误码

| HTTP | code | UI 行为 |
|---|---|---|
| 400 | `workflow_draft_invalid` | 定位字段或 JSON Path，保留 source。 |
| 404 | `workflow_draft_not_found` | 返回 Workflow Catalog。 |
| 409 | `draft_already_active` | 展示现有草稿，提供“继续”或“废弃后新建”。 |
| 409 | `draft_version_conflict` | 显示服务端 revision，允许重新加载或复制本地文本。 |
| 409 | `workflow_version_conflict` | 显示 base/latest，进入版本冲突流程。 |
| 409 | `draft_not_validated` | 要求重新 Validate。 |
| 413 | `workflow_source_too_large` | 显示尺寸上限。 |
| 422 | `workflow_validation_failed` | 显示结构化 diagnostics，不作为通用异常。 |
| 403 | `forbidden` | 移除过期命令并重新读取 Draft。 |

## 9. 应用服务边界

新增 `WorkflowDraftApplicationService`，负责：

- `create_or_resume(workflow_id, base_version, actor)`；
- `revise(draft_id, instruction, expected_revision, actor)`；
- `publish(draft_id, expected_revision, actor)`；
- `discard(draft_id, expected_revision, actor)`。

该服务复用 `WorkflowDefinitionService.validate_workflow` 和 `publish_workflow`，不复制 DSL 编译规则。Publish 在一个明确的用例中完成：

1. 授权 Draft owner；
2. 校验 Draft revision 与状态；
3. 确认 `validated_source_hash == source_hash`；
4. 调用现有版本 Store，以 `base_version` 作为 `expected_latest_version`；
5. 成功后将 Draft 标记为 `published` 并记录 `published_version`；
6. 返回新 WorkflowVersion 和最新 Draft 投影。

若 WorkflowVersion 发布成功后 Draft 状态落库失败，恢复扫描必须能通过 Definition Hash 找到已发布版本并幂等完成 Draft 状态。不能让用户因重试发布产生额外版本。

## 10. UI 信息架构

新增深链：

```text
/ui/#/workflows/{workflow_id}/edit/{draft_id}
```

Workflow Detail 的按钮顺序：

1. 新建目标
2. 编辑／继续编辑
3. 查看历史版本

### 10.1 Editor 布局

桌面布局：

```text
┌ Header：Workflow / base version / Agent 状态 / Publish ┐
├ Prompt：自然语言修改指令                               ┤
├ Preview：只读节点摘要 / 生成源码 / Diagnostics         ┤
└ Footer：Definition Hash / Draft revision / updated_at  ┘
```

窄屏保持顺序页面：Prompt → Preview → Source → Diagnostics。

### 10.2 编辑模式

编辑器只提供 Agent Prompt。用户描述目标修改，服务端将当前 source、Handler
Catalog 和 Schema Catalog 交给 Agent，再使用生产编译器校验结果。节点、边、
Policy 和完整 DSL 仅用于核对结果，不能直接编辑。没有兼容 Reviser 时，Workflow
Detail 不广告编辑命令。

编辑器展示候选前后 source、节点预览和修订历史。候选审核按稳定 ID 汇总节点、连线和
工作流属性的语义变更，完整 source 对照继续作为非 JSON DSL 和细节检查的兜底。用户必须
明确接受或拒绝候选；接受后才替换 Draft，并可撤销最近一次仍位于当前 source 的 accepted 修订。

### 10.3 保存与校验状态

- Agent 调用期间禁用重复提交并显示明确运行状态；
- 网络或编译失败后保留用户提示词，并展示结构化 diagnostics；
- Agent 候选通过编译后进入 Review，Accept 后 source 才成为 `valid`；
- Publish 按钮只使用服务端最新 Draft 投影中的 AllowedCommand；
- 所有命令完成后使用服务端返回的最新 Draft，不做乐观发布成功动画。

## 11. Diagnostics 契约

每条诊断至少包含：

```json
{
  "code": "DSL_PORT_INCOMPATIBLE",
  "message": "...",
  "json_path": "$.edges[2]",
  "severity": "error",
  "source_range": {
    "start_line": 18,
    "start_column": 5,
    "end_line": 21,
    "end_column": 6
  },
  "entity": {"kind": "edge", "id": "review_to_publish"}
}
```

UI 同时提供：

- Source 中的行列定位；
- Outline 中的节点/边定位；
- 可复制的错误码和 JSON Path；
- “重新校验”命令；
- invalid source 原文保留。

## 12. 权限、安全与审计

首发可沿用 `runtime.write`，但命令和服务层仍要为后续拆分 `workflow.author`、`workflow.publish` 做清晰边界。至少审计：

- Draft created/resumed；
- Draft saved；
- Validation passed/failed；
- Publish attempted/succeeded/conflicted；
- Draft discarded。

审计不记录完整 source，只记录 Draft ID、Workflow ID、revision、source hash、Definition Hash、诊断数量和结果。source 不写入 URL、日志、错误 message 或 analytics。

## 13. 分阶段工作包

### P0：契约冻结与迁移设计（1–2 人日）

- 冻结 Draft DTO、AllowedCommand、错误码和状态机。
- 增加 schema/golden fixtures，但不实现 UI。
- 确认旧版本兼容策略：缺 source 的版本走 `source_unavailable` 降级（导出器不在本计划内）。

Gate：DTO golden、状态迁移表、权限矩阵和失败恢复方案完成评审。

### P1：持久化 Draft 与 API（3–5 人日）

- migration、repository、application service；
- create/resume、read、save、validate、publish、discard；
- Workflow Detail 广告 Create/Resume Draft；
- 专用冲突和 validation error envelope。

Gate：API、并发、幂等、重启恢复、权限和故障注入测试通过。

> 以下 P2–P5 记录保留为历史实施轨迹；其中手工 Source/结构化编辑能力已被
> 2026-07-21 的 Agent-only 决策取代，不代表当前产品入口。

### P2：Source Editor 可用闭环（历史，已取代）

- Editor 路由与页面；
- JSON source 编辑、自动保存、格式化、Diagnostics；
- Validate、Publish、Discard；
- Workflow Detail 的 Edit/Continue Edit 入口。

Gate：编辑 v1 → 发布 v2 → 旧 Run 保持 v1 → 新 Goal 使用 v2 的浏览器 E2E 通过。

执行记录（2026-07-20）：P0 冻结 WorkflowDraft 2.0 契约（schema + 正反样例，
含"validating 已删除""PATCH 违反冻结命令契约"两条反例）。P1 落地 migration 13
`workflow_drafts`、`WorkflowDraftApplicationService`（CAS、one-active、同步校验、
发布 CAS + 内容幂等、崩溃窗口 lazy reconcile）与全部六条路由；错误码按 §8.4 映射，
draft 对其它 Actor 以 404 不可区分。P2 交付 Source Editor：`#/workflows/{id}/edit/{draft}`
深链、800ms 自动保存与 Saved/Saving/Unsaved/Conflict 状态、诊断面板、服务端广告的
Validate/Publish/Discard、409 冲突横幅与"重新加载服务端副本"恢复路径。浏览器 E2E
覆盖编辑 v1 → 发布 v2 → 旧 Run 保持 v1，以及双标签 revision 冲突恢复；无 source 的
旧版本按 §7 降级不广告编辑。全量 871 tests 通过。

### P3：结构化 Metadata、Node 与 Agent 编辑（4–6 人日）

- Metadata 表单；
- Node/Handler/Agent 选择；
- ports/config schema 表单；
- 表单与 source 单向事务式同步：修改表单生成候选 source，经服务端校验后替换 Draft。

Gate：不兼容 Handler 不可选择或由服务端明确拒绝；切换 Agent 后端口契约保持有效。

执行记录（2026-07-20）：Editor 新增 Outline / Metadata / Nodes / Source 四区；Metadata
与 Node 表单生成候选 JSON 后通过广告的 Save 命令持久化，并立即交由生产编译器校验。
Node 表单覆盖 kind、输入/输出端口、config schema 字段及高级 JSON；Handler Catalog
新增 inputs、outputs、config_schema 只读事实。Handler / Agent 下拉仅显示 node kind 和
输入/输出端口完全匹配的已注册项，端口变化后旧选择不能通过应用前检查。纯转换测试、
Catalog API 测试和浏览器结构化保存/校验测试已覆盖该 Gate。

### P4：Edge 与 Policy 编辑（5–8 人日）

- Edge CRUD、condition/mapping；
- Join/Retry/Loop/Rework 等 Policy；
- 图摘要和诊断定位；
- 版本冲突比较视图。

Gate：cycle、port incompatibility、invalid policy、invalid join 等失败均可定位且不丢 Draft。

执行记录（2026-07-20）：Editor 新增 Edge CRUD（端点、route、priority、condition、
mapping、back edge、policy ref）和 Policy CRUD（route/join/retry/rework/loop/completion
模板与 JSON config），Node 表单可绑定 Policy 并编辑 route mode。删除或重命名 Policy
会事务式更新 Node/Edge 引用。Draft diagnostics 兼容旧存量 `path` 并统一投影为冻结契约的
`json_path`；点击错误可定位到 Node、Edge、Policy 或源码行列。revision/发布冲突横幅提供
本地与服务端 source 对照。浏览器 E2E 已覆盖 DSL_GRAPH_CYCLE、
DSL_PORT_INCOMPATIBLE、DSL_POLICY_INVALID、DSL_JOIN_INVALID 的定位、修复与重新发布 Gate。

### P5：发布加固（2–4 人日）

- 双主题、三档 viewport、键盘和屏幕阅读器；
- 256 KiB source、30 节点及大量 diagnostics 性能；
- 浏览器断网、Runtime 重启、多标签 revision 冲突；
- 文档、视觉基线、发布记录。

Gate：完整回归通过，控制台无 error，迁移可重复执行，失败无需手工修库。

执行记录（2026-07-20）：Editor section navigation 已采用标准 `tablist` / `tab` /
`tabpanel` 语义，支持左右方向键及 Home/End，保存状态通过 live region 播报。未保存内容会
注册离页保护；自动保存断网失败不再清除 dirty 标记，网络恢复后以原保存意图重试，实时
投影轮询也不会在 Agent 调用期间销毁本地提示词。应用层 Gate 覆盖 256 KiB 边界、30 个无效节点
的大量 diagnostics 和基于同一 SQLite 文件的服务重建恢复；浏览器 Gate 覆盖键盘、离线失败、
重试及 reload 恢复，既有 Gate 覆盖多标签 revision 冲突。视觉回归新增 Editor 深浅主题 ×
360×800、768×900、1280×800 六张响应式基线及八张关键状态基线；发布前以完整测试套件、console error
检查和重复 migration 检查收口。

补充收口（2026-07-21）：Workflow Editor 已收敛为 Agent-only，旧结构化 Node/Edge/Policy
和 Source 编辑入口不再暴露。Workflow Detail 现提供完整版本历史，可查看任意已发布版本、用历史版本启动可重放
Goal，或从所选版本派生 Draft。one-active 冲突提供“继续现有草稿”和“废弃后从所选版本
新建”两个明确动作；发布冲突提供复制本地草稿、查看最新版、废弃并基于最新版新建。
只读 Source Preview 展示 Agent 结果，Diagnostics 展示错误码与 JSON Path。视觉矩阵补齐
empty、dirty、invalid、valid、conflict 状态，Editor 深链纳入 console error Gate。

Agent-only 闭环已包含候选 diff、接受/拒绝、一次撤销和最近修订历史。

## 14. 测试矩阵

| 层级 | 必测内容 |
|---|---|
| Migration | 新库、已有库、重复 migrate、source_text 缺失版本。 |
| Repository | revision CAS、one-active-Draft、discard/publish 状态、并发 Agent 修订。 |
| Application | create/resume、校验、发布幂等、故障恢复、Actor ownership。 |
| API | AllowedCommand、401/403/409/413/422、幂等键、Expected Version。 |
| Compiler round-trip | source → IR → hash 稳定；`source_unavailable` 版本不广告编辑命令。 |
| Browser E2E | 编辑、自动保存、刷新恢复、invalid diagnostics、发布 v2、冲突恢复。 |
| Runtime E2E | 老 Run 固定旧版本，新 Goal 选择最新版本。 |
| Visual | Editor empty/dirty/invalid/valid/conflict，双主题、三档 viewport。 |
| Accessibility | Tab 顺序、焦点恢复、诊断跳转、状态 live region、对话框 Escape。 |

## 15. 文件影响范围

| 区域 | 预计改动 |
|---|---|
| Persistence | `workflow/persistence/migrations.py`、Draft repository/model。 |
| Application | 新增 `workflow/application/workflow_draft_service.py`，复用 `workflows.py`。 |
| Read Model | Workflow Detail source/editability、Draft Detail DTO。 |
| HTTP | `web/api_v1.py` 新增 Draft 路由与错误映射。 |
| UI | `router.js`、`api.js`、Workflows Detail、Editor view、i18n、styles。 |
| Recovery | `workflow/recovery/`：注册“版本已发布但 Draft 未收尾”的扫描项，按 Definition Hash 幂等补齐 Draft 状态。 |
| Tests | migration、API、browser E2E、visual baselines、golden fixtures。 |
| Docs | API 契约、Runtime UI 规划、发布说明。 |

## 16. 发布验收标准

只有同时满足以下条件，才能宣布 Workflow 编辑可用：

1. 已发布版本数据库记录没有 UPDATE/DELETE 路径。
2. Draft 在浏览器刷新和 Runtime 重启后可恢复。
3. invalid Draft 永远不能被发布。
4. 发布并发冲突不会覆盖已有版本，也不会丢失当前 Draft。
5. 发布重试不会产生多余版本。
6. 旧 Run 的 Definition Hash 和行为不受新版本影响。
7. 新 Goal 默认使用最新发布版本，并可显式选择旧版本时仍保持可重放。
8. UI 不自行拼 Save/Validate/Publish URL 或 Expected Version。
9. Diagnostics 至少提供错误码、JSON Path 和可读 message。
10. API、重启、浏览器、双主题、响应式和完整 Runtime 回归全部通过。

## 17. 已拍板决策与后续决策点

### 17.1 已拍板

- Published WorkflowVersion 不可变；编辑产生新版本。
- Draft 必须服务端持久化，不以 localStorage 作为权威。
- Workflow 只能通过 Agent 提示词修改；DSL 与结构化定义均为只读投影。
- 首发不做自动 merge/rebase。
- 每 Actor、每 Workflow 最多一个活动 Draft。
- Run 不能启动 Draft。

### 17.2 首发决策（已冻结）

- YAML Draft 在首次 Agent 修订前保留原始 source；Agent 输出统一为格式化 JSON，不承诺
  保留 YAML 注释或排版。
- 本地首发不自动物理删除 discarded Draft；状态、hash 和审计记录持久保留，后续若引入
  retention job 必须单独定义可恢复窗口。
- 首发继续使用 `runtime.write`，应用服务和 AllowedCommand 边界保持可拆分；
  `workflow.author` / `workflow.publish` 作为多用户部署的独立权限工作包。
- 用户显式提交 Agent Prompt；服务端生成、校验并原子应用修订，失败不改变 Draft。
- 首发历史版本提供版本选择、source/定义查看和从历史版本派生，不实现结构化 node/edge
  diff；Run Plan 的版本 diff 仍是独立的运行时读模型。
