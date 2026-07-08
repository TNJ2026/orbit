# orbit

本地 MCP 信箱 server：多个 LLM CLI/Agent 通过它互传提示词。Python + FastMCP，唯一依赖 `mcp`。

- 启动：`uv run orbit serve`（127.0.0.1:8848/mcp，db 默认按当前项目目录分开存储）
- 代码：`src/orbit/`（`store.py` SQLite 层，`server.py` MCP 工具层）
- 测试：`.venv/bin/python -m unittest discover -s tests -v`

## 多 agent 角色

本仓库用 orbit 做多 agent 协作。如果启动时被指定了角色（如「按 agents/hub.md 工作」），读取 `agents/<role>.md` 并遵循；通信协议见 `agents/_protocol.md`。未指定角色时忽略本节。
