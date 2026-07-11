# orbit

本地多 agent 工作流编排器：任务在一张工作流图上流转，runner 把每个步骤交给它指定的 agent CLI 执行。每个步骤自带 `agent`（谁执行）和 `command`（跑什么 CLI），没有角色/团队中间层。Python + Starlette + uvicorn（依赖 `starlette` + `uvicorn`）。

- 启动：`uv run orbit serve`（UI + 调度 + 内嵌 runner；Web UI 在 127.0.0.1:8848/ui，db 默认按当前项目目录分开存储）
- 代码：`src/orbit/`（`store.py` SQLite 层，`server.py` 工作流引擎 + Web UI/HTTP API，Starlette + uvicorn 托管）
- 测试：`.venv/bin/python -m unittest discover -s tests -v`
- 步骤命令：每个 step 给它的每个 Agent 配命令（`step.agent_commands`），留空则用该 Agent 的内置命令；派发时按 step 的 Agent 列表轮询（同一任务返工回到原 Agent）。卡死巡检的 hub 命令 = Decompose step 首个 Agent 的命令。
