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

Orbit 会为当前项目启动 Runtime，打开 `http://127.0.0.1:8848/ui`，并自动监听
工作流编写请求。当前任务运行期间，Agent 选择器会出现 `app:chatgpt`，并默认
选中它。

监听状态属于当前 Codex 任务。任务结束后 `app:chatgpt` 会离线，但 Runtime
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

默认启动单 Agent 编排 UI。如需使用保持原有能力的多 Agent 高级 UI：

```bash
uv run orbit serve --ui-mode multi-agent
```

## 在 Codex 中使用

### 打开并连接

在新的 Codex 任务中输入：

```text
打开 Orbit
```

Codex 会启动或复用当前项目的 Runtime、打开 UI，并调用
`wait_authoring_request(client="chatgpt")`。当前任务保持活跃时，监听超时后会
自动续听。

### 使用流程模板

1. 在 Orbit 首页确认“当前连接的 Agent”显示为 `app:chatgpt`。
2. 选择“直接执行”“规划后执行”或“执行后人工审核”。
3. 在只读流程图中确认步骤，输入目标并点击 **开始执行**；也可以输入名称并点击
   **发布 Workflow**，保存为可重复使用的流程。
4. 已发布 Workflow 会出现在模板选择器顶部，并继续显示完整流程图。
5. Orbit 在每次运行前将图中的 Agent 节点绑定到当前唯一 Agent Handler，保存本次
   Run 的解析后图快照并交给 LangGraph。

单 Agent 模式不让用户接触 DSL、草稿或版本号。发布操作在内部复用现有 Workflow
版本库，以获得稳定存储和流程图读取能力；UI 始终只展示当前定义。模板或已发布
Workflow 的变化只影响新 Run，已经启动的 Run 始终使用自己的图快照恢复。多 Agent
UI 继续保留完整工作流建模能力。

### 运行目标

1. 打开 **目标** 页面。
2. 选择流程模板。
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

Orbit 随后显示 `app:claude-desktop`。仅连接 MCP 不会注册在线 App。默认单 Agent
模式只利用该等待调用判断在线状态，并直接启动模板；只有使用
`--ui-mode multi-agent` 时，App 才需要用 `submit_authoring_response` 提交 Workflow
DSL，并通过 `get_authoring_job` 处理编译反馈。

Runtime 事件可通过 `wait_app_event`、`list_app_events` 和 `ack_app_event` 处理。
`event_type` 为 `langgraph_run.<status>`（run 状态变化）或
`langgraph_node.<outcome>`（单次 Handler 尝试，另带 `node_id` 和 `attempt_id`）。
节点事件只来自带尝试日志的 Handler —— 即那些执行本身是不可重复的外部效果的 —— 所以
重放的 superstep 不会产生事件。
事件只是提示，执行操作前应重新读取对应 Run。

run 在启动它的那个请求里执行。`POST /api/v1/langgraph-runs` 带 `"wait": false`
时，run 一创建就返回，执行放到后台 —— UI 用的就是这条，页面才能看着自己启动的目标。
两种方式下"这个 run 能不能存在"都已经判定完毕；等待换来的只是知道它**怎么结束的**。

单 Agent 模式下每个 actor 同时只跑一个目标：当已有 `running`、`waiting` 或
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
