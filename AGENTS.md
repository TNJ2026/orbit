# orbit

本地 Agent 工作流 Runtime：Agent 生成静态 Workflow DSL，由可信编译器编译成 LangGraph；每个执行节点交给已注册的 Handler。Python + Starlette + uvicorn。

- 启动：`uv run orbit serve`（默认单 Agent UI；多 Agent UI 使用 `--ui-mode multi-agent`；UI 在 127.0.0.1:8848/ui）
- 测试：`.venv/bin/python -m unittest discover -s tests`
- 详细约定见 [CLAUDE.md](./CLAUDE.md)。

## 给 agent 的接口

Runtime 对 agent 暴露两个面，都走同一套身份与授权：

- **HTTP** `/api/v1` — 读走 cursor 分页，写必须带 `idempotency-key` 头和 `expected_version`。
- **MCP** `/mcp` — JSON-RPC 2.0，运行工具：`list_runs`、`inspect_run`、`start_run`、`resume_run`、`cancel_run`。

命令一律从服务端返回的 `allowed_commands[]` 里取，不要自己拼 URL：服务端是「谁能做什么」的唯一权威。
