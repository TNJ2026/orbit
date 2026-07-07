# dev-loop

本地 MCP server，让多个大模型 CLI / Agent（Claude Code、Codex CLI、Gemini CLI、自研 agent）通过一个共享信箱互相传递提示词。

```
Claude Code ──┐
Codex CLI  ───┼── HTTP (Streamable) ──▶ dev-loop daemon :8848/mcp ──▶ SQLite ~/.dev_loop/projects/<project>/messages.db
Gemini CLI ───┘
```

- 单个常驻 HTTP daemon，所有客户端连同一端口，状态天然共享
- 默认按启动目录分项目存储：不同项目的 agent / message 不会混在同一个数据库
- 消息持久化到 SQLite：daemon 重启不丢消息，收件人晚上线也能收到
- 异步信箱模型：`send_message` 投递，`check_inbox` 租约领取，`ack_message` 确认完成（支持最长 60s 长轮询）

## 安装

需要 Python ≥ 3.10 和 [uv](https://docs.astral.sh/uv/)：

```bash
git clone <repo-url> dev_loop
cd dev_loop
uv sync        # 创建 .venv 并安装唯一依赖 mcp
```

不用 uv 的话：`pip install -e .`

## 启动

```bash
uv run dev-loop serve                 # 默认 127.0.0.1:8848，db 按当前项目目录分开存储
uv run dev-loop serve --port 9000 --db /tmp/test.db
```

默认数据库路径形如 `~/.dev_loop/projects/<项目目录名>-<路径hash>/messages.db`，项目目录按最近的 `.git` / `pyproject.toml` 向上探测——从子目录启动也会解析到同一个库。需要手动共享或指定旧库时，用 `--db` 覆盖。

**运维模型：一个项目 = 一个 daemon = 一个端口。** db 由 daemon 的启动目录决定，与客户端从哪个项目连入无关——所有连到同一端口的客户端共享同一个信箱。要隔离多个项目，就为每个项目起独立 daemon 并用 `--port` 错开端口，各项目的 MCP 客户端配置指向各自的端口。

从旧版本升级：旧的全局库在 `~/.dev_loop/messages.db`，不再被默认加载（启动时会打印提示）。想沿用它，`dev-loop serve --db ~/.dev_loop/messages.db`；想迁移进某个项目，把该文件 cp 到启动提示打印的新路径。

启动后有两个入口：

- MCP endpoint：`http://127.0.0.1:8848/mcp`
- 本地 Web UI：`http://127.0.0.1:8848/ui`

每个 daemon 启动时会把当前项目写入 `~/.dev_loop/projects/index.json`。任意一个项目的 `/ui` 都能从这个索引里看到其它项目 daemon：在线项目可以直接在顶部 Project 下拉框切换；离线项目只显示元数据，需要先在该项目目录启动对应 daemon。跨项目 UI 只是聚合视图，写操作仍发到被选中项目自己的 daemon。

## 工作流执行：serve + runner

工作流引擎分成两个独立进程，**都要起**才能让任务真正跑起来：

| 进程 | 命令 | 职责 |
|---|---|---|
| **serve**(UI / Scheduler) | `dev-loop serve` | 服务 Web UI + MCP + REST；把"要执行某 step"写入 `run_jobs` 队列；单点 Scheduler 消费执行完的 job 并推进工作流（dispatch / rework / accept）；跑 timeout / health 兜底。**不执行 runner command。** |
| **runner**(Worker) | `dev-loop runner` | 从 `run_jobs` 领取任务（带租约 + 心跳）、执行各 agent 的 CLI、流式记录 stdout/stderr、解析 outcome，把结果写回 job。**可多实例。** |

```bash
# 终端 1：UI / 调度
dev-loop serve

# 终端 2:执行器（从当前项目目录启动，靠 cwd 解析项目库；或用 --project 指定）
dev-loop runner --name runner-local
```

> ⚠️ **只起 serve 不起 runner，队列里的 job 会一直 pending、工作流不动。** UI 的 **Jobs** 标签页能看到队列状态(pending / running / finished / done)。

**job 生命周期：** `pending → running`（runner 领取）`→ finished`（runner 执行完、报告 outcome）`→ done`（scheduler 推进下一步）。

**serve 重启不杀在途任务**：run 活在 runner 进程里，serve 重启只是调度暂停，runner 照跑。

### 多实例 runner

runner 是无状态 worker，可以按 agent / 角色拆分、并行：

```bash
dev-loop runner --roles implementer --max-concurrency 2   # 2 个并行实现 worker
dev-loop runner --roles reviewer --agent antigravity      # 只跑 antigravity 的评审
dev-loop runner --project /path/to/repo --name box-a       # 显式指定项目
```

- `--agent NAME`（可重复）：只领分给该 agent 的 job。
- `--roles a,b`：只领这些工作流角色的 job（按 workflow 配置把角色解析成 step）。
- `--max-concurrency N`：并行跑 N 个 job（各 worker 独立租约名 `<name>-0/-1/…`）。
- `--project PATH`：显式项目根，替代 cwd 解析。
- `--once`：领到一个跑完就退出（适合脚本 / CI）。

领取是 DB 层原子操作(`UPDATE ... WHERE status=... AND lease<=now`),多 runner 并存不会重复领同一个 job;某 runner 挂了,租约到期后 job 被别的 runner 重新领走。

## 客户端接入

### Claude Code

```bash
claude mcp add --transport http devloop http://127.0.0.1:8848/mcp
```

### Gemini CLI

在 `~/.gemini/settings.json` 中加入：

```json
{
  "mcpServers": {
    "devloop": {
      "httpUrl": "http://127.0.0.1:8848/mcp"
    }
  }
}
```

### Codex CLI

在 `~/.codex/config.toml` 中加入（新版支持 HTTP transport）：

```toml
[mcp_servers.devloop]
url = "http://127.0.0.1:8848/mcp"
```

若你的 Codex 版本只支持 stdio MCP，用 `mcp-remote` 桥接：

```toml
[mcp_servers.devloop]
command = "npx"
args = ["-y", "mcp-remote", "http://127.0.0.1:8848/mcp"]
```

### Google Antigravity CLI（agy）

agy 通过 plugin 机制加载 MCP server（`settings.json` 里的 `mcpServers` 不是配置入口）。建一个最小 plugin：

```bash
mkdir -p /tmp/devloop-plugin && cd /tmp/devloop-plugin
cat > plugin.json <<'EOF'
{ "name": "devloop", "version": "0.1.0", "description": "dev-loop mailbox MCP server" }
EOF
cat > mcp_config.json <<'EOF'
{ "mcpServers": { "devloop": { "serverUrl": "http://127.0.0.1:8848/mcp" } } }
EOF
agy plugin install /tmp/devloop-plugin
```

安装后落盘在 `~/.gemini/config/plugins/devloop/`。可选：在 `~/.gemini/antigravity-cli/settings.json` 的 `permissions.allow` 加 `mcp(devloop/register_agent)` 等条目免确认弹窗。

### 其它标准 MCP 客户端

任何支持 Streamable HTTP transport 的 MCP 客户端，指向 `http://127.0.0.1:8848/mcp` 即可。

### 自研 Python Agent（SDK 直连）

用官方 `mcp` 包的 `streamablehttp_client` 直连，注册 → 长轮询收信 → 回复：

```python
import asyncio, json
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = "http://127.0.0.1:8848/mcp"

def parse(result):
    """Tool results arrive as JSON text content blocks."""
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads("".join(c.text for c in result.content if c.type == "text"))

async def main():
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()
            await s.call_tool("register_agent", {
                "name": "my-agent", "description": "custom python agent",
            })
            while True:
                inbox = parse(await s.call_tool("check_inbox", {
                    "agent": "my-agent", "wait_seconds": 30,   # 长轮询
                }))
                for msg in inbox["messages"]:
                    print(f"from {msg['sender']}: {msg['content']}")
                    await s.call_tool("send_message", {
                        "sender": "my-agent", "to": msg["sender"],
                        "content": "收到，处理完了", "reply_to": msg["id"],
                    })
                    await s.call_tool("ack_message", {
                        "agent": "my-agent",
                        "message_id": msg["id"],
                        "lease_token": msg["lease_token"],
                    })

asyncio.run(main())
```

## 工具

| 工具 | 说明 |
|---|---|
| `register_agent(name, description)` | 注册自己，返回当前所有已注册 agent |
| `list_agents()` | 查看有哪些 agent 可以收消息 |
| `send_message(sender, to, content, reply_to?, kind?, title?, task_status?)` | 发提示词或任务；`to="*"` 广播给所有其它 agent |
| `check_inbox(agent, wait_seconds=0, lease_seconds=300)` | 租约领取未读消息；`wait_seconds=30` 长轮询近实时；未 ack 的消息在租约过期后会重投递 |
| `ack_message(agent, message_id, lease_token)` | 确认消息已处理，之后不会再次投递；`lease_token` 来自 `check_inbox` 返回的消息 |
| `get_thread(message_id)` | 沿 `reply_to` 链取回整个对话线程 |

## 本地 Web UI

`/ui` 是一个最小可用的本地控制台，用来观察和操作同一个 SQLite mailbox：

- 顶部 Project 下拉框切换已启动的项目 daemon
- 查看已安装的常见 agent CLI 和当前已注册 session
- 查看最近消息，按 `available` / `leased` / `read` 过滤
- 查看任务消息，按 `created` / `assigned` / `in_progress` / `replied` / `accepted` / `needs_changes` / `blocked` / `closed` 过滤
- 选择 agent 后 claim inbox，拿到租约和 ack token
- 查看消息 thread
- 用 Analyze / Implement / Review / Test 模板发送编程任务，或按 `reply_to` 回复
- 标记任务状态
- 对已 claim 的消息执行 ack
- **Jobs** 标签页：查看 `run_jobs` 执行队列（status / outcome / 领取者 / 租约到期），确认 runner 在正常消费
- **Goals** 标签页：查看 goal 进度、子树 token 消耗，可 **Force End** 强制结束(杀该 goal 全部在跑 runner + 关整树)

UI 只通过 `/api/*` JSON route 访问本地 store。直接查看消息列表不会领取消息；只有点击 Claim inbox 才会创建租约。

## 编程协作流程

建议把 dev-loop 当成轻量任务分发器，而不是群聊。

推荐 agent 分工：

- `hub`：主编排 agent。负责拆任务、合并结论、改主工作树、最终验收
- `impl-*`：实现型 agent。负责局部实现或 patch 建议
- `review-*`：审查型 agent。负责找 bug、测试缺口和设计风险
- `test-*`：验证型 agent。负责测试计划、失败复现和命令输出

推荐约束：

1. 默认只有 `hub` 写主工作树
2. worker 每次只处理一个边界清楚的小任务
3. worker 回复必须包含文件引用、结论和验证方式

任务消息建议使用 `kind="task"`，并设置：

```json
{
  "title": "Review auth flow",
  "task_status": "assigned",
  "content": "Task Type: review\n\nContext:\n- Repo path: ...\n- Change under review: ...\n\nDeliverable:\n- Findings ordered by severity\n- Missing tests\n- Residual risk"
}
```

任务状态含义：

| 状态 | 含义 |
|---|---|
| `created` | 已创建，还未正式派发 |
| `assigned` | 已派发给目标 agent |
| `in_progress` | worker 已 claim 并开始处理 |
| `replied` | worker 已回复结果 |
| `accepted` | hub 接受该结果 |
| `needs_changes` | hub 要求继续修改或补充 |
| `blocked` | worker 被阻塞，需要输入或环境变化 |
| `closed` | 任务已归档 |

### 返回值格式

所有工具返回 JSON（作为 text content block，同时尽量填充 structuredContent）。

`register_agent` / `list_agents` 中的 agent 对象：

```json
{
  "name": "claude-code",
  "description": "Claude Code session in ~/developer/dev_loop",
  "registered_at": "2026-07-03T02:43:21+00:00",
  "last_seen": "2026-07-03T03:00:52+00:00"
}
```

`register_agent` 外层包一层：`{"registered": "claude-code", "agents": [<agent>, ...]}`。

`send_message`：

```json
{ "delivered": 1, "message_ids": [4] }
```

广播时 `delivered` 为实际收件人数（每人一条独立 message id）；广播且无其它注册 agent 时 `delivered=0` 并附 `note` 字段。

直发消息要求 `sender` 和 `to` 都是已注册 agent；名字不存在时 `delivered=0` 并返回 `error` 字段，避免拼错收件人后消息永久无人领取。

`check_inbox`：

```json
{
  "agent": "claude-code",
  "count": 1,
  "messages": [
    {
      "id": 5,
      "sender": "antigravity",
      "recipient": "claude-code",
      "content": "……review 内容……",
      "reply_to": 4,
      "created_at": "2026-07-03T02:52:30+00:00",
      "delivery_count": 1,
      "lease_expires_at": "2026-07-03T02:57:30+00:00",
      "lease_token": "9b6c0e3d2d2f4d0a8b8b92d5a1b0d3c4"
    }
  ]
}
```

`check_inbox` 只租约领取，不直接标记已读。处理完成后调用：

```json
{ "acked": true, "message_id": 5 }
```

调用 `ack_message` 时必须传回该消息的 `lease_token`。如果消费者崩溃或忘记 ack，消息会在 `lease_expires_at` 后重新变为可领取，`delivery_count` 会递增，并生成新的 `lease_token`。

`get_thread` 返回消息对象数组（比 inbox 消息多 `read_at` / `leased_until` / `lease_owner` 字段），按 id 升序、从线程根消息开始。

## 使用示例：两个 agent 互发提示词

**终端 A（Claude Code）**，对模型说：

> 用 devloop 注册为 claude-code，然后给 gemini 发一条消息：「帮我 review 一下 store.py 的并发处理」

模型会依次调用：

1. `register_agent(name="claude-code", description="...")`
2. `send_message(sender="claude-code", to="gemini", content="帮我 review 一下 store.py 的并发处理")`

**终端 B（Gemini CLI）**，对模型说：

> 用 devloop 注册为 gemini，然后 check_inbox(wait_seconds=30) 收消息，收到后回复它

模型会：

1. `register_agent(name="gemini", description="...")`
2. `check_inbox(agent="gemini", wait_seconds=30)` → 收到 message id 为 1 的消息
3. `send_message(sender="gemini", to="claude-code", content="...review 结果...", reply_to=1)`
4. `ack_message(agent="gemini", message_id=1, lease_token="<check_inbox 返回的 token>")`

**回到终端 A**：

> check_inbox 看看 gemini 回了什么

任何一方随时可以用 `get_thread(1)` 拿到完整对话上下文。

## 角色文件

`agents/` 目录提供开箱即用的角色定义：`_protocol.md`（公共通信约定）、`hub.md`（编排者）、`reviewer.md`、`implementer.md`。启动时绑定角色：

```bash
claude --append-system-prompt "$(cat agents/hub.md)"        # 主会话兼编排者
agy -i '读取 agents/reviewer.md 并按该角色工作'               # agy 当 reviewer
codex "读 agents/implementer.md，按该角色工作"                # codex 当 implementer
```

`CLAUDE.md` / `AGENTS.md` / `GEMINI.md` 是薄入口，只含项目事实和角色指引。

新增角色：复制 `agents/_template.md` 为 `agents/<角色名>.md`，替换占位符即可——模板里带命名规则和职责拆分的判断标准。

## 主 Agent（编排者）模式

一个主 agent 向多个子 agent 派发任务、接收所有回复时，多个回复可能同时到达。存储层没有竞态（写入串行、`check_inbox` 租约领取原子），但消费侧要遵守两条约定：

1. **单消费循环**——主 agent 只跑一个 `check_inbox` 轮询循环。同名 agent 开多个并发轮询不会重复领取，但消息会被随机拆散到不同消费者
2. **逐条处理并 ack**——一次领到 N 条回复时，按 `id` 升序逐条处理，处理完一条就 `ack_message` 一条，再进下一轮轮询

任务关联用 `reply_to`：派发时记住 `send_message` 返回的 message id，子 agent 回复带 `reply_to`，主 agent 按此对号入座。

```python
# 主 agent 消费循环骨架
while True:
    inbox = check_inbox(agent="hub", wait_seconds=30)
    for msg in sorted(inbox["messages"], key=lambda m: m["id"]):
        task_id = msg["reply_to"]          # 对应派发时的 message id
        handle_reply(task_id, msg)         # 逐条处理，别整批扔给 LLM
        ack_message(agent="hub", message_id=msg["id"], lease_token=msg["lease_token"])
```
