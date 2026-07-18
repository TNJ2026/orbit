# orbit

本地 agent 工作流 Runtime：工作流是一张静态图，运行时按事件溯源推进；每个节点交给一个注册过的 Handler 执行。Python + Starlette + uvicorn。

- 启动：`uv run orbit serve`（Runtime + API + UI + worker + timer 在一个进程；UI 在 127.0.0.1:8848/ui）
- 测试：`.venv/bin/python -m unittest discover -s tests`
- 详细约定见 [CLAUDE.md](./CLAUDE.md)。

## 给 agent 的接口

Runtime 对 agent 暴露两个面，都走同一套身份与授权：

- **HTTP** `/api/v1` — 读走 cursor 分页，写必须带 `idempotency-key` 头和 `expected_version`。
- **MCP** `/mcp` — JSON-RPC 2.0，工具：`list_runs`、`inspect_run`、`start_run`、`cancel_run`。

命令一律从服务端返回的 `allowed_commands[]` 里取，不要自己拼 URL：服务端是「谁能做什么」的唯一权威。
