# orbit

本地多 agent 工作流编排器：任务在一张工作流图上流转，runner 把每个步骤交给对应的 agent CLI 执行。Python + FastMCP，唯一依赖 `mcp`。

- 启动：`uv run orbit serve`（UI + 调度 + 内嵌 runner；Web UI 在 127.0.0.1:8848/ui，db 默认按当前项目目录分开存储）
- 代码：`src/orbit/`（`store.py` SQLite 层，`server.py` 工作流引擎 + Web UI/HTTP API + 工作流 MCP 工具）
- 测试：`.venv/bin/python -m unittest discover -s tests -v`

## 多 agent 角色

本仓库用 orbit 做多 agent 协作。如果启动时被指定了角色（如「按 agents/hub.md 工作」），读取 `agents/<role>.md` 并遵循；执行约定见 `agents/_protocol.md`。未指定角色时忽略本节。
