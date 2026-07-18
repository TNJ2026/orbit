# Future Agent Workflow UI 设计理念

## 1. 背景

传统 Workflow 系统通常采用流程图方式：

```
Node → Node → Node
```

用户通过拖拽节点、连接连线来设计流程。

这种方式适合：

- BPMN 流程
- 企业审批
- 固定自动化任务

但对于未来的 Agent Workflow：

- 流程可能由 AI 动态生成
- Agent 会根据执行结果调整计划
- Workflow 会持续演进

因此，UI 不应该只是一个“流程编辑器”，而应该成为：

> AI Agent 任务的控制中心（Mission Control）

---

# 2. 核心交互范式变化

未来 Workflow 的核心变化：

从：

```
人设计流程
      ↓
系统执行流程
```

变成：

```
人定义目标
      ↓
AI生成计划
      ↓
Agent执行
      ↓
Workflow动态优化
```

因此 UI 应围绕三个阶段：

```
Design
  |
Run
  |
Evolve
```

---

# 3. 三层 UI 架构

## 第一层：Goal Layer（目标层）

用户首先表达目标，而不是创建流程。

例如：

```
完成一次市场调研报告
```

系统生成：

```
Execution Plan

✓ 收集数据
✓ 分析市场
✓ 生成报告
○ 人工审核
```

用户确认后执行。

核心交互：

```
What do you want to accomplish?
```

---

# 4. 第二层：Workflow Plan（计划层）

Workflow 不应该默认展示复杂 Graph。

推荐使用 Pipeline / Timeline：

```
Research Agent

        ↓

Analysis Agent

        ↓

Writer Agent

        ↓

Human Review
```

每个步骤显示：

- 目标
- Agent
- 输入
- 输出
- 策略

例如：

```
┌─────────────────┐
│ Research        │
│                 │
│ Agent           │
│ Claude          │
│                 │
│ Input           │
│ Documents       │
│                 │
│ Output          │
│ Research Report │
└─────────────────┘
```

---

# 5. Step 卡片设计

Step 不应该展示技术信息：

例如：

```
Node ID:
abc123
```

而应该展示用户关心的信息：

```
为什么存在？
谁执行？
产生什么？
失败怎么办？
```

推荐结构：

```
┌──────────────────────┐
│ 🧠 Research           │
│                      │
│ Goal                 │
│ Find market signals  │
│                      │
│ Agent                │
│ Claude               │
│                      │
│ Creates              │
│ 📄 Research Report   │
│                      │
│ Policy               │
│ Retry ×3             │
└──────────────────────┘
```

---

# 6. 第三层：Execution View（执行层）

执行过程应该采用 Timeline，而不是简单节点变色。

例如：

```
AI Run #1024


09:01

Planner Agent

Created execution plan


↓

09:02

Research Agent

Searching data

Created:
market_data.json


↓

09:05

Writer Agent

Generating report


↓

09:07

Reviewer Agent

Found missing information


Decision:

Add additional research step
```

重点展示：

- Agent 做了什么
- 为什么这样决定
- 产生了什么结果

---

# 7. Rework / Loop 展示

传统方式：

```
Test Failed
    |
    ↓
Back to Coding
```

容易形成复杂流程图。

未来应该展示为：

## Workflow Evolution

例如：

```
Version 1

Research
 ↓
Write
 ↓
Review Failed


Version 2

Research
 ↓
Deep Research
 ↓
Write
 ↓
Review Passed
```

表达：

> Workflow 根据反馈进行了自我优化。

---

# 8. Artifact Explorer

未来 Workflow 的核心不是节点，而是 Artifact。

增加 Artifact 视图：

```
Artifacts


📄 requirement.md

Created by:
Planner


↓

📄 design.md

Created by:
Architect


↓

💻 source.zip

Created by:
Coder


↓

📊 test-report.json

Created by:
Tester
```

Artifact 体现：

- 数据流
- 知识积累
- Agent 协作结果

---

# 9. Agent Decision 面板

区别普通 Workflow 的关键能力：

展示 Agent 决策过程。

例如：

```
Agent Decision


Current Situation:

Test Failed


Options:

1. Retry coding
2. Ask user
3. Redesign


Selected:

Retry coding


Reason:

Failure caused by missing validation
```

用户可以理解：

- Agent 为什么行动
- 是否需要人工干预

---

# 10. 高级模式：Graph View

Graph 不应该消失，而应该成为高级模式。

例如：

```
        Planner

       /      \

Research     Code

       \      /

        Review
```

适合：

- 专家用户
- Workflow 调试
- 系统分析

默认用户不需要看到。

---

# 11. 视觉设计方向

不建议：

- BPMN 企业流程风格
- 大量节点连线
- 复杂颜色分类

推荐：

## IDE + Mission Control 风格

参考：

- VS Code
- GitHub Actions
- Figma
- Linear


设计特点：

```
Dark Canvas

+
Minimal Cards

+
Live Agent Activity

+
Artifact Graph

+
Execution Timeline
```

---

# 12. 最终产品形态

未来 Agent Workflow UI：

```
                User Goal

                    ↓


              AI Planner

                    ↓


          Workflow Timeline

                    ↓


          Agent Execution Stream

                    ↓


          Artifact Evolution

                    ↓


          Workflow Improvement
```

---

# 13. 产品定位

未来不应该做：

```
AI版 n8n
```

而应该做：

```
AI Engineering Mission Control
```

核心能力：

- 目标驱动 Workflow
- AI 自动规划
- Agent 协作执行
- Artifact 管理
- Workflow 自我优化

---

# 总结

未来 Workflow UI 的核心变化：

|传统 Workflow|未来 Agent Workflow|
|-|-|
|用户设计流程|用户定义目标|
|节点连接|AI生成计划|
|固定路径|动态调整|
|流程状态|Agent行为轨迹|
|任务完成|持续优化|
|Node中心|Artifact中心|

未来最重要的不是“画流程”，而是：

> **让用户理解、控制和信任一个能够自主执行和演进的 AI 系统。**