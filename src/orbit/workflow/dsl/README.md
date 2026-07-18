# Workflow DSL 与 Canonical IR 1.0

本目录实现 Step 2 的定义期管线。DSL 面向作者和 UI；Runtime 只允许读取已经发布的 Canonical WorkflowIR，不读取或重新解释 DSL、表达式文本、默认值或 Catalog。

## 处理阶段

```text
text -> ParsedDslDocument -> structural validation -> semantic analysis
     -> expression/mapping AST -> normalized WorkflowIR -> Definition Hash
```

每个阶段只能消费前一阶段的成功结果。Parser、Validator 和 Compiler 不访问数据库、时钟、随机数、网络或 Handler 实现。

YAML 与 JSON 都生成字段级 Source Map。输入上限为 2 MiB，展开后的语法节点上限为 20,000，文档嵌套深度上限为 128，YAML Alias 上限为 50；越界统一返回 Diagnostic，不向调用方泄漏 Python `RecursionError`。

## DSL 1.0 字段语义

| 字段 | 身份/语义 | 默认值 | 顺序语义 | 进入 IR/Hash |
| --- | --- | --- | --- | --- |
| `dsl_version` | DSL Schema 版本 | 无，必填 | 无 | 间接；IR 使用独立 `ir_version` |
| `metadata.id` | Workflow 人类可读稳定 slug | 无，必填 | 无 | 是，编译为 `workflow:<slug>` |
| `metadata.name` | 版本显示名 | 无，必填 | 无 | 是 |
| `metadata.description` | 版本描述 | `""` | 无 | 是 |
| `metadata.labels` | 版本标签 | `{}` | Key 无序并排序 | 是 |
| `inputs/outputs` | Workflow 边界 Port | `[]` | 按 Port ID 排序 | 是 |
| `nodes` | 静态节点集合 | 无，必填 | 按 Node ID 排序 | 是 |
| `edges` | 静态边集合 | 无，必填 | 按 Edge ID 排序 | 是 |
| `entry` | 显式入口 Node ID 集合 | 无，至少一个 | 排序 | 是 |
| `terminals` | 显式终点 Node ID 集合 | 无，至少一个 | 排序 | 是 |
| `policies` | 定义期 Policy 引用与配置 | `[]` | 按 Policy ID 排序 | 是 |
| `extensions` | 版本化扩展信封 | `[]` | 按 ID、Version 排序 | 是 |
| `handler.version` | 编译前版本约束 | 无 | 无 | 否；解析后的精确版本进入 IR/Hash |
| `condition` | 受限条件表达式或 AST | `true` | AST 规范化 | 只有编译后 AST |
| `mapping` | 受限数据映射 | Identity | Object Key 排序 | 只有编译后 AST |
| Source Map、注释、文件名 | 诊断与审计信息 | — | — | 否 |

Definition Hash 使用 Step 1 的 Canonical JSON 和 SHA-256，只覆盖完整 WorkflowIR。源格式、空白、注释、字段排列、Catalog Fingerprint、发布时间、发布者和数据库版本不参与 Hash。Catalog 解析出的精确 Handler/Schema/Extension Version 会进入 IR，因此能力解析结果变化会自然产生新 Hash。

## Core 图和引用规则

- DSL Core 1.0 是 DAG；普通 Edge 形成环时编译失败。
- Entry 和 Terminal 必须显式声明。所有 Node 必须从 Entry 可达，并且存在到 Terminal 的路径。
- Entry 可以是任意 Node Kind；Entry 同时为 Terminal 表示合法的零执行步骤 Workflow。只有 `terminals` 集合中的节点被强制要求 `kind=terminal`。
- Terminal Node 不得有出边。
- Edge 必须从输出 Port 指向输入 Port；单值输入只能有一个 Writer。
- Port Schema v1 采用保守兼容：源和目标必须引用相同的版本化 Schema ID。Mapping 必须声明结果 Schema ID。
- Edge 条件和 Mapping 只能读取 `source.<当前源端口>` 与 `workflow.inputs.<端口>`；跨节点历史读取必须通过显式 Port/Artifact 建模。

## Handler 版本

DSL 接受精确 SemVer `x.y.z` 或 caret 约束。Compiler 在不可变 Catalog Snapshot 中选择满足约束的最高版本，并把精确版本写入 IR。`^1.2` 限制在 `1.x`；`^0.2` 限制在 `0.2.x`。没有匹配版本、Node Kind 不匹配或 Manifest Port 不一致都会在编译期失败。

字符串条件使用 Python 表达式的白名单子集，服务于人类手写 DSL；它只在编译期存在。UI 不生成 Python 文本，直接提交同一版本的结构化表达式 AST。Runtime 只执行 IR 中经过验证的 AST，因此不绑定 Python Evaluator 或 `eval`。

## Extension

扩展只能通过 `{extension_id, extension_version, config}` 信封出现。Registry 必须存在完全匹配的 Manifest，Config 必须通过该版本 Schema。Agentic Region 在 Step 2 仍是 Draft Extension；其 Payload 可以被保存和 Hash，但在后续步骤注册可执行语义前，不能被 Runtime 当作稳定节点执行。

## 发布语义

- `workflow_definitions` 保存稳定 Workflow 身份。
- `workflow_versions` 保存不可变 Canonical IR、Hash、编译元数据和可选 Source。
- 相同 Workflow ID 与 Hash 的重复发布返回已有版本。
- 内容幂等优先于乐观并发：相同 Hash 即使携带过期 `expected_latest_version` 也返回已有版本；只有新 Hash 才比较并发版本。
- 不同 Hash 必须携带正确 `expected_latest_version`，事务内分配下一版本。
- 数据库 Trigger 禁止更新或删除已发布 WorkflowVersion。
- `workflow_definitions.name` 是列表展示用的最新名称；每次成功发布新版本后更新。各历史名称仍保存在对应不可变 IR 中。
