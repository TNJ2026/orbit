# orbit

本地 agent 工作流 Runtime：工作流是一张静态图，运行时按事件溯源推进；每个节点交给一个注册过的 Handler 执行。Python + Starlette + uvicorn（依赖 `starlette`、`uvicorn`、`jsonschema`、`PyYAML`）。

- 启动：`uv run orbit serve`（Runtime + API + UI + worker + timer 在一个进程；UI 在 127.0.0.1:8848/ui，db 默认按项目目录分开存到 `~/.orbit/projects/<project>/runtime.db`）
- 测试：`.venv/bin/python -m unittest discover -s tests`

## 代码结构

- `src/orbit/workflow/` — Runtime 本体：`domain/`（不可变类型与命令/事件信封）、`runtime/`（确定性 kernel + reducer）、`persistence/`（SQLite、migration、快照）、`handlers/`（Handler SDK 与内置 Handler）、`api/`（read model 与 DTO）、`application/`（用例层）
- `src/orbit/web/` — 唯一的组合根 `app.py`，以及 `api_v1.py`、`mcp.py`、`builtin_handlers.py`、`local_identity.py`
- `src/orbit/platform/` — 项目发现、进程管理、cutover 门禁
- `src/orbit/workspace/` — git worktree provider
- `src/orbit/static/workflow-ui/` — 模块化 UI（index.html + assets/，无构建步骤）

## 几条不能破的约定

- **命令只从 `allowed_commands[]` 来。** 服务端在 read model 里给出 method/href/expected_version，UI 不自己拼 mutation URL，也不维护状态机。
- **写操作必须带 idempotency key 和 expected_version。** 缺任一个直接拒绝。
- **Handler 注册表在任何 worker 启动前 seal。** 计划里绑定的 manifest fingerprint 是运行时保证，不是约定。
- **DSL / UI / Planner 不能提供命令。** 开发类工具（git、verify）只能按名字选择，argv 写死在 `handlers/dev_tools.py`，`orbit serve --dev-tools` 才注册。
- **旧引擎已在 M6 删除。** `server.py` / `store.py` / 旧 UI / `orbit start|up|init|config|runner` 都不存在了；`tests/test_migration_guard.py` 会挡住它们回流。
