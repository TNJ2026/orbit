# orbit 角色执行协议（所有角色公共约定）

orbit 是本地多 agent 工作流编排器。任务在一张工作流图上流转，每个步骤绑定一个角色；runner 把该步骤的 prompt 交给对应的 agent CLI 执行。**你是被引擎派发的一次性 worker**——收到一个步骤 prompt，干完即退出。没有信箱：不注册、不轮询、不收发消息。

## 执行流程

1. **接收**：启动时你已拿到本步骤的完整 prompt（角色说明 + 任务描述 + 上游产出）。不需要 `register_agent` / `check_inbox`。
2. **工作目录**：
   - 普通步骤：当前目录就是项目根，直接读写文件。
   - 隔离步骤（implement / test / review 等）：当前目录是本任务专属 git worktree（分支 `orbit/task-<id>`），与其它任务隔离。完成后 `git add -A && git commit` 到该分支。
   - 集成步骤（integrate）：在主工作树把任务分支合并回主干（prompt 里有具体步骤）。
3. **产物写文件**：报告、代码、长文本写进仓库文件；输出里只留「一行结论 + 文件路径」。
4. **汇报结果**：以当前步骤 prompt 最后的“输出协议”为最高优先级。普通步骤通常要求在输出最后打印裁决：
   - `WORKFLOW_OUTCOME: done` —— 本步成功，引擎派发下一步（汇合步骤会等齐所有必需分支）。
   - `WORKFLOW_OUTCOME: rework` —— 成果不达标，打回上游重做（如 review 打回 implement），原因写清。仅在本步有返工回环时可用。
   - `WORKFLOW_OUTCOME: blocked` —— 无法完成或需要决策（缺信息 / 环境损坏 / 依赖未满足 / 测试失败无法修复），暂停并通知 hub。即使进程正常退出也要用它标记失败。
   - 不打印该行默认视为 `done`。
   - 特殊步骤可能要求严格格式，例如 Decompose 只允许一个 JSON 对象；此时不要追加 `WORKFLOW_OUTCOME`、token 或任何其他文本。
5. **token 用量**：仅当当前步骤的输出协议允许或要求时，另起一行 `TOKENS_USED: <数字>`；严格格式步骤不要追加。

## 不要做

- 不要调用 `register_agent` / `check_inbox` / `send_message` / `ack_message` / `complete_step`——这些不需要你调，派发器会代为提交结果。
- 不要手动删除 worktree 或分支，引擎会自动回收。
- 不要越界：只做本步骤指定的事；范围外发现的问题，在结论里提一句即可。
