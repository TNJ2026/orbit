# orbit

**简体中文** | [English](./README.md)

> 本地多 agent 工作流编排器：把编码目标拆成任务，在一张可配置的工作流图上流转，runner 无头调用各 agent CLI（Claude Code、Codex CLI、Gemini CLI、自研 agent）执行「实现 → 测试 → 评审 → 集成」，每个任务在独立 git worktree 隔离进行，失败自动返工。

```
目标/任务 ──▶ orbit（工作流引擎 + 调度）──▶ runner ──▶ agent CLI（Claude Code / Codex / Gemini …）
                    │                                       │
                    └──────── SQLite ~/.orbit/projects/<project>/ ◀── 结果回收（WORKFLOW_OUTCOME）
```

- 一条命令起：Web UI + 调度 + 内嵌 runner 全在一个进程
- 任务在可配置工作流图上流转：并行分支、汇合、返工回环、机器验证门
- 每个任务在独立 git worktree 隔离执行，`integrate` 步骤把分支合并回主干；项目还不是 git 仓库时 orbit 自动初始化（带一个基线提交），未装 git 则降级为不隔离运行
- runner 无头调用 agent CLI，可按角色 / agent 拆分、水平扩展
- 状态持久化到 SQLite：进程重启不丢，超时 / 卡死有兜底

## 目录

- [安装](#安装)
- [快速开始](#快速开始)
- [高级用法](#高级用法)
- [工作流引擎](#工作流引擎)
- [本地 Web UI](#本地-web-ui)
- [任务协作模型](#任务协作模型)
- [角色文件](#角色文件)

## 安装

需要 Python ≥ 3.10 和 [uv](https://docs.astral.sh/uv/)。`git` 用于每任务
worktree 隔离（项目还不是 git 仓库时 orbit 会自动创建）；runner 调用各 agent
CLI（Claude Code、Codex 等），按需自行安装。原生支持 macOS、Linux、Windows
（进程控制按系统区分 —— POSIX 进程组、Windows `taskkill`）。

**全局命令（推荐）** —— 装一次，任何电脑、任何项目里直接 `orbit`：

```bash
uv tool install git+https://github.com/TNJ2026/orbit.git
uv tool update-shell            # 确保 ~/.local/bin 在 PATH（仅首次）
# 之后更新：  uv tool upgrade orbit
```

**从本地 checkout**（开发 orbit 本身时）：

```bash
git clone https://github.com/TNJ2026/orbit.git
uv tool install --editable ./orbit   # 全局 `orbit`，改代码即时生效
# 或不安装、就地跑：  cd orbit && uv run orbit serve
```

`uv run orbit …` 和 `uv tool` 首次使用时会自动建环境，**无需单独 `uv sync`**。
不用 uv 的话：`pip install -e .`。

## 快速开始

任何仓库，零配置：

```bash
cd <你的项目>          # orbit 编排当前目录所在的项目
orbit start            # gitignore .orbit/ + agents/，再用包内默认值直接 serve
```

然后打开 `http://127.0.0.1:8848/ui`：

1. **Team** 页 —— 给每个核心角色(hub / implementer / reviewer)指派一个 agent CLI(Claude Code、Codex 等)。`start` 自带默认角色提示词和默认工作流，但 **team 是空的**,所以起 goal 前需先做这一步(一次性)。
2. 给 **hub** 一个 goal —— 引擎把它拆成子任务并驱动它们走完工作流。

`start` 不落任何需要提交的文件(只往 `.gitignore` 补 `.orbit/` 和 `agents/`;UI 里改 role 仍会按需 materialize 出 `agents/`,但在 `start` 下不进 git),且支持全部 `serve` 参数(`--host` / `--port` / `--db` / `--no-runner` / `--runner-concurrency`)。(`orbit up` 是 `orbit start` 的向后兼容别名。)没全局装? `uvx --from git+https://github.com/TNJ2026/orbit.git orbit start` 临时拉起。已准备好、或直接用默认值? 直接 `orbit serve`。(从本地 checkout 且未全局安装时,命令前加 `uv run`。)

## 高级用法

### 定制并提交配置 —— `orbit config`

`orbit start` / `orbit serve` 无需任何准备。只有当你要改角色提示词、自定义工作流并提交给团队共享时,才用 `orbit config`(原 `orbit init`,别名仍可用):它会把 `agents/*.md`、`.orbit/workflow.json`、`team.json`、`CLAUDE.md` 段落写进仓库 —— 这些**故意不 gitignore**,供 commit 共享。它还会把核心角色铺到你已装的 agent CLI 上生成一个默认 team,所以之后往往能直接起 goal。

### 多进程：解耦 serve + 独立 runner

`serve` 默认内嵌一个 runner,所以 **serve 重启会中断在途 step**(租约到期后自动重跑)。要重启安全 / 多机 / 水平扩展,把调度和执行拆开:

```bash
# 终端 1：只跑 UI / 调度，不内嵌 worker
orbit serve --no-runner

# 终端 2+：独立 runner（重启 serve 不影响它们）
orbit runner --name runner-local
```

此模式下 run 活在独立进程里,serve 重启不杀在途任务 —— 调度暂停一下,runner 照跑。runner 是无状态 worker,可按 agent / 角色拆分并扩:

```bash
orbit runner --roles implementer --max-concurrency 2   # 2 个并行实现 worker
orbit runner --roles reviewer --agent antigravity      # 只跑 antigravity 的评审
orbit runner --project /path/to/repo --name box-a      # 显式指定项目
```

- `--agent NAME`（可重复）：只领分给该 agent 的 job。
- `--roles a,b`：只领这些工作流角色的 job（按 workflow 配置把角色解析成 step）。
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

## 工作流引擎

工作流引擎逻辑上是三层——**Scheduler**（决定下一步、推进）、**Runner/Worker**（执行 agent CLI）、以及它们之间的 `run_jobs` 队列。默认打包进一个进程，也可以拆开跑：

| 层 | 职责 |
|---|---|
| **Scheduler**（serve 内线程） | 把"要执行某 step"写入 `run_jobs` 队列；单点消费执行完的 job 并推进工作流（dispatch / rework / accept）；跑 timeout / health 兜底 |
| **Runner / Worker** | 从 `run_jobs` 领取任务（带租约 + 心跳）、执行各 agent 的 CLI、流式记录 stdout/stderr、解析 outcome，把结果写回 job |

默认工作流（设计优先）：`intake(hub) → product_design → [ui_design ∥ architecture] → plan(hub) → implement → test → review → integrate(hub) → accept(hub)`。goal 先跑一次设计步，再由 `plan` 拆成实现子任务（每模块一个），子任务各自从 `implement` 起；`review` 有一条回到 `implement` 的返工回环，`test` 可配 `verify` 命令作为机器验证门。runner 把步骤 prompt 交给 agent CLI 无头执行，agent 在末尾打印 `WORKFLOW_OUTCOME: done|rework|blocked` 汇报（详见 `agents/_protocol.md`）。

### 默认：一体进程

```bash
orbit serve        # UI + 调度 + 内嵌 Runner，全在一个进程
```

`serve` 默认**内嵌一个 in-process runner**（名字 `serve-embedded`，并发 5），所以启动一个 goal 后不需要再手动起 runner——建 job → 内嵌 runner 执行 → scheduler 推进，全自动。UI 的 **Jobs** 标签页能看到队列状态(pending / running / finished / done)。

> ⚠️ 内嵌 runner 与 serve 同生命周期：**serve 重启会中断在途 step**（租约到期后该 step 自动重跑）。要重启安全 / 多机 / 水平扩展，把它们拆开 —— 见[高级用法 → 多进程](#多进程解耦-serve--独立-runner)。

**job 生命周期：** `pending → running`（runner 领取）`→ finished`（runner 执行完、报告 outcome）`→ done`（scheduler 推进下一步）。

### 设计优先与 `decompose` 步

goal 在哪一步拆成子任务，由打了 `decompose: true` 的那步决定。默认工作流把 `plan` 标为 decompose，所以 goal **自己**先跑一次设计步（`intake → product_design → [ui_design ∥ architecture] → plan`），再由 `plan`（hub）**结合设计产出**输出子任务 JSON。每个子任务从拆解步的**后继步**（`implement` 起）开始并继承该产出——于是设计步在 goal 层**只跑一次**（而非每个子任务一遍），子任务按架构模块干净切分。

想换拆解点，就在 `.orbit/workflow.json` 里给别的步打标记；**不打** `decompose` 标记时，goal 会退回在入口步（`intake`）拆，每个子任务重跑整条工作流——更简单，但设计变成按子任务重复。该标记只能改 JSON 配置（同 `isolate`/`integrate`）；decompose 步自动 required、从不隔离、且必须有后继步供子任务起跑。

### 目标收敛验证（goal_verify）最佳实践

当一个目标（Goal）下的业务子任务全部自测通过并关闭后，orbit 会在主分支执行 `goal_verify` 命令，对整体成果进行客观验收。以下指南帮助正确配置、运行并排查这一流程。

#### 何时显式配置

- **默认自动检测**：如果未设置 `goal_verify`，orbit 会基于项目根的文件自动推测常见验证命令（如 `npm test`、`cargo test`、`python -m unittest discover -s tests` 等）。适合快速试用，但请在 Web UI 的 Workflow 面板确认检测结果是否符合预期。
- **生产环境推荐显式声明**：将命令写入 `.orbit/workflow.json`（或通过 UI / CLI 保存），并在团队文档中记录来源与依赖，避免默认检测随项目结构变化而漂移。
- **多模块/单体拆分**：若目标需要串连多个子项目，建议将 `goal_verify` 指向自定义脚本（例如 `./scripts/goal-verify.sh`），脚本内部再按需调用各模块验证命令。

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
- `goal_verify` 是普通 shell / 测试命令，不经过 LLM，**本身不消耗 token、也不计入 `goal_token_budget`**（预算只统计各 agent step 的 token 用量）。需要注意的是相反方向：预算冻结发生在派发阶段，若目标在收敛前就耗尽预算，子任务会先被冻结，`goal_verify` 可能没有机会运行。

#### 结果与观测

- 每次运行都会在 UI 的 Runs 面板显示，并在状态目录写入日志：`<项目根>/.orbit/tasks/<goal_id>/run-XXX/verify`。
- 成功时目标状态自动置为 `accepted`；失败则为 `stalled`，并向 hub 发送通知提醒人工介入。所有子任务重新关闭即会自动再触发 `goal_verify`。

### Token 统计与预算

每次运行都记录 token 用量，按目标聚合，可查看也可设上限。

- **每次运行**：orbit 解析每个 run 的用量 —— 优先用 agent CLI 自己的用量行（准确），否则回落到每个 runner 被要求打印的 `TOKENS_USED: <n>` 哨兵行（近似，模型自估；见 `agents/_protocol.md`）。计数存在 run 上、显示在该 run 的日志里；CLI 用量行是累计的，故取最后一条。
- **每个目标**：用量按目标整棵子树（目标 + 子任务 + step 卡）求和。**Goals** 标签页显示累计。
- **预算上限**：在工作流配置里设 `goal_token_budget` 作为目标整棵子树 token 的硬上限（每目标可单独覆盖工作流默认值；`0` = 不限）。目标超预算时冻结后续派发并通知 hub（目标转 `blocked`）。
- `goal_verify` 与 step 的 `verify` 命令不跑 LLM，所以**不耗 token**，也不计入预算。

## 本地 Web UI

`/ui` 是本地控制台，用来观察和操作工作流：

- 顶部 Project 下拉框切换已启动的项目 daemon
- 查看已安装的常见 agent CLI 与团队配置
- **看板**：任务按状态分列（todo / 进行中 / 测试 / 评审 / 阻塞 / 完成）
- **Workflow**：可视化编辑工作流图（步骤、角色、边、`verify` 命令、goal 预算）
- **Jobs**：查看 `run_jobs` 执行队列（status / outcome / 领取者 / 租约到期），确认 runner 在正常消费
- **Goals**：查看 goal 进度、子树 token 消耗，可 **Force End** 强制结束（杀该 goal 全部在跑 runner + 关整树）
- 查看每个 step 的运行日志（命令、退出码、stdout/stderr 尾部）

UI 只通过 `/api/*` JSON route 访问本地 store，且强制仅本机可访问。

## 任务协作模型

给 hub 一个目标（goal），引擎自动拆成业务子任务，各自并行走工作流；每个步骤由对应角色的 agent 无头执行。

### 角色分工与约束

默认工作流涉及的角色（见 `agents/`）：

- `hub`：编排者。拆 goal、集成合并、最终验收；不做大块实现 / review。
- `implementer`：按任务改代码并自测。
- `reviewer`：找 bug、测试缺口、设计风险；只评审不改码。
- `tester`：设计执行测试、复现失败、报告覆盖风险。
- `architect` / `product_designer` / `ui_designer` / `security_auditor` / `refactorer`：按需接入的设计 / 审计 / 重构角色。

约束：

1. 默认只有 `hub`（在 `integrate` 步骤）写主工作树；其它角色在各自 worktree 里干。
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
| `assigned` | 已派发给目标角色 |
| `in_progress` | 某步骤的 runner 正在执行 |
| `reviewing` | 在评审步骤 |
| `accepted` | hub 接受该结果（终结态之一） |
| `blocked` | 被阻塞，需要输入或环境变化 |
| `stalled` | 父级目标因子任务阻塞而停滞 |
| `closed` | 任务已归档（终结态之一） |

## 角色文件

`agents/` 目录提供开箱即用的角色定义：`_protocol.md`（公共**执行约定**）、`hub.md`（编排者）、`implementer.md`、`reviewer.md`、`tester.md` 等。runner 会把对应角色的 `.md` 注入到步骤 prompt 里，agent 据此干活。

`hub` 也可以作为你的**交互主会话**运行——负责拆 goal、集成与验收：

```bash
claude --append-system-prompt "$(cat agents/hub.md)"
```

`CLAUDE.md` / `AGENTS.md` / `GEMINI.md` 是薄入口，只含项目事实和角色指引。

新增角色：复制 `agents/_template.md` 为 `agents/<角色名>.md`，替换占位符即可——模板里带命名规则和职责拆分的判断标准。
