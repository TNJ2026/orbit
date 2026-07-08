# orbit

**简体中文** | [English](./README.md)

> 本地 MCP server，让多个大模型 CLI / Agent（Claude Code、Codex CLI、Gemini CLI、自研 agent）通过一个共享信箱互相传递提示词，并跑多角色任务工作流。

```
Claude Code ──┐
Codex CLI  ───┼── HTTP (Streamable) ──▶ orbit daemon :8848/mcp ──▶ SQLite ~/.orbit/projects/<project>/messages.db
Gemini CLI ───┘
```

- 单个常驻 HTTP daemon，所有客户端连同一端口，状态天然共享
- 默认按启动目录分项目存储：不同项目的 agent / message 不会混在同一个数据库
- 消息持久化到 SQLite：daemon 重启不丢消息，收件人晚上线也能收到
- 异步信箱模型：`send_message` 投递，`check_inbox` 租约领取，`ack_message` 确认完成（支持最长 60s 长轮询）

## 目录

- [安装](#安装)
- [启动](#启动)
- [工作流引擎](#工作流引擎)
- [客户端接入](#客户端接入)
- [MCP 工具](#mcp-工具)
- [本地 Web UI](#本地-web-ui)
- [任务协作模型](#任务协作模型)
- [编排者模式](#编排者模式)
- [角色文件](#角色文件)
- [附录：工具返回值格式](#附录工具返回值格式)

## 安装

需要 Python ≥ 3.10 和 [uv](https://docs.astral.sh/uv/)：

```bash
git clone <repo-url> orbit
cd orbit
uv sync        # 创建 .venv 并安装唯一依赖 mcp
```

不用 uv 的话：`pip install -e .`

## 启动

```bash
uv run orbit serve                 # 默认 127.0.0.1:8848，db 按当前项目目录分开存储
uv run orbit serve --port 9000 --db /tmp/test.db
```

三种起法，按需求选：

| 命令 | 会往仓库写文件吗 | 适用 |
|---|---|---|
| `orbit up` | 只补 `.gitignore`（忽略 `.orbit/`） | 别的仓库零配置快速用，包内自带角色/工作流默认值 |
| `orbit serve` | 否 | 已 `init` 过、或直接用默认值 |
| `orbit init` + `orbit serve` | 写角色/工作流/配置文件 | 改角色提示词、自定义工作流并 commit 给团队共享 |

### 在别的仓库里零配置起

不想往别的仓库里复制角色/配置文件时，用 `orbit up`：它先把状态目录（`.orbit/`）写进该仓库的 `.gitignore`，再用**包内自带的角色和工作流默认值**直接 serve——不落任何需要提交的文件。`serve` 支持的参数（`--host` / `--port` / `--db` / `--no-runner` / `--runner-concurrency`）它都支持。

```bash
orbit up                                   # 已装 orbit
uvx --from git+<repo-url> orbit up         # 没装也行，uvx 临时拉起
```

### 在仓库内定制

需要改角色提示词、自定义工作流并提交给团队共享时，用 `orbit init`：它会把 `agents/*.md`、`.orbit/workflow.json`、`team.json`、`.mcp.json`、`CLAUDE.md` 段落写进仓库，这些是**故意不 gitignore** 的，供 commit 共享。

### 数据库与运维模型

默认数据库路径形如 `~/.orbit/projects/<项目目录名>-<路径hash>/messages.db`，项目目录按最近的 `.git` / `pyproject.toml` 向上探测——从子目录启动也会解析到同一个库。需要手动共享或指定旧库时，用 `--db` 覆盖。

**一个项目 = 一个 daemon = 一个端口。** db 由 daemon 的启动目录决定，与客户端从哪个项目连入无关——所有连到同一端口的客户端共享同一个信箱。要隔离多个项目，就为每个项目起独立 daemon 并用 `--port` 错开端口，各项目的 MCP 客户端配置指向各自的端口。

从旧版本升级：旧的全局库在 `~/.dev_loop/messages.db`，不再被默认加载（启动时会打印提示）。想沿用它，`orbit serve --db ~/.dev_loop/messages.db`；想迁移进某个项目，把该文件 cp 到启动提示打印的新路径。

### 访问入口

启动后有两个入口：

- MCP endpoint：`http://127.0.0.1:8848/mcp`
- 本地 Web UI：`http://127.0.0.1:8848/ui`

每个 daemon 启动时会把当前项目写入 `~/.orbit/projects/index.json`。任意一个项目的 `/ui` 都能从这个索引里看到其它项目 daemon：在线项目可以直接在顶部 Project 下拉框切换；离线项目只显示元数据，需要先在该项目目录启动对应 daemon。跨项目 UI 只是聚合视图，写操作仍发到被选中项目自己的 daemon。

## 工作流引擎

工作流引擎逻辑上是三层——**Scheduler**（决定下一步、推进）、**Runner/Worker**（执行 agent CLI）、以及它们之间的 `run_jobs` 队列。默认打包进一个进程，也可以拆开跑：

| 层 | 职责 |
|---|---|
| **Scheduler**（serve 内线程） | 把"要执行某 step"写入 `run_jobs` 队列；单点消费执行完的 job 并推进工作流（dispatch / rework / accept）；跑 timeout / health 兜底 |
| **Runner / Worker** | 从 `run_jobs` 领取任务（带租约 + 心跳）、执行各 agent 的 CLI、流式记录 stdout/stderr、解析 outcome，把结果写回 job |

### 默认：一体进程

```bash
orbit serve        # UI + MCP + Scheduler + 内嵌 Runner，全在一个进程
```

`serve` 默认**内嵌一个 in-process runner**（名字 `serve-embedded`，并发 5），所以启动一个 goal 后不需要再手动起 runner——建 job → 内嵌 runner 执行 → scheduler 推进，全自动。UI 的 **Jobs** 标签页能看到队列状态(pending / running / finished / done)。

> ⚠️ 内嵌 runner 与 serve 同生命周期：**serve 重启会中断在途 step**（租约到期后该 step 自动重跑）。要重启安全 / 多机 / 水平扩展，用下面的解耦模式。

**job 生命周期：** `pending → running`（runner 领取）`→ finished`（runner 执行完、报告 outcome）`→ done`（scheduler 推进下一步）。

### 解耦：serve 不带 runner + 独立 runner

```bash
# 终端 1：只跑 UI / 调度，不内嵌 worker
orbit serve --no-runner

# 终端 2+：独立 runner（重启 serve 不影响它们；可多实例）
orbit runner --name runner-local
```

此模式下 run 活在独立 runner 进程里，**serve 重启不杀在途任务**——调度暂停一下，runner 照跑。

### 多实例 runner

runner 是无状态 worker，可以按 agent / 角色拆分、并行：

```bash
orbit runner --roles implementer --max-concurrency 2   # 2 个并行实现 worker
orbit runner --roles reviewer --agent antigravity      # 只跑 antigravity 的评审
orbit runner --project /path/to/repo --name box-a       # 显式指定项目
```

- `--agent NAME`（可重复）：只领分给该 agent 的 job。
- `--roles a,b`：只领这些工作流角色的 job（按 workflow 配置把角色解析成 step）。
- `--max-concurrency N`：并行跑 N 个 job，默认 5（各 worker 独立租约名 `<name>-0/-1/…`）。
- `--project PATH`：显式项目根，替代 cwd 解析。
- `--once`：领到一个跑完就退出（适合脚本 / CI）。

领取是 DB 层原子操作(`UPDATE ... WHERE status=... AND lease<=now`),多 runner 并存不会重复领同一个 job;某 runner 挂了,租约到期后 job 被别的 runner 重新领走。

## 客户端接入

### Claude Code

```bash
claude mcp add --transport http orbit http://127.0.0.1:8848/mcp
```

### Gemini CLI

在 `~/.gemini/settings.json` 中加入：

```json
{
  "mcpServers": {
    "orbit": {
      "httpUrl": "http://127.0.0.1:8848/mcp"
    }
  }
}
```

### Codex CLI

在 `~/.codex/config.toml` 中加入（新版支持 HTTP transport）：

```toml
[mcp_servers.orbit]
url = "http://127.0.0.1:8848/mcp"
```

若你的 Codex 版本只支持 stdio MCP，用 `mcp-remote` 桥接：

```toml
[mcp_servers.orbit]
command = "npx"
args = ["-y", "mcp-remote", "http://127.0.0.1:8848/mcp"]
```

### Google Antigravity CLI（agy）

agy 通过 plugin 机制加载 MCP server（`settings.json` 里的 `mcpServers` 不是配置入口）。建一个最小 plugin：

```bash
mkdir -p /tmp/orbit-plugin && cd /tmp/orbit-plugin
cat > plugin.json <<'EOF'
{ "name": "orbit", "version": "0.1.0", "description": "orbit mailbox MCP server" }
EOF
cat > mcp_config.json <<'EOF'
{ "mcpServers": { "orbit": { "serverUrl": "http://127.0.0.1:8848/mcp" } } }
EOF
agy plugin install /tmp/orbit-plugin
```

安装后落盘在 `~/.gemini/config/plugins/orbit/`。可选：在 `~/.gemini/antigravity-cli/settings.json` 的 `permissions.allow` 加 `mcp(orbit/register_agent)` 等条目免确认弹窗。

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

## MCP 工具

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
- 查看任务消息，按 `created` / `assigned` / `in_progress` / `reviewing` / `accepted` / `blocked` / `stalled` / `closed` 过滤
- 选择 agent 后 claim inbox，拿到租约和 ack token
- 查看消息 thread
- 用 Analyze / Implement / Review / Test 模板发送编程任务，或按 `reply_to` 回复
- 标记任务状态
- 对已 claim 的消息执行 ack
- **Jobs** 标签页：查看 `run_jobs` 执行队列（status / outcome / 领取者 / 租约到期），确认 runner 在正常消费
- **Goals** 标签页：查看 goal 进度、子树 token 消耗，可 **Force End** 强制结束(杀该 goal 全部在跑 runner + 关整树)

UI 只通过 `/api/*` JSON route 访问本地 store。直接查看消息列表不会领取消息；只有点击 Claim inbox 才会创建租约。

## 任务协作模型

建议把 orbit 当成轻量任务分发器，而不是群聊。

### 角色分工与约束

推荐 agent 分工：

- `hub`：主编排 agent。负责拆任务、合并结论、改主工作树、最终验收
- `impl-*`：实现型 agent。负责局部实现或 patch 建议
- `review-*`：审查型 agent。负责找 bug、测试缺口和设计风险
- `test-*`：验证型 agent。负责测试计划、失败复现和命令输出

推荐约束：

1. 默认只有 `hub` 写主工作树
2. worker 每次只处理一个边界清楚的小任务
3. worker 回复必须包含文件引用、结论和验证方式

### 任务消息格式

任务消息建议使用 `kind="task"`，并设置：

```json
{
  "title": "Review auth flow",
  "task_status": "assigned",
  "content": "Task Type: review\n\nContext:\n- Repo path: ...\n- Change under review: ...\n\nDeliverable:\n- Findings ordered by severity\n- Missing tests\n- Residual risk"
}
```

### 任务状态

| 状态 | 含义 |
|---|---|
| `created` | 已创建，还未正式派发 |
| `assigned` | 已派发给目标 agent |
| `in_progress` | worker 已 claim 并开始处理 |
| `reviewing` | reviewer 正在评审 |
| `accepted` | hub 接受该结果 |
| `blocked` | worker 被阻塞，需要输入或环境变化 |
| `stalled` | 父级目标因子任务阻塞而停滞 |
| `closed` | 任务已归档 |

## 编排者模式

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

## 角色文件

`agents/` 目录提供开箱即用的角色定义：`_protocol.md`（公共通信约定）、`hub.md`（编排者）、`reviewer.md`、`implementer.md`。启动时绑定角色：

```bash
claude --append-system-prompt "$(cat agents/hub.md)"        # 主会话兼编排者
agy -i '读取 agents/reviewer.md 并按该角色工作'               # agy 当 reviewer
codex "读 agents/implementer.md，按该角色工作"                # codex 当 implementer
```

`CLAUDE.md` / `AGENTS.md` / `GEMINI.md` 是薄入口，只含项目事实和角色指引。

新增角色：复制 `agents/_template.md` 为 `agents/<角色名>.md`，替换占位符即可——模板里带命名规则和职责拆分的判断标准。

## 附录：工具返回值格式

所有工具返回 JSON（作为 text content block，同时尽量填充 structuredContent）。

`register_agent` / `list_agents` 中的 agent 对象：

```json
{
  "name": "claude-code",
  "description": "Claude Code session in ~/developer/orbit",
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

`check_inbox` 只租约领取，不直接标记已读。处理完成后调用 `ack_message`：

```json
{ "acked": true, "message_id": 5 }
```

调用 `ack_message` 时必须传回该消息的 `lease_token`。如果消费者崩溃或忘记 ack，消息会在 `lease_expires_at` 后重新变为可领取，`delivery_count` 会递增，并生成新的 `lease_token`。

`get_thread` 返回消息对象数组（比 inbox 消息多 `read_at` / `leased_until` / `lease_owner` 字段），按 id 升序、从线程根消息开始。
