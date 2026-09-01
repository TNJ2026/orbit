# orbit

本地 Agent 工作流 Runtime：Agent 生成静态 Workflow DSL，由可信编译器编译成 LangGraph；每个执行节点交给已注册的 Handler。Python + Starlette + uvicorn。

生产服务分为三层：固定端口 MCP Gateway、每工作区 Control Runtime、每 Runtime 一个可配置的独立 Execution Worker 进程池。Control Runtime 持有图状态、授权与 `allowed_commands[]`；Worker 只执行受信 Handler。Streamable HTTP 会话可通过 `list_workspaces`/`select_workspace` 按名称或绝对路径选择工作区。

- 启动：`scripts/start-orbit.sh [项目路径]`（Hub 在 127.0.0.1:8848，工作区 Runtime 使用动态端口）
- 测试：`.venv/bin/python -m unittest discover -s tests`
- 详细约定见 [CLAUDE.md](./CLAUDE.md)。

## 给 agent 的接口

Runtime 对 agent 暴露两个面，都走同一套身份与授权：

- **HTTP** 工作区 Runtime 的 `/api/v1` — 读走 cursor 分页，写必须带 `idempotency-key` 头和 `expected_version`。
- **MCP Gateway** Hub 的 `/mcp`（默认工作区）或 `/workspaces/<id>/mcp` — Hub 终止 JSON-RPC/MCP 协议，仅将 Agent 工具目录与调用发送到工作区 Runtime；Runtime 的动态端口不向 Agent App 暴露。

命令一律从服务端返回的 `allowed_commands[]` 里取，不要自己拼 URL：服务端是「谁能做什么」的唯一权威。
