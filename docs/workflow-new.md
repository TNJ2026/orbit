下面整理成一篇结构化文档，保留本次讨论的核心设计思想，适合作为 Workflow / Agent 系统设计文档初稿。

:::writing{variant="document" id="58391"}
# Agent Workflow 系统设计思考：从流程图到通用工作流引擎

## 1. 背景

随着 AI Agent 的发展，越来越多的任务开始由多个 Agent 协同完成，例如：

- AI 软件开发
- 内容生产
- 数据分析
- 企业审批
- 自动化运营
- Research Agent

这些场景都需要一种方式描述：

- 谁执行任务
- 如何传递信息
- 如何处理失败
- 如何循环优化
- 如何展示给用户

因此，需要设计一种 Workflow 系统。

---

# 2. Workflow 保存格式选择

Workflow 定义通常有两种主要格式：

## JSON

适合：

- Runtime 执行
- API 传输
- 数据库存储
- Schema 校验

特点：

优点：

- 标准化
- 跨语言支持好
- 机器处理方便

缺点：

- 人工维护困难
- 大流程可读性差


示例：

```json
{
  "nodes": [
    {
      "id": "planner",
      "type": "agent"
    },
    {
      "id": "coder",
      "type": "agent"
    }
  ],
  "edges": [
    {
      "from": "planner",
      "to": "coder"
    }
  ]
}
```

---

## YAML

适合：

- Workflow 源文件
- Git 管理
- 人工编辑

特点：

优点：

- 易读
- 修改方便
- 类似 GitHub Actions、Kubernetes 配置

示例：

```yaml
workflow:
  name: coding-flow

  steps:

    - id: plan
      agent: claude

    - id: code
      agent: codex
```

---

## 推荐架构

采用：

```
workflow.yaml
       |
       v
Parser + Validator
       |
       v
Workflow AST
       |
       v
Runtime JSON
```

即：

- YAML 作为源码
- JSON 作为执行格式
- JSON Schema 负责校验

---

# 3. LangGraph 类系统解决什么问题

LangGraph 的核心思想：

> 使用 Graph 描述有状态 Agent Workflow。

核心元素：

```
Node
Edge
State
Condition
Loop
```

例如：

```
Planner
   |
Coder
   |
Reviewer
   |
失败
 |
Fix
 |
Coder
```

它主要解决：

## 多 Agent 协作

不同 Agent 负责不同角色。

例如：

- Planner
- Coder
- Reviewer


## 状态管理

Workflow 中保存：

```json
{
  "task": "",
  "context": "",
  "history": []
}
```

不同步骤读取和更新状态。


## 循环执行

例如：

```
Code
 |
Test
 |
失败
 |
Fix
 |
Code
```

适合自动优化任务。


## 条件分支

例如：

```
Review

成功 -> Deploy

失败 -> Fix
```

---

# 4. 常见 Agent Workflow 框架

## LangGraph

特点：

- Graph
- State Machine
- Loop

适合：

复杂 Agent Workflow。


## AutoGen

特点：

- Agent 对话
- Agent Communication

模型：

```
Agent A
 |
Message
 |
Agent B
```

适合：

多 Agent 协作。


## CrewAI

特点：

- Role-based Agent

例如：

```
Product Manager
Developer
Reviewer
```

适合：

企业自动化。


## LlamaIndex Workflow

特点：

- Event-driven

适合：

- RAG
- 数据处理
- Knowledge Agent


## Semantic Kernel

特点：

- 企业 Agent SDK
- Plugin
- Planner

---

# 5. 一般开发流程并不需要复杂 Graph

对于软件开发场景：

通常流程是：

```
需求
 |
分析
 |
设计
 |
编码
 |
测试
 |
Review
```

最多：

- 少量分支
- 少量循环

因此不需要复杂 Graph 编辑器。

更适合：

```
Pipeline + Loop + Checkpoint
```

---

# 6. Workflow 用户展示方式

## 不推荐：复杂 Graph 默认展示

原因：

- 节点多时难理解
- 简单流程反而复杂化


## 推荐方式一：Timeline

例如：

```
AI Development Run

✓ Analyze
  Claude

✓ Design
  Claude

▶ Coding
  Codex

○ Test

○ Review
  Gemini
```

优势：

用户知道：

- 当前状态
- 下一步
- 执行结果


---

## 推荐方式二：Pipeline

例如：

```
Analyze
   |
Implement
   |
Test
   |
Review
```

适合作为 Workflow 编辑器。


---

## 推荐方式三：Graph 高级模式

专家用户查看：

```
      Review
       |
   +---+---+
   |       |
 Pass    Fail
   |       |
Deploy   Fix
```

---

# 7. Rework（返工循环）的设计

不要把 Rework 展示成普通分支：

```
Test失败
 |
返回Coding
```

这样容易产生复杂流程图。


更好的方式：

把它作为 Step 内部 Loop。


例如：

```
Test

失败

Iteration #1

Fix

Test

Iteration #2

成功
```

展示：

```
Validate

  Attempt History:

  #1 Failed
  #2 Failed
  #3 Passed
```

---

# 8. Workflow 输入输出设计

核心原则：

> Step 之间不要直接传数据，而是通过 Artifact 连接。

模型：

```
Step A

产生 Artifact

      |

Step B

消费 Artifact
```

---

例如：

```
Analyze

Output:
 requirement.md


Design

Input:
 requirement.md

Output:
 design.md


Code

Input:
 design.md

Output:
 source code
```

---

# 9. Artifact 作为核心对象

不要：

```yaml
input:
  - code
```

而应该：

```yaml
artifact:

 id: document

 type: text
```

Artifact 可以是：

- 文档
- 图片
- 代码
- 数据
- 报告
- 音频

这样 Workflow 才能通用。

---

# 10. 通用 Workflow 数据模型

推荐：

```
Workflow

 ├── Steps
 │
 ├── Executors
 │
 ├── Artifacts
 │
 ├── Rules
 │
 ├── State
 │
 └── History
```

---

## Step

定义任务：

```yaml
step:

 id: summarize

 executor:
   type: agent

 input:
   - document

 output:
   - summary
```

---

## Executor

负责执行：

类型：

- Agent
- API
- Script
- Human


---

## Artifact

表示数据：

例如：

```
document
image
code
report
```

---

## Rule

控制流程：

例如：

```
if approved:
    next = publish

else:
    next = revise
```

---

# 11. 推荐的通用 Agent Workflow 架构

```
                 Workflow YAML

                       |

                Workflow Compiler

                       |

                 Execution Engine

                       |

        +--------------+--------------+

      Agent          Tool          Human


                       |

                 Artifact Store


                       |

                 State Database
```

---

# 12. Loop Engineering 的定位

对于 AI Coding Agent：

不要强调：

“复杂 Agent Graph”

而应该强调：

```
Build
  |
Test
  |
Learn
  |
Improve
  |
Repeat
```

核心价值：

- 自动执行
- 自动验证
- 自动修复
- 自动迭代


---

# 总结

一个好的 Agent Workflow 系统应该：

1. 使用 YAML 描述 Workflow
2. 使用 JSON 执行 Runtime
3. 使用 Step + Artifact 作为核心抽象
4. 使用 State 管理执行过程
5. 使用 Loop 处理失败和优化
6. 使用 Timeline 作为主要用户界面
7. 使用 Graph 作为高级视图

对于普通业务流程：

> Pipeline + Artifact + Loop

已经可以覆盖大部分场景。

只有在任务高度动态、路径不可预测时，才需要复杂 Graph Agent 系统。
:::

如果后续继续设计这个系统，可以进一步展开成：
- Workflow DSL 规范
- JSON Schema 设计
- Runtime Engine 架构
- Workflow UI 原型
- Agent Executor 接口设计。

---

# 评审结论与待补设计

## 1. 总体结论

本文作为 Workflow 系统的设计思路和产品方向初稿，整体方向基本合理：

- 使用图描述步骤及其关系
- 通过状态保存执行过程
- 支持条件、循环和人工介入
- 使用 Artifact 表达需要持久化或跨步骤传递的产物
- 根据流程复杂度提供 Timeline、Pipeline 和 Graph 等不同视图

但本文目前仍属于概念设计，尚不能直接作为 Runtime 或 Workflow DSL 的实现规范。后续需要进一步明确执行语义、数据模型、异常恢复、版本管理以及与 orbit 现有实现的兼容方式。

## 2. Rework、Retry、Iteration 与 Foreach 需要分离

本文建议将 Rework 作为 Step 内部 Loop，但这种设计只适合同一节点内部的再次尝试，无法覆盖跨执行器的业务返工。

例如：

```text
Implement -> Test -> Implement
```

Test 失败后回到 Implement，涉及不同执行器、不同输入输出和独立的执行记录，不应隐藏在 Test 节点内部。

建议区分以下语义：

- `Retry`：同一节点因超时、限流或临时错误重新执行。
- `Rework`：节点业务结果不合格，沿显式工作流边返回其他节点。
- `Iteration`：一个节点或子流程根据业务条件反复运行。
- `Foreach`：对集合中的每个数据项执行节点或子流程。

Retry 应由节点执行策略管理；Rework 和 Iteration 应保留可观察的图路由与事件记录；Foreach 还需要定义并发数、失败策略和结果聚合方式。

## 3. Artifact 不应是唯一的数据传递方式

“Step 之间不要直接传数据，而是通过 Artifact 连接”过于绝对。布尔值、评分、计数、分支标签、小型 JSON 对象等数据没有必要全部物化为文件或独立 Artifact。

建议把节点输出统一定义为带 Schema 的 Output Port，并允许两种主要载荷：

```yaml
outputs:
  score:
    schema:
      type: number
    value: 0.95

  report:
    schema:
      type: object
    artifact_ref: artifact://run-123/report-1
```

- `value`：JSON 可序列化的小型结构化数据。
- `artifact_ref`：文件、图片、代码包、音频、大型数据集等外部或持久化对象的引用。

Artifact 模型还需要补充：

- 稳定 ID
- 名称与类型
- URI 或存储位置
- Content Type
- Schema
- 大小和校验和
- Producer Node Run
- Workflow Run ID
- 版本和血缘关系
- 访问权限
- 生命周期及清理策略

## 4. Workflow 定义态与运行态需要分离

本文把 Steps、Executors、Artifacts、Rules、State 和 History 都放在 Workflow 下，容易混淆可编辑定义与具体执行数据。

建议至少拆分为：

```text
WorkflowDefinition
  └── WorkflowVersion (immutable)
        ├── Nodes
        ├── Edges
        ├── Schemas
        └── Policies

WorkflowRun
  ├── definition_version
  ├── RunState
  ├── NodeRuns
  │     └── Attempts
  ├── Events / Transitions
  └── ArtifactInstances
```

- `WorkflowDefinition`：工作流逻辑身份和可编辑草稿。
- `WorkflowVersion`：发布后的不可变执行快照。
- `WorkflowRun`：绑定某个版本的一次具体执行。
- `NodeRun`：某个节点在一次运行中的状态。
- `Attempt`：NodeRun 的一次实际调用或重试。
- `Event/Transition`：状态变化和路由历史。
- `ArtifactInstance`：本次运行生成或引用的产物。

Workflow Run 启动后必须绑定不可变版本。之后即使用户修改 WorkflowDefinition，也不能改变已启动 Run 的图结构和执行含义。

## 5. YAML、JSON、AST 与 Runtime Plan 的关系不清晰

YAML 和 JSON 本质上都是序列化格式，不应简单地分别等同于“源码”和“Runtime”。

建议采用以下分层：

```text
YAML / JSON / UI
        |
        v
Parser + Schema Validation
        |
        v
Canonical Workflow IR
        |
        v
Semantic Validation + Compilation
        |
        v
Immutable Runtime Plan
```

需要进一步明确：

- Canonical IR 的字段和语义
- `schema_version` 及升级策略
- YAML、JSON 与 UI 编辑结果的同步规则
- 默认值展开和规范化时机
- 静态类型及引用校验
- 条件表达式编译
- Runtime Plan 是否持久化
- Runtime Plan 与原始定义的摘要或哈希关系
- 配置迁移、回滚和兼容策略

orbit 当前使用 JSON 保存 Workflow 配置。如果引入 YAML，需要明确谁是唯一事实来源，避免 YAML、JSON 和 UI 状态发生冲突。更稳妥的方式是内部只使用一种 Canonical IR，YAML 和 JSON 都作为导入、导出或编辑格式。

## 6. 通用 Workflow DSL 的执行语义尚不完整

当前 Step、Executor 和 Rule 只给出了概念示例，还不足以驱动一个通用工作流引擎。正式 DSL 至少需要定义以下内容。

### 6.1 节点

- 节点 ID、类型、名称和版本
- 输入输出端口
- 输入输出 JSON Schema
- 字段映射及默认值
- 节点配置 Schema
- 执行策略和资源限制
- Timeout、Retry、Backoff
- 幂等键和缓存策略
- 失败及取消行为

### 6.2 Executor / Handler

统一接口至少需要包含：

```text
validate(config)
prepare(context, inputs)
execute(context, inputs)
cancel(execution_id)
recover(execution_id)
normalize_result(raw_result)
```

Executor 类型可以包括：

- Agent
- Tool
- HTTP/API
- Script/Command
- Human
- Decision
- Join
- Foreach
- Subflow
- Transform

每种 Executor 都需要说明输入输出约束、幂等行为、取消能力、恢复能力和安全边界。

### 6.3 Edge 与路由

Edge 至少需要定义：

- `from` / `to`
- 源输出端口与目标输入端口
- 条件表达式
- 数据映射
- 优先级
- 路由类型，例如 forward、rework、error、timeout
- 未选中分支如何终止
- 多条条件同时命中时的行为

条件表达式需要使用受限且可验证的表达式语言，不能直接执行任意代码。还要定义缺失字段、类型不匹配和表达式异常时的处理方式。

### 6.4 并行与 Join

需要定义：

- 并行分支如何创建
- `all`、`any`、`n-of-m` 等 Join 策略
- 可选分支和未选中分支是否计入 Join
- 分支失败、取消和超时时的 Join 行为
- 多分支输出冲突时如何合并
- Join 是否允许部分结果

### 6.5 Foreach

需要定义：

- 集合来源和 Item Schema
- Item Key
- 单项输入映射
- 并发上限
- 顺序执行或并行执行
- 单项 Retry
- Fail-fast 或继续处理
- 部分成功语义
- 结果排序和聚合 Schema
- 单项运行的 Correlation ID

### 6.6 Subflow

需要定义：

- 子流程版本绑定
- 父子输入输出映射
- 父子运行关系
- 状态、取消和失败传播
- 递归限制
- 子流程内 Artifact 的可见范围

## 7. State 的作用域与并发合并规则缺失

“不同步骤读取和更新状态”还不足以定义一个可预测的运行模型。

建议按作用域拆分状态：

- Workflow Input：运行启动时的不可变输入。
- Run State：当前运行的共享结构化状态。
- Node Input：映射后传给节点的只读输入。
- Node Output：节点执行后产生的结构化输出。
- Item Scope：Foreach 单项的局部状态。
- Secret Scope：仅执行器可读取、不可写入日志和普通状态的数据。

对于并行节点，必须定义：

- 是否允许直接修改共享 State
- 乐观锁或版本检查
- 字段级冲突处理
- Merge/Reducer 策略
- 写入顺序是否影响最终结果
- Run 恢复后如何保证结果一致

更安全的默认值是：节点只产生输出事件，不直接原地修改全局 State，由引擎使用确定性的映射或 Reducer 合并结果。

## 8. 可靠性与恢复模型缺失

通用 Workflow Runtime 必须假设进程可能在任意时刻退出。需要补充：

- Run、NodeRun 和 Attempt 的状态机
- 状态更新与事件写入的事务边界
- 调度任务的 Lease 和续租机制
- 重复投递及幂等处理
- Runner 崩溃后的恢复规则
- 外部请求已经成功但本地未记录时的处理
- 人工节点长期暂停后的恢复
- Run 取消及向下游传播
- Dead Letter 或人工接管机制
- 重启后重新构建待调度队列的方法

执行历史应使用持久化事件或 Transition 记录，而不是只依赖当前状态字段。

## 9. Human 节点需要完整的暂停和审批协议

Human 不只是另一种 Executor。它通常会让 Workflow 长时间暂停，因此需要单独定义：

- 审批请求 ID
- 审批人或审批组
- 可执行动作，例如 approve、reject、rework、cancel
- 表单 Schema
- 审批意见和附件
- 截止时间
- 提醒和升级策略
- 权限及身份校验
- 重复提交保护
- 审批后的恢复点
- 审计记录

还要区分节点本身是 Human Task，还是自动节点完成后需要 Human Approval Gate。这两种模式的 UI 和运行语义不同。

## 10. 安全、权限和资源治理缺失

当 Executor 可以调用 Agent、API、Script 和 Tool 时，需要明确安全边界：

- Secret 只通过受控引用传递
- 日志和 Prompt 自动脱敏
- Script/Command 沙箱
- 文件系统和网络权限
- Tool Allowlist
- Artifact 访问控制
- 人工审批权限
- 最大运行时间
- Token、费用、CPU、内存和并发额度
- 审计日志
- 不可信 Workflow 的导入校验

Workflow Definition 不能直接携带明文 Secret，也不能允许条件表达式或数据映射执行任意代码。

## 11. Timeline 不适合作为所有流程的唯一默认视图

Timeline 非常适合线性流程，但对并行分支、Join、Foreach 和动态子流程的表达能力有限。建议将 UI 定义为同一运行模型的多种投影：

- `List/Timeline`：展示运行进度、等待项、异常和人工操作。
- `Pipeline`：用于编辑和理解简单线性流程。
- `Graph`：用于条件、并行、循环和复杂数据关系。
- `Run Detail`：展示 NodeRun、Attempt、输入输出、日志和 Artifact。

系统可以根据流程复杂度自动选择默认视图：

- 线性流程默认 Timeline 或 Pipeline。
- 少量分支使用带分组的 Timeline。
- 出现并行 Join、Foreach、Subflow 或复杂循环时默认 Graph。

Graph 不应只是专家模式；在复杂流程中，它是避免信息丢失所必需的运行视图。

## 12. “Pipeline 可覆盖大部分场景”需要限定边界

“一般开发流程不需要复杂 Graph”和“Pipeline + Artifact + Loop 可以覆盖大部分场景”可以作为产品设计判断，但不能作为 Runtime 能力裁剪的依据。

更准确的表达是：

> UI 应优先使用简单的 Pipeline 或 Timeline 呈现常见流程，但 Runtime 应采用能够表达条件、并行、循环、Join、Foreach 和 Subflow 的通用图模型。

即使默认模板是线性的，底层模型也不应限制为线性 Pipeline，否则以后扩展业务审批、数据处理、内容生产和 Research Workflow 时需要再次改造核心数据模型。

## 13. 文档结构仍带有生成稿痕迹

正式版本需要清理：

- 开头“下面整理成一篇……”等对话式说明
- `:::writing` 包装标记
- 结尾“如果后续继续设计……”等临时措辞
- 过多空行和不统一的标题层级
- 只有概念、没有字段定义的示例

框架介绍还应注明信息来源和适用版本，避免随着框架演进而失真。对于“更适合”“覆盖大部分场景”等判断，应明确其假设、目标用户和适用边界。

## 14. 与 orbit 当前实现的衔接

orbit 当前已经具备部分工作流能力，包括：

- JSON Workflow 配置
- Step 与显式 Edge
- Rework 路由
- Required Step 与 Merge 等待
- Agent 调度
- Timeout 和有限返工次数
- Human Approval
- Step Input、Result Summary 和 Artifact 列表
- Task Transition 与 Attempt 记录

新设计不应直接替换这些能力，而应提供兼容升级路径：

1. 为现有 Workflow JSON 增加 `schema_version`。
2. 将现有 Step 迁移为统一 Node Definition。
3. 将 `agents`、`verify`、`decompose`、`integrate` 等字段映射到 Handler 配置或内置 Node Type。
4. 保留现有显式 Rework Edge，补充 Condition 和 Mapping。
5. 把现有 `step_inputs`、`result_summary`、`artifacts` 迁移为端口输出及 Artifact Reference。
6. 为每次 Run 保存不可变 Workflow Version 或 Snapshot。
7. 保留旧配置读取兼容层，新写入统一使用新版 Canonical IR。
8. 在 Runtime 稳定后再增加 YAML 导入、导出，避免同时维护两个事实来源。

## 15. 建议新增的正式规范文档

本文适合定位为《Workflow 设计原则与产品方向》。为了支持实现，还需要单独编写《Workflow DSL 与 Runtime Specification》，建议包含：

1. Goals、Non-goals 和术语表
2. Canonical Workflow IR
3. Workflow Definition 与 Version Schema
4. Node、Port、Edge 和 Mapping Schema
5. Handler/Executor Protocol
6. Condition 与表达式规范
7. Retry、Rework、Iteration、Foreach 和 Subflow 语义
8. Join 与并行执行语义
9. Workflow Run、NodeRun、Attempt 状态机
10. State、Artifact 和数据血缘
11. Human Task 与 Approval Protocol
12. Persistence、Recovery 和 Idempotency
13. Security、Secret 和 Resource Policy
14. UI Projection 与 API
15. Schema Version、Migration 和向后兼容
16. 完整示例、错误示例和验收标准

在上述规范完成前，本文中的模型应被视为方向性建议，而不是已经冻结的实现契约。
