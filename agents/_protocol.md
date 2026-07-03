# devloop 通信协议（所有角色公共约定）

devloop 是本地 MCP 信箱（server 名 `devloop`），用于 agent 间传递提示词。

## 基本流程

1. **注册**：会话开始时调 `register_agent(name=<你的角色名>, description=<一行能力广告>)`。
   name 用角色名（hub / reviewer / implementer），不用模型名。
2. **收信**：`check_inbox(agent=<角色名>, wait_seconds=30)` 长轮询。
   返回的消息是租约领取——不 ack 会在租约过期后重投递（`delivery_count` 递增）。
3. **处理**：按 `id` 升序逐条处理。收到 `delivery_count > 1` 的消息说明上次处理中断，先检查是否已做过一半。
4. **回复**：`send_message(sender=<角色名>, to=<对方>, content=..., reply_to=<收到的消息id>)`。
5. **确认**：每处理完一条立即 `ack_message(agent=<角色名>, message_id=<id>, lease_token=<该消息的 lease_token>)`。

## 消息内容约定

- **产物写文件，消息发指针**：报告、代码、长文本写到仓库文件，消息只发「一行结论 + 文件路径」。消息正文超过 ~20 行就该落盘。
- **里程碑汇报**：只在状态变化时主动发消息——完成 / 阻塞 / 需要决策。不发进度流水。
- **任务对号**：回复必须带 `reply_to`。派发方以 `send_message` 返回的 message id 作为任务 id。

## 禁止

- 同一角色名开多个并发 `check_inbox` 循环。
- 跳过 ack（除非确实没处理完，留给重投递）。
- 把整批消息不加区分地一次性总结——逐条处理。
