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

### 创建工作流

1. 打开 Orbit 的 **工作流** 页面。
2. 确认“当前连接的 Agent”显示为 `app:chatgpt`。
3. 描述工作流并点击 **生成工作流**。
4. Codex 接收请求、返回 Workflow DSL，并根据编译反馈进行修正，直到草稿成功或失败。
5. 检查生成结果并发布工作流。

单 Agent 表示整张图只使用一个 `agent.*` Handler，并不限制节点数量或固定拓扑；
工作流仍可包含工具、判断、并行、受限循环、人工任务和多个终止路径。

### 运行目标

1. 打开 **目标** 页面。
2. 选择已发布的工作流。
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

App 必须保持下面的调用处于等待状态，才会出现在 Orbit 的 Agent 选择器中：

```text
wait_authoring_request(client="claude-desktop", timeout_seconds=300)
```

Orbit 随后显示 `app:claude-desktop`。仅连接 MCP 不会注册在线的工作流编写 App。
收到请求后，App 使用 `submit_authoring_response` 提交一个 Workflow DSL JSON 对象，
通过 `get_authoring_job` 检查结果；如果编译器要求修正，就再次等待并提交新版本。

Runtime 事件可通过 `wait_app_event`、`list_app_events` 和 `ack_app_event` 处理。
事件只是提示，执行操作前应重新读取对应 Run。

## CLI 快速参考

```bash
orbit serve
orbit --version
orbit run start <workflow_id> --goal "..."
orbit run inspect <run_id> --json
orbit workflow validate <file> --catalog <catalog.json>
orbit workflow publish <file> --catalog <catalog.json> --expected-version <n>
orbit db check
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
