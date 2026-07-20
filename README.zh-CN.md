# Orbit

**简体中文** | [English](./README.md)

> 本地 agent 工作流 Runtime。工作流是一张静态图；一次运行靠往事件日志里追加事件推进。Action 节点由启动前注册的 Handler 执行，路由、Join 和持久化 HumanTask 等待由确定性 Controller 推进。中途重启也能接着跑——状态在日志里，不在进程里。

```
工作流定义 ──▶ 计划 ──▶ Job ──▶ Handler
                    └──▶ HumanTask Controller
                        │
   事件日志（SQLite）◀───┘──▶ 读模型 ──▶ /api/v1、/mcp、/ui
```

- **持久化是结构性的。** 命令幂等，Job 带租约；Handler 在调用中途丢租约时报「结果未知」，而不是被悄悄重试出第二次副作用。
- **能做什么由服务端说了算。** 客户端可执行的每个动作都以 `allowed_commands[]` 给出，带目标和 expected version。客户端不自己拼 mutation URL。
- **没注册过的东西跑不了。** Handler 注册表在第一个 worker 启动前就 seal，计划绑定的是它编译时的那个 manifest fingerprint。
- **一个进程。** `orbit serve` 就是 Runtime + API + UI + worker + timer。状态存在 `~/.orbit/projects/` 下的 SQLite。

## 适用范围

orbit 运行的是**静态**工作流图：发布的工作流编译成一份计划，运行期间形状不变。
静态 `human` 节点会创建持久化审批任务，并在获授权提交后恢复图路由。
动态规划——foreach 组、subflow、能改写自身图的 agentic region——在领域层和
service 层已实现，但已发布工作流仍无法触达：组合根已经监管 Planner dispatcher，
但 DSL 和 Kernel 尚不会创建 Planner attempt 或结构节点。详见
[docs/migration/unwired-capabilities.md](docs/migration/unwired-capabilities.md)。

## 目录

- [安装](#安装)
- [快速开始](#快速开始)
- [CLI](#cli)
- [HTTP API](#http-api)
- [MCP](#mcp)
- [开发类工具](#开发类工具)
- [从旧引擎升级](#从旧引擎升级)

## 安装

需要 Python ≥ 3.10 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv tool install git+https://github.com/TNJ2026/orbit.git
uv tool update-shell            # 首次确保 ~/.local/bin 在 PATH 上
```

本地检出：

```bash
git clone https://github.com/TNJ2026/orbit.git
uv tool install --editable ./orbit
# 或直接跑：cd orbit && uv run orbit serve
```

## 快速开始

```bash
cd <你的项目>
orbit workflow publish my-workflow.yaml --catalog catalog.json --expected-version 0
orbit serve
```

打开 `http://127.0.0.1:8848/ui`。控制台显示所有运行、每个运行在等什么、它的计划，以及一个收件箱汇总所有等人处理的事项。中英双语，初始语言跟浏览器。

也可以只用终端：

```bash
orbit run start my-workflow --input '{"value": 1}'
orbit run inspect run:abc123          # 状态、待办责任、最近错误
```

`orbit serve` 只绑回环地址，把键盘前的人当作操作者。非回环来的请求拿不到任何身份——所以把端口暴露出去得到的是 401，而不是一个敞开的 Runtime。

## CLI

```bash
orbit --version
orbit <command> --help
```

### `orbit serve`

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 绑定地址 |
| `--port` | `8848` | 端口 |
| `--db` | 按项目 | 数据库路径，默认 `~/.orbit/projects/<project>/runtime.db` |
| `--artifact-root` | 数据库旁 | 本地内容寻址 Artifact 目录，默认位于所选数据库旁的 `artifacts/` |
| `--runner-concurrency` | `5` | 进程内 worker 的并行度 |
| `--no-agent-discovery` | 关 | 启动时不探测已安装的 agent CLI |
| `--dev-tools` | 关 | 注册可信的 git / verify 工具（见下） |
| `--acknowledge-discard-legacy-data` | 关 | 一次性的 cutover 确认（见下） |

Artifact 存储默认启用。其 `staging/` 与 `blobs/` 目录必须可写且位于同一
文件系统。备份时应同时备份 Runtime 数据库与该目录：元数据位于 SQLite，
不可变内容位于 Artifact 根目录。

### `orbit run`

```bash
orbit run start <workflow_id> [--input JSON] [--goal TEXT] [--workflow-version N]
                              [--idempotency-key KEY] [--json]
orbit run inspect <run_id> [--json]
```

`run start` 走的是和 HTTP API 同一套用例层，所以终端起的运行和 UI 起的运行产生完全相同的事件。带 `--idempotency-key` 才能让整条命令重跑保持幂等；不带的话每次调用都是一个新运行。

### `orbit workflow`

```bash
orbit workflow validate <file> --catalog <catalog.json> [--json]
orbit workflow compile  <file> --catalog <catalog.json> [--output PATH]
orbit workflow publish  <file> --catalog <catalog.json> --expected-version N [--db PATH]
```

`--expected-version` 是 CAS：版本过期就发布失败，而不是覆盖掉别人的。

### `orbit db check`

审计事件、投影、回执和快照的完整性。默认只读；`--drop-invalid-snapshots` 只删损坏的快照缓存，事件和投影永远不动。数据库不健康时返回非零。

## HTTP API

全部在 `/api/v1` 下。读走 cursor 分页并带 schema 版本；写必须带 `idempotency-key` 头和 body 里的 `expected_version`。

| 路由 | 用途 |
| --- | --- |
| `GET /api/v1/runs` | 运行列表，最新在前，`?active=true` 只看进行中 |
| `GET /api/v1/runs/{id}` | 概要：状态、工作流、预算 |
| `GET /api/v1/runs/{id}/responsibilities` | 运行在等什么，以及你能下哪些命令 |
| `GET /api/v1/runs/{id}/timeline` | 事件日志 |
| `GET /api/v1/runs/{id}/errors` | 失败投影（不是过滤后的时间线） |
| `GET /api/v1/runs/{id}/data` | 内联值和已提交 Artifact 元数据 |
| `GET /api/v1/runs/{id}/data/{data_id}/lineage` | Run 范围内的 Value 或 Artifact 血缘 |
| `GET /api/v1/runs/{id}/plan` | 计划定义：节点、Handler、边。不含运行状态 |
| `GET /api/v1/runs/{id}/plan/overlay` | 每个节点的运行状态，标注所属计划版本 |
| `GET /api/v1/runs/{id}/plan/diff` | 两个计划版本之间的差异 |
| `GET /api/v1/inbox` | 跨所有运行、等人处理的事项 |
| `GET /api/v1/recovery` | Runtime 认为卡住的东西 |
| `GET /api/v1/handler-catalog` | 已装 Handler 和发现到的 agent CLI |
| `GET /api/v1/workflows` | 已发布工作流及获授权的 `start_run` 命令 |
| `POST /api/v1/runs` | 开始一个运行 |
| `POST /api/v1/runs/{id}/cancel` | 在已知版本上取消 |
| `POST /api/v1/runs/{id}/budget` | 追加预算 |
| `POST /api/v1/human-tasks/{id}/claim` \| `/submit` | 认领 / 决定人工任务 |
| `POST /api/v1/recovery/apply` | 执行恢复动作 |

定义和叠加是刻意分开的：把两者揉成一个「带状态的节点」，就再也分不清「重新规划过」和「只是重试了一次」，还会诱使客户端把上一版的状态画到这一版的图上。

`/health/live` 和 `/health/ready` 在 `/api/v1` 之外。就绪检查逐项报告数据库、migration、已 seal 的注册表，以及每个后台循环。

## MCP

`POST /mcp` 走 JSON-RPC 2.0，工具有 `list_runs`、`inspect_run`、`start_run`、`cancel_run`，身份与授权和 HTTP API 完全一致。工具发现是开放的；每次工具调用都要有 scope。

```toml
# .codex/config.toml
[mcp_servers.orbit]
url = "http://127.0.0.1:8848/mcp"
```

## 开发类工具

`orbit serve --dev-tools` 注册四个可信工具——`git.status`、`git.diff`、`git.integrate`、`verify`——它们在 Runtime 按 workspace ref 分配的 git worktree 里运行。

工作流只能**按名字选**工具并传有界参数，不能提供程序、参数、路径或 shell 字符串：每个 adapter 的 argv 是写死的，验证跑的是组合根注册的**具名 profile**。整条路径上没有 shell，子进程的环境是显式构造的，不是继承来的。

能力策略在注册表 seal 之前生效，所以部署没授权的工具是**不存在**，而不是事后被拒。`git.integrate` 是唯一写操作，因此也是唯一一个丢租约后报「结果未知」并升级给人、而不是重试的工具。

## 从旧引擎升级

早期的 orbit 是另一个系统：一个带 `messages.db` 的任务队列，有 `orbit start / up / init / config / runner`，还有未版本化的 `/api/tasks`。这些全部已删除。

如果项目里还留着迁移前的数据库，`orbit serve` 会拒绝启动并以退出码 3 结束。那些文件的内容——包括 cutover 之前写入的 Runtime 数据——是被放弃，不是被迁移。orbit 不会打开、复制或删除它们，那是你的决定。要继续：

```bash
orbit serve --acknowledge-discard-legacy-data
```

这会写一个 `0600` 的标记文件，只记录你确认了哪些路径和确认时间，别的什么都不记。这里刻意没有导入路径：一个「半支持」的导入，正是双份事实来源回来的方式。

## 开发

```bash
.venv/bin/python -m unittest discover -s tests
node --test tests/ui/client_modules.test.mjs   # 客户端模块，装了 node 才跑
```

浏览器套件需要额外依赖，没装就跳过：

```bash
uv pip install -e '.[dev]'
python -m playwright install chromium
```
