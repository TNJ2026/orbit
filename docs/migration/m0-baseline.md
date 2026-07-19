# M0 基线记录

> 阶段：M0（冻结切换边界和真实基线）
> 上级方案：[workflow-runtime-clean-migration-plan.md](../workflow-runtime-clean-migration-plan.md)
> 采集日期：2026-07-18
> 性质：本文是 M6 删除范围与 M7 发布 Gate 的事实基准。数字必须可重复复现，不得手工估算。

## 1. 复现命令

```bash
.venv/bin/python -m unittest discover -s tests        # 全量测试
.venv/bin/python -m compileall -q src/orbit           # 编译检查
```

## 2. 测试基线

| 项 | 值 |
|---|---:|
| 全量测试 | **732 passed** |
| 耗时 | ~11.9s |
| 编译检查 | 通过 |

拆分（按引擎归属，详见 §5 归属矩阵）：

| 归属 | 测试数 | 占比 |
|---|---:|---:|
| 旧引擎（M6 删除范围） | 281 | 50.4% |
| 新 Runtime（保留） | 277 | 49.6% |

**M6 后测试数会从 558 降到约 277。** 这不是覆盖率下降的许可——§5 矩阵逐文件规定"迁移 / 重写 / 删除"，M1B 与 M6 必须按矩阵核验，不得只对总数负责。

## 3. Migration 基线

新 Runtime Migration Ledger 当前版本 **9**，Fresh Database 顺序执行全部 9 个：

| 版本 | 名称 |
|---:|---|
| 1 | workflow definitions and immutable versions |
| 2 | deterministic runtime projections and event store |
| 3 | durable jobs leases and timers |
| 4 | immutable values artifacts and lineage |
| 5 | static graph generations joins and control counters |
| 6 | durable planner attempts and proposals |
| 7 | dynamic plans policy human tasks and budget ledger |
| 8 | human collaboration foreach subflow and dynamic dag |
| 9 | security audit and api idempotency |

方案 §2.3 规定：Ledger 不因本次切换被压平；新增持久化需求只能按现有 Ledger 增量提交。

## 4. Fresh Schema allowlist（34 张表）

M2/M6 的 Schema allowlist 校验以此清单为准。出现清单外的表（尤其 `tasks`/`messages`/`run_jobs` 等旧表）即判定为混合 Schema，拒绝启动。

```text
api_command_receipts      artifact_acl              artifact_links
artifacts                 audit_records             branch_tokens
budget_accounts           budget_ledger_entries     budget_reservations
command_receipts          durable_timers            execution_plans
foreach_groups            foreach_items             graph_control_counters
human_task_participants   human_tasks               job_leases
jobs                      join_groups               node_attempts
node_runs                 plan_patches              planner_attempts
planner_proposals         policy_decisions          run_events
run_snapshots             security_capabilities     subflow_links
value_links               values                    workflow_definitions
workflow_runs             workflow_versions
```

注：`values` 是 SQL 保留字，代码中始终使用标识符引用（Step 7 Completion Record 已记录）。

## 5. 测试归属矩阵

方案 §4.3 要求"删除测试不等于降低覆盖"，M0 任务 8 要求逐 Test ID 定性。

**权威清单**：[`tests/migration/legacy_test_disposition.json`](../../tests/migration/legacy_test_disposition.json)（281 条，逐 Test ID）
**校验**：`tests/test_migration_guard.py::LegacyTestDispositionGuard` — 新增/删除旧测试而未更新清单即失败。

### 5.1 逐 ID 统计

| 处置 | 数量 | 含义 |
|---|---:|---|
| `delete` | 136 | 领域概念随旧引擎消失 |
| `migrate` | 92 | 能力存续，行为须在新模块测试中出现 |
| `rewrite` | 53 | 能力存续，测试须针对新 Port 重建 |

`migrate`/`rewrite` 共 **145 条**，M6 删除原测试前必须先有 Replacement Test ID。

### 5.2 纠正方案的初始分组假设

方案 §M0"旧测试归属的初始分组"按文件名推定能力，逐 ID 核对后发现两处偏差：

| 文件 | 方案假设 | 实际构成 |
|---|---|---|
| `test_worktree.py` (80) | "M1B 全部重写到新 Workspace/Process Port" | **真正 worktree/进程相关仅 22 个**（WorktreeLifecycle 5、GitProvisioning 10、Sweep 1、ProcessControl 4、DescendantPid 2）。其余 58 个是 workflow schema (9)、step prompt (8)、goal verify 检测 (9)、goal 收敛 (7)、budget gate (6+2)、canvas layout (5)、goal status (3)、task terminal (4)、stale run (1) — 各自归属不同阶段 |
| `test_workflow_engine.py` (140) | "旧 Goal/Task 推进语义删除；其余逐项迁移" | 删除 96、迁移 44。迁移项集中在 AutoRunner (19，进程执行/取消/流式)、TokenStats (14，用量记账)、TaskHealthCheck (12，健康与陈旧恢复)、HubInspect (9，卡死检测) |

**这正是计划坚持逐 Test ID 而非按文件的价值**：按文件名迁移会把 58 个非 worktree 测试错误地塞进 M1B，或连同能力一起误删。

### 5.3 新 Runtime 测试（277）

全部保留。M6 不得因"总数下降"而放宽这部分的任何断言。

### 5.2 新 Runtime 测试（277）

全部保留。M6 不得因"总数下降"而放宽这部分的任何断言。当前分布：

```text
domain 34 · dsl 29 · durable_runtime 16 · runtime 13 · handler_runtime 13
graph_runtime 12 · planner 10 · graph_contracts 9 · handler_contracts 9
rehydration 9 · advanced 9 · event_store 8 · data_contracts 8 · step12_api 8
step10_agentic 7 · persistence 7 · data_e2e 6 · artifact_backend 6
step11_structures 6 · step12_sandbox 6 · version_store 5 · 其余 ≤4
```

### 5.3 M6 核验方式

M6 删除后必须证明：

1. §5.1 标记"迁移/重写"的 111 个测试在新模块有对应实现，且新测试不 import 旧模块。
2. 新 Runtime 277 个测试全部通过，无一被跳过或放宽。
3. 能力矩阵（worktree / 进程取消 / 项目索引 / 打包契约）逐项有新归属测试。

## 6. 外部集成清单

**权威清单**：[`tests/migration/external_integrations.json`](../../tests/migration/external_integrations.json)（8 项）
**校验**：`tests/test_migration_guard.py::ExternalIntegrationGuard` — `unclassified` 非空即失败；`.codex/config.toml` 里出现未登记的 URL 也失败。

| 集成 | 现状 | 处置 | 目标阶段 |
|---|---|---|---|
| MCP `/mcp` | **实现已不存在**（见 §6.1） | 保留并重写 → `web/mcp.py` | M3 |
| `orbit serve` 端口 8848 | 活跃 | 保持不变 | — |
| 旧 `/ui` 任务看板 | 活跃 | 同 URL 换内容（新 Runtime UI） | M6 |
| `/workflow-ui` 原型 | 活跃（仅 smoke） | 删除 | M4 |
| `/static/dagre.min.js` | 活跃 | 移入 `/ui/assets/` 或随旧 UI 删除 | M4 |
| 未版本化 `/api/*`（38 条路由） | 活跃 | 删除，无别名/重定向 | M6 |
| 项目索引在线探测 | 活跃 | 改 `/health/ready` | M1A |
| 旧 CLI（start/up、config/init、runner、workflow db-check） | 活跃 | 删除 | M6 |

### 6.1 B1 结论：MCP 端点

**当前代码中 `/mcp` 不存在**，`.codex/config.toml` 是陈旧配置（连接必然失败）。git 历史确证：

- `948d0e2` — 移除交互式 mailbox MCP 工具
- `8e4d786` — *"refactor: drop FastMCP, serve on plain Starlette + uvicorn"*，理由是"剩余三个 workflow 工具与已有 /api 路由重复"

按方案 M0 Gate 明文要求（"即使 M0 发现当前 `/mcp` 实现已经缺失或配置陈旧，也必须把它记录为待恢复/待通知的外部契约，不能用'当前已不可用'作为静默删除理由"），处置为 **retain_and_rewrite**：M3 新建 `web/mcp.py`，只调用新 Application Service/Read Model/统一 Command，复用身份、Capability、幂等与 Expected Version。

**遗留通知项**：恢复后的工具面若与 `8e4d786` 之前不同，须通知 codex 使用方。

## 7. 能力归属（方案 §4.1）

review 曾指出 §4.1 遗漏 Agent CLI 发现与 i18n；**方案已采纳并补入**（文档由 478 → 526 行）：

| 能力 | 方案处置 | 归属 | 阶段 |
|---|---|---|---|
| Agent CLI / Hermes profile 发现 | 重写为可信 Handler Adapter 注册源；只产生受控 Manifest，不产生 DSL 可执行命令 | `workflow/catalogs/agent_discovery.py` | M5 |
| i18n | 保留中英双语；`zh-CN`/`en-US` message catalog + 稳定 key，文本/ARIA/错误/日期/预算单位全部走 formatter | M4 任务 10 | M4 |
| MCP | 保留 `/mcp` 协议入口，改接新 Application Service | `web/mcp.py` | M3 |

三项均已有明确归属，B1–B3 关闭。

## 8. 代码体量基线

| 文件 | 行数 | M6 处置 |
|---|---:|---|
| `src/orbit/server.py` | 6,903 | 删除 |
| `src/orbit/store.py` | 1,976 | 删除 |
| `src/orbit/__main__.py` | 463 | 收敛（保留新 CLI） |
| `src/orbit/project_index.py` | 146 | 迁入 `platform/projects.py` |
| `src/orbit/static/ui.html` | 3,921 | 删除 |
| `src/orbit/static/workflow-ui.html` | 890 | M4 拆分后删除 |

M1B 的估算依据：从 6,903 行 `server.py` 中识别进程/取消/日志/Git 行为并重建独立 Port，不是机械复制。

## 9. Step 10–12 状态校准

方案 M0 任务 3 要求"将 Step 10–12 的未完成项与 UI U0 缺口合并成阻断清单"。该表述写于此前红灯语境，现已变化：

- 当前全量 **732 passed**，无失败。
- `plan_patch` 等契约的 Draft→Stable 升级已走 [ADR-002](../adr/002-plan-patch-stable.md) 显式程序，Step 1 golden 同步更新。
- Step 10–12 补充测试文件（`test_workflow_step10_agentic.py` / `step11_structures` / `step12_api` / `step12_recovery` / `step12_sandbox`）共 31 个测试已就位。

**结论**：Step 10–12 不再是红灯阻断项。剩余阻断项收敛为下表。

## 10. M0 阻断清单

| # | 阻断项 | 类型 | 状态 |
|---|---|---|---|
| B1 | MCP 端点去向 | 外部集成 | ✅ 关闭 — §6.1，retain_and_rewrite @ M3 |
| B2 | Agent CLI 发现能力归属 | 能力归属 | ✅ 关闭 — §7，`agent_discovery.py` @ M5 |
| B3 | i18n 去留 | 能力归属 | ✅ 关闭 — §7，中英双语 catalog @ M4 |
| B4 | UI U0 后端契约未实现（11 项） | 依赖阶段 | 归属 M3，不阻断 M0 |
| B5 | Step 11 Foreach/Subflow Runtime 缺口 | 依赖阶段 | ⚠️ M7 复核重开并部分关闭 — 恢复接线已修；能力仍不可达，见 [unwired-capabilities.md](./unwired-capabilities.md) |

**M0 无未关闭阻断项。**

> **M7 复核补记**：B5 在 M5 被宣布完成时未复查。M7 复核发现两件事：
> 恢复路径缺 ForeachService 接线（已修，见 `tests/test_recovery_wiring.py`），
> 以及 Foreach/Subflow/Planner 三项能力虽已实现且有测试，但 DSL 无法表达、
> 运行时无循环驱动，因此从运行中的系统不可达。详见
> [unwired-capabilities.md](./unwired-capabilities.md)。

## 11. M0 Gate 状态

| Gate 条件 | 状态 | 证据 |
|---|---|---|
| 新 Runtime 当前测试基线可重复 | ✅ | 732 passed，命令见 §1 |
| 未完成能力没有被文档标记为 Completed | ✅ | §9 复核：Step 10–12 与测试事实一致，ADR-002 走了显式升级程序 |
| 每个旧能力都有删除或替代归属 | ✅ | §5 逐 Test ID 281/281；§6 外部集成 8/8；§7 三项能力归属 |
| 本文可由 Git 跟踪、Diff 和评审 | ✅ | `.gitignore` 放行规范文档集（迁移方案 + 实现规划 + 2 UI 契约 + 2 ADR + `docs/migration/`） |
| `/mcp`、webhook、外部脚本有明确目标与负责人 | ✅ | §6/§6.1；Guard 校验 `.codex/config.toml` |
| 旧引擎 281 个测试全部有逐项 disposition | ✅ | §5；Guard 校验清单与代码同步 |

## 12. M0 交付物

| 交付物 | 路径 | 校验 |
|---|---|---|
| 基线记录（本文） | `docs/migration/m0-baseline.md` | — |
| 逐 Test ID 归属清单 | `tests/migration/legacy_test_disposition.json` | Guard |
| 外部集成清单 | `tests/migration/external_integrations.json` | Guard |
| 架构 Guard（记录模式） | `tests/test_migration_guard.py` | 7 tests |
| gitignore 放行规范文档 | `.gitignore` | — |

### Guard 当前模式

M0 的 Guard 是**记录模式**：锁住清单与代码的同步、冻结旧模块体量上限（只能缩不能涨）、并已硬性禁止新 Runtime import 旧模块。M6 时再把 §M6"绝对禁止项"翻成阻断断言（route manifest、CLI help snapshot、package manifest、fresh schema allowlist）。
