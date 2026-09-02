# Orbit

**简体中文** | [English](./README.md)

Orbit 是面向 Agent App 的本地持久化 LangGraph 工作流 Runtime。Runtime、API、Web UI、
持久化定时器、工作流编写和 MCP 接入都运行在同一个进程中。项目数据保存在
`~/.orbit/projects/`。

## 在 Codex 中安装

从对应的 GitHub Release 下载 `orbit-marketplace-<版本>.zip`，然后执行：

```bash
unzip orbit-marketplace-<版本>.zip
codex plugin marketplace add ./orbit-marketplace
codex plugin add orbit@orbit-local
```

也可以先添加解压后的 Marketplace 目录，再从 Codex 插件界面安装。

1. 打开 Codex App 的 **Plugins**。
2. 在 **Orbit Local** 中找到 **Orbit**，点击 **Install**。
3. 新建一个 Codex 任务，让新安装的 Skill 和 MCP 工具生效。
4. 打开需要运行工作流的目标项目。
5. 告诉 Codex：`打开 Orbit`。

Orbit 必须获得明确的项目目录。如果聊天没有项目 workspace，启动会停止并提示用户
打开或选择项目；不会再把进程碰巧所在的目录当作项目。

Orbit 会为当前项目启动 Runtime，在 Codex 对话旁打开原生工作流面板，并自动监听
工作流编写请求。完整浏览器 UI 仍可通过 Runtime 地址访问。当前任务运行期间，
Agent 选择器会出现 `codex`，并默认
选中它。

多 Workspace 时，Agent CLI 的安装发现和 Workflow 源码模板可全局共用；
Handler 注册/授权、已发布 Workflow 及原始执行统计仍按 Workspace 隔离。
Hub 可聚合在线 Runtime 的 Agent 统计，把模板实例化到目标 Workspace 时会
重新编译并校验，不直接共用可执行版本。

监听状态属于当前 Codex 任务。任务结束后 `codex` 会离线，但 Runtime
可以继续运行。新建任务并再次说“打开 Orbit”即可重新连接。

## 安装 CLI

需要 Python 3.10 或更高版本，以及 [uv](https://docs.astral.sh/uv/)。

```bash
uv tool install git+https://github.com/TNJ2026/orbit.git
uv tool update-shell
```

从源码运行：

```bash
git clone https://github.com/TNJ2026/orbit.git
cd orbit
uv sync --extra dev
uv run orbit serve
```

UI 地址为 `http://127.0.0.1:8848/ui`。

只有一个 UI、一个目录、一个已发布 Workflow 库。工作流点名哪些 Agent，就在这些
Agent 存在的地方用它们。

已发布的工作流钉住的是编译时那个精确的 Handler 构建，而 Agent 的构建就是它的 CLI
版本——所以在别的机器上写的工作流、或者 CLI 升级过之后，它点名的东西可能不在这里。
无处可去的步骤会被送到一个在的 Agent 上，且尽可能少动：优先送到同一个 Agent 已安装
的构建，实在不行才送到本 Runtime 正在对话的那个 Agent。**点名的 Agent 在，就绝不
移动**——所以刻意用两个 Agent 的工作流仍然用两个。

## 在 Codex 中使用

### 打开并连接

在新的 Codex 任务中输入：

```text
打开 Orbit
```

Codex 会启动或复用当前项目的 Runtime、打开 UI，并调用
`wait_authoring_request(client="codex-app")`。当前任务保持活跃时，监听超时后会
自动续听。

### 运行目标

1. 打开 **目标** 页面。
2. 选择一个已发布的 Workflow，或者描述一个、让 Agent 写出来。
3. 输入目标并启动。
4. 在工作台查看每一步进度，或在 **历史** 中检查已完成的运行。

也可以通过 MCP 使用 `list_runs`、`inspect_run`、`start_run` 和
`cancel_run`。客户端必须遵循 Runtime 返回的 `allowed_commands[]`，不要自行拼接
写操作 URL。

### 停止 Orbit

点击刷新按钮右侧的 **停止 Orbit** 并确认。该操作会结束当前项目的 Runtime、
Worker、定时器、MCP 端点和事件连接。

## 接入其他 Agent App

任何支持 MCP 的 Agent App 都可以通过 Orbit 的 stdio Proxy 连接。根据目标 App
的 MCP 配置格式调整以下示例：

```json
{
  "mcpServers": {
    "orbit": {
      "command": "bash",
      "args": ["/absolute/path/to/orbit/scripts/start-mcp-proxy.sh"],
      "env": {
        "ORBIT_AGENT_APP_WORKSPACE": "/absolute/path/to/project"
      }
    }
  }
}
```

App 必须保持下面的调用处于等待状态，才会被 Orbit 识别为当前在线 Agent：

```text
wait_authoring_request(client="claude-desktop", timeout_seconds=300)
```

Orbit 随后显示 `app:claude-desktop`。仅连接 MCP 不会注册在线 App——是这个等待调用
让它可被寻址。被请求撰写 Workflow 的 App 用 `submit_authoring_response` 提交 DSL，
并通过 `get_authoring_job` 处理编译反馈。

Runtime 事件可通过 `wait_app_event`、`list_app_events` 和 `ack_app_event` 处理。
`event_type` 为 `langgraph_run.<status>`（run 状态变化）或
`langgraph_node.<outcome>`（单次 Handler 尝试，另带 `node_id` 和 `attempt_id`）。
节点事件只来自带尝试日志的 Handler —— 即那些执行本身是不可重复的外部效果的 —— 所以
重放的 superstep 不会产生事件。
事件只是提示，执行操作前应重新读取对应 Run。

run 在启动它的那个请求里执行。`POST /api/v1/langgraph-runs` 带 `"wait": false`
时，run 一创建就返回，执行放到后台 —— UI 用的就是这条，页面才能看着自己启动的目标。
两种方式下"这个 run 能不能存在"都已经判定完毕；等待换来的只是知道它**怎么结束的**。

每个 actor 同时只跑一个目标：当已有 `running`、`waiting` 或
`interrupted` 的 run 时，再启动会以 `active_goal_exists` 拒绝，并在拒绝里带上占用
该槽位的 run，客户端可以直接跳过去。取消或结束即释放。

run 默认永久保留。`/api/v1/ops/status` 报告引擎占用的容量；
`create_app(run_retention_days=N)` 会忘掉结束超过 N 天的 run —— 按**整个 run**
删除，因为缺了控制台或 checkpoint 的 run 会错误地描述自己。等待人工输入的 run、
以及 Handler 以 `unknown` 结束的 run，永远不会被忘掉。

Handler 进程打印的内容通过
`GET /api/v1/langgraph-runs/{run_id}/output?after=<chunk_id>` 读取，需要 sensitive
scope。它是控制台而非日志：按尝试和流分别限量、写在所有事务之外，且永远不参与重放。

## CLI 快速参考

```bash
orbit serve
orbit --version
orbit mcp
orbit run list
orbit run inspect <run_id>
orbit workflow validate <file> --catalog <catalog.json>
orbit workflow publish <file> --catalog <catalog.json> --expected-version <n>
```

`orbit serve` 默认只绑定 `127.0.0.1`。Runtime 状态和 Artifact 按项目隔离；已发布
工作流定义保存在本地共享的 Orbit 工作流库中。

## 开发

```bash
uv sync --extra dev
.venv/bin/python -m unittest discover -s tests
node --test tests/ui/client_modules.test.mjs
```

构建 Python 包：

```bash
uv build
python scripts/build-marketplace-release.py \
  --version 0.4.0 \
  --output dist/orbit-marketplace-0.4.0.zip
```

推送 `v0.4.0` 这样的标签后，Release workflow 会检查标签与
`src/orbit/__init__.py` 的版本是否一致、运行测试，并把 wheel、源码包和本地
Marketplace ZIP 一起上传到 GitHub Release。
