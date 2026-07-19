# 提示词生成工作流

> 状态：已交付（2026-07-19）
> 入口：Workflows 页「生成工作流」；`POST /api/v1/workflows/generate` + `POST /api/v1/workflows/{id}/versions`
> 实现：`src/orbit/workflow/authoring/`、`web/api_v1.py`、`web/app.py` 组合根、`workflow-ui` 生成对话框

用户用自然语言描述流程，本机受信 Agent CLI 起草 DSL，编译器完成全量校验，
用户在预览确认后才发布为不可变 WorkflowVersion。生成物是纯数据；发布与运行
沿用既有的 AllowedCommand、幂等键与 Expected Version 纪律。

## 信任与约束模型

原始方案要求"输出五层漏斗、输入当数据"。落到新 Runtime 后大部分约束由既有
机制承担，不再需要单独实现：

| 原方案约束 | 落地形态 |
|---|---|
| 协议层：单个 JSON | `WorkflowAuthoringService._extract_json`：单对象，容忍 ```json 围栏 |
| 结构层：字段白名单/枚举 | `compile_source` 的 DSL JSON Schema + 语义分析（与 `orbit workflow validate` 同一条路径） |
| 安全剥离：不得携带可执行命令 | **结构性成立**：DSL 只按名字引用 sealed registry 里的 handler，没有 command 字段可注入 |
| 数值/规模边界 | 指令 ≤4000 字符、节点 ≤30、CLI 输出 ≤256 KiB、超时 300s |
| 图语义校验 | 编译器：入口可达、端口 schema 匹配、handler 存在、human 节点约束 |
| 注入防护 | 指令包进 INSTRUCTION-BEGIN/END 定界符并声明为数据；防线不依赖模型听话——输出侧无可执行面 |
| 校验失败回喂重试 | 诊断原样喂回，共 3 次尝试；耗尽返回结构化 diagnostics + 原始输出（可查，不是裸 500） |

生成 CLI 与 Planner 同一条信任规则：命令来自 discovery allowlist 解析的
executable（组合根选择，请求无法指定），stdin 传数据，输出有界。测试与嵌入方
可通过 `create_app(workflow_generator=...)` 注入。

## 命令面

- 目录响应顶层广告 `workflow.generate`（需 write scope 且生成器可用）；
- 生成响应携带草稿 + 服务端广告的 `workflow.publish` 命令（含当前
  latest_version 作 expected_version）——预览到发布全程不拼 URL；
- 发布端点**先编译校验、再核对路由与 source 声明的 workflow_id 一致、最后落库**，
  拒绝时零残留；冲突映射 409；
- 能力申报：`capabilities.workflow_generation.available/reason`，discovery 关闭
  或无 CLI 时 UI 直接不渲染入口。

## 测试

- `tests/test_workflow_authoring.py`：提示词事实、围栏提取、诊断回喂、上限、
  CLI 错误分级（10）；
- `tests/test_api_v1.WorkflowAuthoringApiTests`：广告命令全链路、失败诊断、
  无生成器 503/能力申报、跨 workflow 发布拒绝、冲突与越权（5）；
- `tests/test_browser_e2e.GenerateWorkflowTests`：中文界面 描述→生成→预览→
  发布→向导起 run→succeeded，模型为脚本假件，其余全为生产路径（1）。

## 未做（有意）

- 修改已有工作流的对话式迭代（回传 current source）——生成新 version 已可用，
  对话式 diff 预览另行排期；
- 生成期澄清问答；
- 除首个 discovered CLI 外的生成器选择 UI。
