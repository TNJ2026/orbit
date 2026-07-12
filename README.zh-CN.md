# orbit

**简体中文** | [English](./README.md)

> 本地多 agent 工作流编排器：把编码目标拆成任务，在一张可配置的工作流图上流转，runner 无头调用各 agent CLI（Claude Code、Codex CLI、Gemini CLI、自研 agent）执行「实现 → 评审 → 测试 → 集成」，每个任务在独立 git worktree 隔离进行，失败自动返工。

```
目标/任务 ──▶ orbit（工作流引擎 + 调度）──▶ runner ──▶ agent CLI（Claude Code / Codex / Gemini …）
                    │                                       │
                    └──────── SQLite ~/.orbit/projects/<project>/ ◀── 结果回收（WORKFLOW_OUTCOME）
```

- 一条命令起：Web UI + 调度 + 内嵌 runner 全在一个进程
- 任务在可配置工作流图上流转：并行分支、汇合、返工回环、机器验证门
- 每个任务在独立 git worktree 隔离执行，`integrate` 步骤把分支合并回主干；项目还不是 git 仓库时 orbit 自动初始化（带一个基线提交），未装 git 则降级为不隔离运行
- runner 无头调用 agent CLI，可按步骤 / Agent 过滤、水平扩展
- 状态持久化到 SQLite：进程重启不丢，超时 / 卡死有兜底

## 目录

- [安装](#安装)
- [快速开始](#快速开始)
- [何时该用 orbit](#何时该用-orbit)
- [高级用法](#高级用法)
- [命令行参考](#命令行参考)
- [工作流引擎](#工作流引擎)
- [本地 Web UI](#本地-web-ui)
- [任务协作模型](#任务协作模型)

## 安装

需要 Python ≥ 3.10 和 [uv](https://docs.astral.sh/uv/)。`git` 用于每任务
worktree 隔离（项目还不是 git 仓库时 orbit 会自动创建）；runner 调用各 agent
CLI（Claude Code、Codex 等），按需自行安装。原生支持 macOS、Linux、Windows
（进程控制按系统区分 —— POSIX 进程组、Windows `taskkill`）。

**全局命令（推荐）** —— 装一次，任何电脑、任何项目里直接 `orbit`：

```bash
uv tool install git+https://github.com/TNJ2026/orbit.git
uv tool update-shell            # 确保 ~/.local/bin 在 PATH（仅首次）
# 之后更新
uv tool upgrade orbit
```

**从本地 checkout**（开发 orbit 本身时）：

```bash
git clone https://github.com/TNJ2026/orbit.git
uv tool install --editable ./orbit   # 全局 `orbit`，改代码即时生效
# 或不安装、就地跑
cd orbit && uv run orbit serve
```

`uv run orbit …` 和 `uv tool` 首次使用时会自动建环境，**无需单独 `uv sync`**。
不用 uv 的话：`pip install -e .`。

## 快速开始

任何仓库，零配置：

```bash
cd <你的项目>          # orbit 编排当前目录所在的项目
orbit start            # gitignore .orbit/，再用包内默认值直接 serve
```

然后打开 `http://127.0.0.1:8848/ui`：

1. 在 **Workflow** 页双击每个必选步骤，选择已安装的 Agent CLI。系统自动使用它的内置命令；步骤 Command 仍可显式覆盖。
2. 启动 Goal；引擎执行一次设计、按配置拆解，再驱动各工作项走完后续步骤。

`start` 不把配置写进仓库，只往 `.gitignore` 补 `.orbit/`，并支持全部 `serve` 参数。`orbit up` 是兼容别名；从本地 checkout 运行时在命令前加 `uv run`。

## 何时该用 orbit

orbit 是工作流编排器，不是快速改代码的工具。值得结构化的活它才划算，不值得的就是纯开销。两个问题决定：**装了几个 Agent CLI**，**任务多大 / 能不能拆**。

**按已装 Agent CLI：**

- **1 个 agent**——为的是*结构*而非模型多样性：设计 → 拆解 → 每任务 git worktree 隔离 → review/test 门禁 → 合并，带并行子任务、自动返工循环、token 预算、恢复。一个 CLI 跑所有步骤（自审、无轮询）。适合多部分构建；改一行就别用。
- **2 个 agent**——甜点区。给 `review` 配和 `implement` **不同**的模型，让第二双眼审查（比自审抓得多得多），并让 `implement`/`review`/`test` 在两者间轮询分担负载、扛过某个 CLI 的限流/会话上限。
- **3+ 个 agent**——每步最多 3 个（仅 `implement`/`review`/`test`）：review 模型更多样、并行吞吐更高。够覆盖你的限流后收益递减。

**按任务大小 / 形态：**

- **快速改动 / 一次性**（重命名、小 bug、单文件）——orbit 是杀鸡用牛刀。直接自己开 Agent CLI，或起一个**不拆解的 goal**（不带 `decompose` 标志），让它走一遍流程不分裂。
- **小功能**（几处相关改动）——拆成 2–4 个子任务的 goal；review/test 门禁和 worktree 隔离能抓到快路径漏掉的回归，开销也不大。
- **大功能 / 多模块构建**——orbit 的主场。设计优先只跑**一次**，Decompose 按架构模块拆成多个隔离子任务，**并行** implement/review/test/integrate；`depends_on` 只串行真正必须等的部分。
- **单一主题流程**（调研、审批、发布/报告步骤）——不拆解的 goal 自己走完整个工作流，不产工作项。

**何时*别*用：**自己在一个 CLI 里敲更快的活——当设计/拆解/审查这套脚手架比它包裹的活还贵时。

## 高级用法

### 定制并提交配置 —— `orbit config`

`orbit start` / `orbit serve` 无需生成配置。只有需要落盘并提交自定义工作流时才运行 `orbit config`（`orbit init` 仍是别名），它会生成 `.orbit/workflow.json`。

### 多进程：解耦 serve + 独立 runner

`serve` 默认内嵌一个 runner,所以 **serve 重启会中断在途 step**(租约到期后自动重跑)。要重启安全 / 多机 / 水平扩展,把调度和执行拆开:

```bash
# 终端 1：只跑 UI / 调度，不内嵌 worker
orbit serve --no-runner

# 终端 2+：独立 runner（重启 serve 不影响它们）
orbit runner --name runner-local
```

此模式下 run 活在独立进程里，serve 重启不杀在途任务。runner 是无状态 worker，可按 Agent / 步骤过滤并扩展：

```bash
orbit runner --steps implement --max-concurrency 2     # 2 个并行实现 worker
orbit runner --steps review --agent antigravity        # 只跑 antigravity 的评审
orbit runner --project /path/to/repo --name box-a      # 显式指定项目
```

- `--agent NAME`（可重复）：只领分给该 agent 的 job。
- `--steps a,b`：只领这些工作流步骤 id 的 job。
- `--max-concurrency N`：并行跑 N 个 job，默认 5（各 worker 独立租约名 `<name>-0/-1/…`）。
- `--project PATH`：显式项目根，替代 cwd 解析。
- `--once`：领到一个跑完就退出（适合脚本 / CI）。

领取是 DB 层原子操作(`UPDATE ... WHERE status=... AND lease<=now`),多 runner 并存不会重复领同一个 job;某 runner 挂了,租约到期后 job 被别的 runner 重新领走。

### 数据库与运维模型

默认数据库路径形如 `~/.orbit/projects/<项目目录名>-<路径hash>/messages.db`，项目目录按最近的 `.git` / `pyproject.toml` 向上探测——从子目录启动也会解析到同一个库。需要手动共享或指定旧库时，用 `--db` 覆盖。

**一个项目 = 一个 daemon = 一个端口。** db 由 daemon 的启动目录决定。要同时跑多个项目，就为每个项目起独立 daemon 并用 `--port` 错开端口。

从旧版本升级：旧的全局库在 `~/.dev_loop/messages.db`，不再被默认加载（启动时会打印提示）。想沿用它，`orbit serve --db ~/.dev_loop/messages.db`；想迁移进某个项目，把该文件 cp 到启动提示打印的新路径。

### 访问入口

启动后访问本地 Web UI：`http://127.0.0.1:8848/ui`——观察和操作任务 / 工作流 / 队列的主入口。所有操作走 `/api/*` JSON route（仅本机可访问），也可脚本直连。

每个 daemon 启动时会把当前项目写入 `~/.orbit/projects/index.json`。任意一个项目的 `/ui` 都能从这个索引里看到其它项目 daemon：在线项目可以直接在顶部 Project 下拉框切换；离线项目只显示元数据，需要先在该项目目录启动对应 daemon。跨项目 UI 只是聚合视图，写操作仍发到被选中项目自己的 daemon。

## 命令行参考

`orbit <命令> [参数]`。在要编排的项目目录下运行——项目根和数据库从当前工作目录解析（向上寻找 `.git` / `pyproject.toml`）。本地 checkout 用 `uv run` 前缀，或不安装直接 `uvx --from git+https://github.com/TNJ2026/orbit.git`。

```bash
orbit --version          # 打印版本并退出
orbit <命令> --help      # 单命令参数帮助
```

### `orbit start`（别名 `orbit up`）

零配置启动：把 `.orbit/` 追加进 `.gitignore`，然后用打包的默认工作流启动服务（不往仓库拷任何配置）。日常这样跑。

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `--host <地址>` | `127.0.0.1` | 绑定地址。除非有意对外暴露，否则保持本机。 |
| `--port <n>` | `8848` | UI/API 端口。每个项目用不同端口（一项目 = 一 daemon = 一端口）。 |
| `--db <路径>` | 按项目 | 覆盖 SQLite 路径（默认 `~/.orbit/projects/<名>-<哈希>/messages.db`）。 |
| `--no-runner` | 关 | 不跑内嵌 worker；改为单独启动 `orbit runner`（重启安全 / 多机）。 |
| `--runner-concurrency <n>` | `5` | 内嵌 worker 并行跑多少个 job。 |

```bash
orbit start                                  # 默认 127.0.0.1:8848，内嵌 runner
orbit start --port 9000                       # 改端口
orbit start --host 0.0.0.0 --port 9000        # 绑定所有网卡（对外暴露，谨慎）
orbit start --db ~/.orbit/shared/app.db       # 指定数据库
orbit start --runner-concurrency 10           # 最多同时跑 10 个步骤
```

### `orbit serve`

与 `start` 相同，但**不动** `.gitignore`——当你的 `.orbit/` 配置已提交（用 `orbit config` 生成）后用它。参数与 `start` 完全一致（`--host` / `--port` / `--db` / `--no-runner` / `--runner-concurrency`）。

```bash
orbit serve --port 9000
orbit serve --no-runner --port 9000           # 只跑 UI/调度；runner 另起
```

### `orbit runner`

启动独立 runner 进程，认领队列里的 run job 执行。配合 `serve --no-runner`（或 `start --no-runner`），这样重启服务不会中断进行中的步骤，也便于横向扩容。

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `--db <路径>` | 按项目 | 监听的数据库——必须和服务端一致。 |
| `--name <id>` | `runner-local` | job 租约上记录的 runner 名（每个 runner 取唯一名）。 |
| `--agent <名>` | 全部 | 只认领该 agent 的 job；**可重复**（`--agent codex --agent gemini`）。 |
| `--steps <ids>` | 全部 | 只认领这些步骤 id 的 job，逗号分隔（`implement,review`）。 |
| `--project <路径>` | cwd | 服务的项目根（默认从当前目录解析）。 |
| `--max-concurrency <n>` | `5` | 最多并行跑多少个 job。 |
| `--poll-seconds <秒>` | `2.0` | 无 job 时的轮询间隔。 |
| `--once` | 关 | 最多认领一个 job，无 job 时退出（适合 cron / CI）。 |

```bash
# 终端 1 —— 只跑 UI/调度：
orbit serve --no-runner --port 9000
# 终端 2 —— 只做 implement 的 runner，按主机命名：
orbit runner --name box-a --steps implement --max-concurrency 8
# 终端 3 —— 绑定特定 agent 的 runner：
orbit runner --name box-b --agent codex --agent gemini
```

### `orbit config`（别名 `orbit init`）

在 `.orbit/` 生成可编辑、可提交的工作流配置（工作流图 + gitignore 条目），让团队共享同一套工作流。可选——`start`/`serve` 不依赖它。用 `--host`/`--port`（默认 `127.0.0.1:8848`）只是在输出里打印一条 serve 提示，不启动服务。

```bash
orbit config                     # 写 .orbit/workflow.json（已存在则保留）
orbit config --port 9000         # 同上，提示里用 9000 端口
```

## 工作流引擎

工作流引擎逻辑上是三层——**Scheduler**（决定下一步、推进）、**Runner/Worker**（执行 agent CLI）、以及它们之间的 `run_jobs` 队列。默认打包进一个进程，也可以拆开跑：

| 层 | 职责 |
|---|---|
| **Scheduler**（serve 内线程） | 把"要执行某 step"写入 `run_jobs` 队列；单点消费执行完的 job 并推进工作流（dispatch / rework / accept）；跑 timeout / health 兜底 |
| **Runner / Worker** | 从 `run_jobs` 领取任务（带租约 + 心跳）、执行各 agent 的 CLI、流式记录 stdout/stderr、解析 outcome，把结果写回 job |

默认工作流（设计优先）：`intake → product_design → ui_design → architecture → decompose → implement → review → test → integrate`。设计步骤由 goal 串行执行一次，再由 Decompose 拆成从 `implement` 开始的实现任务。Review、Test、Integrate 可在不达标时退回 Implement。每个步骤直接选择 Agent；未为某 Agent 填写 Command 时使用其内置 CLI 命令。

### 默认：一体进程

```bash
orbit serve        # UI + 调度 + 内嵌 Runner，全在一个进程
```

`serve` 默认**内嵌一个 in-process runner**（名字 `serve-embedded`，并发 5），所以启动一个 goal 后不需要再手动起 runner——建 job → 内嵌 runner 执行 → scheduler 推进，全自动。UI 的 **Jobs** 标签页能看到队列状态(pending / running / finished / done)。

> ⚠️ 内嵌 runner 与 serve 同生命周期：**serve 重启会中断在途 step**（租约到期后该 step 自动重跑）。要重启安全 / 多机 / 水平扩展，把它们拆开 —— 见[高级用法 → 多进程](#多进程解耦-serve--独立-runner)。

**job 生命周期：** `pending → running`（runner 领取）`→ finished`（runner 执行完、报告 outcome）`→ done`（scheduler 推进下一步）。

每个步骤实例都会持久化结构化执行详情。Orbit 把任务、步骤元数据和上游结果记录为
`step_inputs`，并要求 runner 在输出末尾给出 `RESULT_SUMMARY: ...` 与
`ARTIFACTS: ["路径或 URI", ...]`，分别写入步骤卡的 `result_summary` 和
`artifacts` API 字段。旧 runner 未输出该协议时，系统会把清理后的原输出作为结果摘要。

### 卡住的工作流如何恢复

在任务的 **Goal Execution** 页面可用以下操作恢复阻塞步骤，无需修改工作流图：

- **Re-run（重新运行）**：把阻塞/失败的步骤重新派发给你选的 Agent —— 比如它的 CLI
  撞了限流/会话上限 —— 无需改工作流。步骤阻塞或上次运行失败时，与 Re-implement 一同可用。
- **Re-implement（重新实现）**：任务因达到返工上限而阻塞时使用。选择一个
  implementer 后，Orbit 会把最新 review 反馈交给它，并重新派发 `implement`；不会
  提高或重置项目的返工上限。
- **Skip step（跳过步骤）**：接受当前产出并沿正常的前向边进入下一步骤。适合无法继续
  的非结构性 review/test 门禁。运行中的步骤不能跳过；`integrate`、`decompose` 和
  没有后继的终止步骤也不能跳过。
- **Check & recover（检查并恢复）**：立即运行 watchdog，重新派发因 runner 死亡或
  推进中断而遗留的工作；对于超过超时阈值、需要人工处理的步骤会通知 hub。watchdog
  本身也会后台定期运行；该操作适合刚重启服务后或排查卡住任务时使用。

脚本可调用对应的本机 JSON API：

```text
POST /api/tasks/{task_id}/rerun         {"agent": "codex", "step": "review"}  # step 可省略
POST /api/tasks/{task_id}/reimplement   {"agent": "codex"}
POST /api/tasks/{task_id}/skip          {"step": "review"}  # step 可省略
POST /api/health-check                  {}
```

这些 API 只接受本机请求。后台循环失败会记录到服务日志，`GET /api/status` 的
`background_errors` 会给出失败次数、最近时间和错误摘要。

### 设计优先与 `decompose` 步

goal 在哪一步拆成子任务，由打了 `decompose: true` 的步骤决定。默认由 `decompose` 结合上游设计产出生成子任务 JSON；每个子任务从其后继步骤（`implement` 起）开始并继承该产出，因此设计步骤在 goal 层只执行一次。

想换拆解点，就在 `.orbit/workflow.json` 里给别的步打标记。**不打** `decompose` 标记时，goal 不创建工作项，而是自己走完整条工作流；这适合学术研究、审批、出版等单主体流程。该标记只能改 JSON 配置（同 `isolate`/`integrate`）；decompose 步自动 required、从不隔离、且必须有后继步供工作项起跑。

### 目标收敛验证（goal_verify）最佳实践

当一个目标（Goal）的工作项全部关闭，或无拆解的 goal 走到终点后，orbit 会在主分支执行 `goal_verify` 命令，对整体成果进行客观验收。以下指南帮助正确配置、运行并排查这一流程。

#### 何时显式配置

- **按目标设置**：启动目标时在 Goals 页的 **Goal verify command** 输入框里填命令，只对该目标生效。留空则自动检测（见下）。
- **留空时自动检测**：未设命令时，orbit 会基于项目根的文件自动推测常见验证命令（如 `npm test`、`cargo test`、`python -m unittest discover -s tests` 等）。适合快速试用。
- **生产环境推荐显式声明**：命令应幂等、可离线运行、覆盖单元/集成测试。多模块单体建议指向自定义脚本（例如 `./scripts/goal-verify.sh`），脚本内部再按需调用各模块验证命令。

#### 命令设计原则

1. **幂等**：重复执行不会修改仓库状态，也不依赖交互输入。避免长驻服务或写入操作。
2. **离线可执行**：依赖应在 runner 主机预装好（包缓存、Docker 镜像、测试数据），避免访问外网导致波动或阻塞。
3. **覆盖面充分**：至少包含单元/集成测试，必要时追加 Lint、类型检查等；如命令过长，可封装脚本输出阶段日志。
4. **推荐模板**：
   - Python：`uv run pytest` 或 `poetry run pytest`
   - Node.js：`npm test -- --runInBand` / `pnpm test`
   - Go：`go test ./...`
   - Rust：`cargo test --all`
   - Monorepo：`./scripts/goal-verify.sh`

#### 超时与成本控制

- 验证命令受 `VERIFY_HARD_TIMEOUT_SECONDS`（默认 900 秒）限制；预估执行时间过长时，需优化命令或拆分目标，否则将被视为失败。
- `goal_verify` 是普通 shell / 测试命令，不经过 LLM，**本身不消耗 token、也不计入目标预算**（预算只统计各 agent step 的 token 用量）。需要注意的是相反方向：预算冻结发生在派发阶段，若目标在收敛前就耗尽预算，子任务会先被冻结，`goal_verify` 可能没有机会运行。

#### 结果与观测

- 每次运行都会在 UI 的 Runs 面板显示，并在状态目录写入日志：`<项目根>/.orbit/tasks/<goal_id>/run-XXX/verify`。
- 成功时目标状态自动置为 `accepted`；失败则为 `stalled`，并向 hub 发送通知提醒人工介入。所有子任务重新关闭即会自动再触发 `goal_verify`。

### Token 统计与预算

每次运行都记录 token 用量，按目标聚合，可查看也可设上限。

- **每次运行**：orbit 优先解析 agent CLI 自己的用量行，否则回落到引擎输出协议中的 `TOKENS_USED: <n>`（近似，模型自估）。
- **每个目标**：用量按目标整棵子树（目标 + 子任务 + step 卡）求和。**Goals** 标签页显示累计。
- **预算上限**：启动目标时**按目标**设一个 token 预算（在启动目标的对话框里），作为该目标整棵子树 token 的硬上限；`0` = 不限。目标超预算时冻结后续派发并通知 hub（目标转 `blocked`）。
- `goal_verify` 与 step 的 `verify` 命令不跑 LLM，所以**不耗 token**，也不计入预算。

## 本地 Web UI

`/ui` 是本地控制台，用来观察和操作工作流：

- 顶部 Project 下拉框切换已启动的项目 daemon
- 查看已安装的 Agent CLI 及其内置命令
- **看板**：任务按状态分列（todo / 进行中 / 测试 / 评审 / 阻塞 / 完成）
- **Workflow**：可视化编辑工作流图（步骤、Agent、Prompt、边及每 Agent 命令）
- **Jobs**：查看 `run_jobs` 执行队列（status / outcome / 领取者 / 租约到期），确认 runner 在正常消费
- **Goals**：查看 goal 进度、子树 token 消耗，可 **Force End** 强制结束（杀该 goal 全部在跑 runner + 关整树）
- **Settings**：UI 语言、**最大返工次数**（2–5，一个步骤最多返工几次后引擎将其阻塞）、**最大同时任务数**（1–6，所有 runner 加起来同时运行多少个工作流步骤）。存到 `.orbit/settings.json`。
- 查看每个 step 的运行日志（命令、退出码、stdout/stderr 尾部）

UI 只通过 `/api/*` JSON route 访问本地 store，且强制仅本机可访问。

## 任务协作模型

启动 Goal 后，引擎自动拆成业务子任务并行走工作流；每个步骤由其配置的 Agent 执行。

### 步骤 Agent 与约束

每个步骤默认不指定 Agent，并拥有自己的可编辑 Prompt——在工作流页面为各步骤分配 Agent。所有可达步骤都配好可运行的 Agent 后，goal 才能启动；启动前的预检会列出未设置的步骤。只有 `implement`、`review`、`test` 可配多个 Agent，其余步骤只能单个。步骤配置多个 Agent 时，Orbit 按持久化的步骤派发历史轮询（对不同任务做 round-robin）。每个 Agent 有自己的可选命令（留空则用其内置 CLI）。返工会把任务发回首次执行该步骤的同一个 Agent，让实现者保住自己的 worktree，而不是把半成品交给下一个 CLI。各步骤独立分配，因此 Review/Test/Integrate 保持各自的执行池。

约束：

1. 只有 `integrate` 步骤写主工作树；隔离步骤共享该任务自己的 worktree。
2. worker 每次只处理一个边界清楚的小任务。
3. worker 产出写文件，输出末尾给「一行结论 + 产物路径」并打印 `WORKFLOW_OUTCOME`。

### 任务内容格式

任务的 `content` 建议结构化，便于 agent 无歧义执行：

```
Task Type: review

Context:
- Repo path: ...
- Change under review: ...

Deliverable:
- Findings ordered by severity
- Missing tests
- Residual risk
```

### 任务状态

| 状态 | 含义 |
|---|---|
| `created` | 已创建，还未进入工作流 |
| `assigned` | 已派发给目标 Agent |
| `in_progress` | 某步骤的 runner 正在执行 |
| `blocked` | 被阻塞，需要输入或环境变化 |
| `closed` | 完成（终结态） |

Task 状态在所有工作流中固定不变。设计、评审、测试、审批等领域阶段属于工作流步骤，不属于 Task 状态。
