# devloop 通信协议（所有角色公共约定）

devloop 是本地 MCP 信箱（server 名 `devloop`），用于 agent 间传递提示词。

## 基本流程

1. **注册**：会话开始时调 `register_agent(name=<你的角色名>, description=<一行能力广告>)`。
   name 用稳定 agent/session 名（如 hub-agent / codex / reviewer-1），职责角色由 team 配置里的 `role_id` 绑定。
2. **收信**：`check_inbox(agent=<角色名>, wait_seconds=30)` 长轮询。
   返回的消息是租约领取——不 ack 会在租约过期后重投递（`delivery_count` 递增）。
3. **处理**：按 `id` 升序逐条处理。收到 `delivery_count > 1` 的消息说明上次处理中断，先检查是否已做过一半。
4. **回复**：`send_message(sender=<角色名>, to=<对方>, content=..., reply_to=<收到的消息id>)`。
5. **确认**：每处理完一条立即 `ack_message(agent=<角色名>, message_id=<id>, lease_token=<该消息的 lease_token>)`。

## 消息内容约定

- **产物写文件，消息发指针**：报告、代码、长文本写到仓库文件，消息只发「一行结论 + 文件路径」。消息正文超过 ~20 行就该落盘。
- **里程碑汇报**：只在状态变化时主动发消息——完成 / 阻塞 / 需要决策。不发进度流水。
- **任务对号**：回复必须带 `reply_to`。派发方以 `send_message` 返回的 message id 作为任务 id。

## 工作流任务（workflow 引擎派发）

收到 sender 为 `workflow`、正文以 `[workflow step: <step>]` 开头的消息时，任务由流程引擎自动路由：

1. 按消息里的角色与步骤要求干活，产物照常写文件。
2. 完成后**不用 send_message 回复**，改调 `complete_step(agent=<你>, task_id=<id>, step=<step>, outcome=..., result=<一行结论+产物路径>)`：
   - `outcome="done"`：通过，引擎沿流程派发下一步（汇合步骤会等齐所有必需分支）。
   - `outcome="rework"`：打回上游（如 review 打回 implement），`result` 写明原因。
   - `outcome="blocked"`：无法决定/需要选择时用，`result` 写卡点与候选项；任务挂起并通知 hub。
3. 然后照常 `ack_message`。只有收到该步骤派发的 agent 能完成当前 active 步骤；hub 可代为完成 active 步骤用于恢复。

## 禁止

- 同一角色名开多个并发 `check_inbox` 循环。
- 跳过 ack（除非确实没处理完，留给重投递）。
- 把整批消息不加区分地一次性总结——逐条处理。
