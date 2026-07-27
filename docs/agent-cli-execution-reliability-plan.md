# Agent CLI 正常收尾与可靠结算方案

## 1. 背景

Orbit 将 Workflow 的 action 节点交给本机 Agent CLI 执行。Agent 可能修改工作区、运行测试并生成文档，因此一次调用既是长时间运行的子进程，也是可能产生外部副作用的执行。

运行 `run:ec2050c6f2b5d10321c38a8a87720b7addf2c9c27b191bfb566d8ba09e7d946a` 暴露了当前链路的问题：第二步在重新运行全量测试期间没有正常返回，租约停止续期，最终由 Reaper 将 Attempt 标记为 `unknown_external_result`。Runtime 无法确认工作区修改是否完成，只能暂停 Run 并要求人工决定是否重跑。

这个问题不能只依靠提示词解决。提示词只能提高 Agent 主动收尾的概率，Runtime 仍必须在 Agent 不合作、CLI 卡死、子进程不退出、数据库暂时不可用或进程崩溃时给出有界、可解释的结果。

## 2. 目标

本方案要保证：

1. Agent 在时间和工作量预算内主动停止新工作并返回结果。
2. Agent CLI 及其派生子进程都受同一个生命周期管理。
3. 节点执行、CLI 超时、取消宽限期和租约有效期之间存在明确余量，且这些余量能被自动校验。
4. 达到截止时间后，Attempt 在有限时间内进入明确状态，不依赖租约自然过期。
5. 已经可能产生外部副作用的执行不会被 Runtime 静默自动重试；能证明无副作用的执行仍然可以自动重试。
6. 强制终止后工作区的实际状态可被观察，人工重跑前知道自己面对的是什么。
7. 页面和日志能够解释"为什么停止、停止在哪一步、是否允许重跑"。

非目标：

- 不保证第三方 Agent CLI 一定输出高质量结果。
- 不把 `unknown_external_result` 一律改成普通失败。
- 不把所有超时一律升级成 `unknown_external_result`（见 §4.3）。
- 不通过无限延长租约掩盖失控的 Agent。
- 不允许 Workflow 作者传入任意命令或覆盖受信任 CLI 的启动参数。

## 3. 总体设计

执行过程采用四层约束：

```text
Workflow 任务范围
  → Agent 主动收尾协议
  → CLI/进程组硬超时
  → Worker 强制结算（+ 可选的 Kernel 定时器）+ 租约与重启恢复兜底
```

四层必须同时成立。前一层减少异常发生，后一层处理前一层失效。

### 3.1 时间预算模型

时间约束分两个**互相正交**的维度，不能混在同一条轴上：

- **执行轴**：一次 Attempt 从开始到收尾的内部节奏，由 Handler Manifest 决定。
- **故障隔离轴**：Worker 执行单元停止续租后多久被发现，由租约决定。

早期版本把 `lease_expiry` 与 `soft_deadline` 等并列在同一张预算表里，写成 `node_deadline + 2 分钟`。这在实现上不成立——见 §3.1.2。

#### 3.1.1 执行轴

设 `start` 为 Attempt 开始时间，`D = manifest.resource_profile.max_duration_seconds`。

```text
node_deadline       = start + D
soft_deadline       = start + max(0.7 * D, D - 300)     # D < 600 时不启用
cleanup_reserve     = kill_grace + SETTLEMENT_MARGIN
process_deadline    = start + min(
  max(0.85 * D, D - 120),
  D - cleanup_reserve
)
settlement_deadline = process_deadline + kill_grace + SETTLEMENT_MARGIN
```

`SETTLEMENT_MARGIN` 默认 30s，`kill_grace` 取 `DEFAULT_KILL_GRACE_SECONDS`（当前 2.0s）。
Agent 节点配置的 `D` 不得小于 `MIN_AGENT_DURATION_SECONDS`（建议默认 60s），
并且必须严格大于 `cleanup_reserve`。服务端 Schema、编辑 UI 和启动期校验使用同一个最小值。

不能用固定分钟数往回减。`max_duration_seconds` 现在跨三个量级：

| handler | `max_duration_seconds` | 来源 |
|---|---|---|
| `agent.*` | 1800 | `AgentSpec.max_duration_seconds` 默认值 |
| dev tools（git/verify） | 900 | `handlers/dev_tools.py` `DEFAULT_TIMEOUT_SECONDS` |
| 内置 handler | 300 | `web/builtin_handlers.py` `TRANSFORM_MANIFEST` |
| 上限 | 86400 | `domain/handlers.py` `MAX_HANDLER_DURATION_SECONDS` |

`node_deadline - 5 分钟` 对 300s 的内置 handler 会落在 `start` 之前。比例公式配合下限保证任何合法 `D` 都得到单调递增的时间点：`soft_deadline < process_deadline < settlement_deadline <= node_deadline` 在 `D > cleanup_reserve` 的全区间成立。

收尾窗口的规模需要注意：`max(0.7 * D, D - 300)` 在 `D >= 1000` 时恒取 `D - 300`，配合 `process_deadline` 的 `D - 120`，收尾窗口固定为 180s，与 `D` 无关。`D = 86400` 的节点也只有 3 分钟收尾时间。如果意图是"任务越大给越长的收尾时间"，应改为纯比例项或提高常数上限；当前公式是"收尾窗口有上界"的取舍，需要显式确认这是想要的行为。

`settlement_deadline` **晚于** `process_deadline`，但不得晚于 `node_deadline`。结算是终止动作的后继，因此必须从节点总预算中预留，而不能在达到 `node_deadline` 后再追加。§10 的“达到硬截止后 60 秒内、且不晚于节点截止时间完成 Attempt 结算”是这条公式的验收表述，两者必须一致。

#### 3.1.2 故障隔离轴

租约不是覆盖整个节点执行的一次性窗口，而是每 `renew_interval_seconds` 滚动续期的短窗口：

| 常量 | 当前值 | 位置 |
|---|---|---|
| `MAX_JOB_LEASE_TTL` | 5 分钟 | `domain/durable_execution.py` |
| `JOB_LEASE_TTL` | 120s | `worker/runtime.py` |
| `renew_interval_seconds` | 10.0 | `worker/supervisor.py` |
| `max_consecutive_renewal_failures` | 3 | `worker/supervisor.py` |

上限在两处执行，规则不同：

- **初始 claim**：`_durable_claim_job` 校验 `issued_at < expires_at <= issued_at + MAX_JOB_LEASE_TTL`，这是**绝对上限**，由 kernel 强制。
- **续租**：`durable_runtime_service.renew_lease` 校验 `expires_at - current.expires_at <= 5 分钟`，这是**单次延长上限**，由服务层强制，不构成绝对上限。

因此 `lease_expiry = node_deadline + 2 分钟` 两种解读都不成立：若理解为 claim 时一次性设定 32 分钟租约，kernel 直接拒绝；若理解为靠持续续租达到，那它就是今天已有的滚动行为，写进预算分配表里没有增加任何约束。

5 分钟绝对上限是**故意的**故障隔离窗口：Worker 执行线程或整个 Orbit 进程停止续租后，服务存活时由 Reaper 回收；若整个服务已退出，则在重启恢复后回收。

租约与 `node_deadline` 无关，需要校验的是这一条：

```text
renew_interval_seconds * max_consecutive_renewal_failures
  + kill_grace + SETTLEMENT_MARGIN
  < lease_ttl
```

当前值：`10 * 3 + 2 + 30 = 62 < 120`，成立。这条不等式的含义是：从续期开始连续失败，到 Worker 完成强制结算，必须发生在租约到期之前，否则 Reaper 和 Worker 会同时试图改写状态。

#### 3.1.3 必须拒绝的配置

启动时校验，不通过则拒绝启动而不是静默截断（`PlannerDispatcher` 已经是这个做法）：

- `D < MIN_AGENT_DURATION_SECONDS` 或 `D <= cleanup_reserve`。
- §3.1.2 的租约不等式不成立。
- `MIN_AGENT_DURATION_SECONDS`、`cleanup_reserve` 在 Schema、编辑 UI 和服务端三处取值不一致。

以下三条由 §3.1.1 的公式**恒定保证**，写成断言而不是配置校验——没有任何配置能触发它们，读者不必去寻找触发条件：

- `process_deadline < node_deadline`
- `settlement_deadline <= node_deadline`
- `settlement_deadline - process_deadline >= kill_grace`

CLI adapter 的超时**不是启动期可校验项**，见 §4.1 缺口 5：它当前是进程级构造参数，而 `process_deadline` 是每个 Attempt 计算的，两者不在同一个作用域。正确做法是让 adapter 每次调用从 `ExecutorRequest.deadline` 派生超时，构造参数只作为上限兜底。

时间值由一处计算并下发，不散落为互相独立的魔法数字。

### 3.2 Agent 主动收尾协议

在所有 Agent action 节点的运行提示词末尾追加 Runtime 约束，不依赖 Workflow 作者重复书写：

> 在有限时间内完成任务。优先运行与修改相关的定向测试，不要默认运行完整测试套件。收到收尾通知、剩余时间不足、测试失败或无法继续时，停止启动新操作并立即返回当前结果。结果必须说明已完成修改、已运行测试、错误和剩余工作；任务未全部完成也必须返回，不得持续执行直到被强制终止。

#### 3.2.1 注入位置与可回放性

`render_agent_prompt(prepared.payload["input"], prepared.payload["config"])` 从 `node_input_prepared` 事件的 payload 构造提示词。注入分两类，处理方式不同：

- **静态约束**（上面那段文字）：作为常量拼在 `render_agent_prompt` 里即可。它随代码版本走，不进事件流。
- **动态预算**（"你还剩 N 分钟"、具体的 `soft_deadline` 时刻）：**必须在 prepare 阶段写入 `node_input_prepared` 的 payload**，不能在 Handler 执行时读 `clock()` 现拼。否则同一个 Attempt 的输入不再由事件唯一决定，回放会构造出与当时不同的提示词。

这是事件溯源的硬约束，不是风格问题。第一阶段可以只注入静态约束；引入动态预算时同步扩展 `node_input_prepared` 的 payload 契约。

#### 3.2.2 结构化结果需要改 Manifest

期望的返回形状：

```json
{
  "status": "completed | partial | blocked | failed",
  "summary": "本次已完成的工作",
  "changes": ["修改摘要"],
  "tests": [
    {"command": "...", "status": "passed | failed | timed_out", "summary": "..."}
  ],
  "errors": ["错误或阻塞原因"],
  "remaining_work": ["尚未完成的工作"]
}
```

但 action 节点的 outputs 必须**精确等于** handler manifest 的 ports——这是编译期的 `DSL_PORT_INCOMPATIBLE` 规则，节点不能自定义输出形状。agent manifest 现在只有单个 `AGENT_RESULT_PORT`，schema 是 `schema://object/1.0`，即裸 `{"type": "object"}`。

所以引入这个结构需要二选一：

- **A（推荐）**：新增 `schema://agent-result/1.0` 并把 `agent_manifest()` 的 `result_schema_id` 指向它。影响 `catalogs/agent_discovery.py` 与 schema catalog，需要评估已发布 workflow 的兼容性（旧版本节点绑定的是旧 schema_id，`definition_hash` 不变，因此只影响新发布）。
- **B**：保留裸 object schema，把结构作为**约定**写进提示词，Runtime 只做尽力解析，缺字段时降级。改动小，但没有编译期保证。

`partial` 和 `blocked` 是正常返回，不等于节点执行成功。Workflow 的路由策略决定它们进入修复、人工处理还是失败终态。

对于纯文本或 Artifact 输出，使用 Markdown 模板表达相同字段。

### 3.3 限制单步任务范围

生成 Workflow 时增加以下约束：

- 编码与定向测试可以在同一个步骤中完成。
- 全量测试、长时间构建、端到端测试应拆成独立验证步骤。
- 一个步骤不得同时承担需求分析、跨模块实现、全量验证和最终报告。
- 测试命令必须有独立超时，且不得超过节点剩余预算。
- Agent 在剩余预算不足时不得启动预计无法完成的新命令。

这部分既写入 Workflow 生成提示词，也在发布前检查中给出诊断。第一阶段仅告警，形成数据后再决定是否作为发布门禁。

### 3.4 Run 级预算

节点级预算不构成 Run 级上限。一个 Goal 可以并行展开多个 agent 节点，每个各自 30 分钟，Run 的总时间与总成本目前无界。

补充两个 Run 级约束：

- `run_max_duration_seconds`：超过后不再调度新节点，已在执行的节点走各自的收尾路径，Run 进入 `budget_exhausted` 终态。
- `run_max_cost_microunits`：复用 Planner 已有的 Run 预算账户机制（`_reserve_budget`），把 agent 节点的 `cost_microunits` 纳入同一账户。

与 §4.6 的交互：并行的 N 个 agent 节点若各自隔离，就会同时存在 N 个 worktree。需要定义并发 worktree 上限、磁盘占用估算，以及 `run_max_duration_seconds` 触发时这些 worktree 的归属——它们承载着未交付的改动，不能随 Run 终止一起删除。

这几项在 §10 需要对应的观测指标，否则无法定阈值。

## 4. Runtime 改造

### 4.1 进程组生命周期：能力已在平台层，Agent 路径未接入

关键事实：`orbit.platform.process` 具备完整的进程树生命周期管理，**但 Agent CLI 这条路径一项都没有用到它**。使用它的是 dev tools（`WorkspaceRunner` → `process_port.run`）；出问题的恰好是没用它的那条。

`TrustedCliAgentClient._run` 自己起进程（`handlers/agent.py`）：

```python
process = subprocess.Popen(
    (*self.command, *extra_args), stdin=subprocess.PIPE,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.environment,
)
```

取消路径同样是裸 `Popen` 语义：

```python
process.terminate()
try:
    process.wait(timeout=self.kill_grace_seconds)
except subprocess.TimeoutExpired:
    process.kill()
    process.wait()
```

`Popen.terminate()` / `Popen.kill()` 只作用于**主进程**，不是 `killpg`，不是进程树。

| 能力 | `platform/process.py`（dev tools 路径） | `TrustedCliAgentClient`（Agent 路径） |
|---|---|---|
| 独立 session / process group | ✅ `detached_process_kwargs()` | ❌ 无 |
| 组 `SIGTERM` | ✅ `terminate_pid_tree()` → `os.killpg` | ❌ `Popen.terminate()` |
| 组 `SIGKILL` + 逃逸子进程 | ✅ `kill_pid_tree()`，杀前快照进程树 | ❌ `Popen.kill()` |
| 关闭管道解开卡死的读线程 | ✅ `ProcessHandle.kill()` 显式 close | ❌ 无 |
| grace 等待 | ✅ `ProcessHandle.cancel(grace_seconds=...)` | 部分（仅主进程） |
| 输出读取 | ✅ `ProcessHandle` 内建，带 `on_stdout` 回调 | 手写 `_communicate_bounded` 三线程 |
| 幂等 | ✅ `_lock` + `_cancelled` | 部分 |

这解释了 §1 故障的完整机制：CLI 启动的测试进程继承 stdout → CLI 主进程被 `terminate()` 杀掉 → 孙进程仍持有管道写端 → drain 线程读不到 EOF → `thread.join(timeout=kill_grace_seconds)` 超时后**静默返回** → 线程与子进程都还活着，Handler 却已经返回。

因此本节是**实质性改造**，不是验证：

1. **把 `TrustedCliAgentClient` 迁移到 `ProcessHandle`**。这是替换而非重写：`ProcessHandle` 已经提供 `stdin_text`、`on_stdout`/`on_stderr` 流式回调、`max_output_bytes` 上限和 `cancel()`，正好覆盖 `_communicate_bounded` 手写的三线程与 `publish` 逻辑。迁移后 Agent 自动获得进程组、组信号和管道关闭。
2. `ProcessHandle.wait()` 中 `thread.join(timeout=DEFAULT_KILL_GRACE_SECONDS)` 超时后，若 drain 线程仍未退出，代码直接返回，没有记录也没有回收。需要计数并上报，否则迁移只是把泄漏换了个位置。
3. `DEFAULT_KILL_GRACE_SECONDS = 2.0` 对"正在写文件的测试进程"偏短。应改为可配置，agent 节点用更长的值（建议 10s），并同步进 §3.1.1 的 `cleanup_reserve`。
4. 终止原因当前没有被保存到 `ProcessResult`，只有 `timed_out` / `cancelled` 两个布尔量，不足以支撑 §4.3 的映射表。
5. **adapter 超时改为 per-invocation**。`TrustedCliAgentClient.timeout_seconds` 现在是构造参数（组合根 `agent_handlers(timeout_seconds=1800)` 一次性传入，所有节点共用），与 §3.1.1 按 Attempt 计算的 `process_deadline` 不在同一个作用域。`_run` 已经能拿到 `context.request`，改为从 `ExecutorRequest.deadline` 派生本次超时，构造参数降级为上限兜底。不做这一步，§5.1 接通 `config.timeout_seconds` 后会出现"节点配置 600s、adapter 仍 1800s"的不一致。
6. Windows `taskkill` 路径缺少覆盖测试。

平台差异继续封装在 `orbit.platform.process`，Handler 不直接实现平台分支——迁移后这条约定才第一次对 Agent 路径真正生效。

### 4.2 软截止与硬截止

第一阶段先实现硬截止：到 `process_deadline` 终止进程组。若 CLI 支持可控输入或协议化通知，再增加软截止；不能假设所有 CLI 都能在运行中读取新的 stdin 指令。

不支持软通知的 CLI 仍通过初始提示词获知总预算（§3.2.1 的动态预算路径），并由 Runtime 在硬截止时兜底。

在软截止真正落地之前，UI 不得展示"正在收尾"这一状态——见 §6。

### 4.3 取消原因与结果映射：按 ExecutionSafety 分流

必须保存最初触发终止的原因，并进行稳定映射。**判据已经存在**：`HandlerManifest.execution_safety` 有 `REPLAY_SAFE` 与 `UNKNOWN_ON_LEASE_LOSS` 两个取值，agent manifest 声明的正是后者，内置 `transform` 是前者。`manifest.capabilities` 里的 `workspace.write` 可作为第二判据。

早期版本把所有 deadline 超时一律升级为 `unknown_external_result` 且禁止自动重试。代价是**每一次超时都变成需要人工介入的 Run 暂停**，包括只读的调研、总结、报告生成节点——这些本来可以安全自动重试。

分流后的映射：

| 原因 | `execution_safety` | 外部副作用状态 | Attempt 结果 |
|---|---|---|---|
| 用户在执行前取消 | 任意 | 未开始 | `cancelled` |
| 用户在 Agent 已启动后取消 | `REPLAY_SAFE` | 无 | `cancelled` |
| 用户在 Agent 已启动后取消 | `UNKNOWN_ON_LEASE_LOSS` | 不确定 | `unknown_external_result` |
| 达到 `process_deadline` | `REPLAY_SAFE` | 无 | `failed/attempt_timeout`，走 retry policy |
| 达到 `process_deadline` | `UNKNOWN_ON_LEASE_LOSS` | 不确定 | `unknown_external_result`，原因 `deadline_exceeded` |
| 输出超限且已执行工具 | `UNKNOWN_ON_LEASE_LOSS` | 不确定 | `unknown_external_result` |
| 输入或配置校验失败 | 任意 | 未执行 | `failed/permanent_error` |
| CLI 启动失败 | 任意 | 未提交请求 | `failed/permanent_error` 或 `transient_error` |
| CLI 明确返回结构化失败 | 任意 | 已知 | 按返回契约路由 |

原则不变：在不能证明"未产生副作用"时，不得把结果降级为可自动重试的普通 timeout。改变的是——`execution_safety` 就是这个证明，已经在 manifest 里，直接用。

### 4.4 Worker 强制结算

`LeaseSupervisor` 不应只取消 Token 然后停止续租。它需要向 Worker 发布一个明确的终止信号，Worker 必须在 `settlement_deadline` 前执行结算。

#### 4.4.1 为什么不能是"执行前后检查"

`WorkerRuntime.run_once` 里 `executor.execute(request, token)` 是**同步阻塞**调用。Handler 卡住时，Worker 主循环根本没有机会运行任何事后检查——它就在那一行上等着。任何"在 execute 之后判断 supervisor 是否还活着"的设计都无法执行。

因此强制结算必须建立在明确的双线程控制结构上，而不是在单线程里插检查点。

#### 4.4.2 双线程控制结构

1. Worker **控制线程**创建 Handler **执行线程**，由后者调用 `executor.execute()`。
2. 控制线程同时等待三个信号：Handler 结果、Supervisor 异常退出、`settlement_deadline` 到期。
3. Supervisor 正常触发 deadline 时由它负责取消进程组；Supervisor 异常退出时，控制线程立即补发取消。
4. 到 `settlement_deadline` Handler 执行线程仍未返回时，控制线程按 §4.3 的映射强制结算，并采集工作区快照（§4.6）后释放租约。
5. Handler 执行线程迟到返回时只记录 late result，不能再次结算。

即使 Handler 执行线程仍未退出，也不能等待 Reaper 才改变 Run 状态。迟到结果必须被 fencing token 拒绝，并记录为审计事件。

为避免两个线程同时结算，Worker 使用单次 settlement gate；只有第一个合法结算写入状态，后续结果记录为 stale/late result。

该结构改动限于 `worker/runtime.py` 与 `worker/supervisor.py`，不触碰 kernel 与事件契约。

#### 4.4.3 泄漏的 Handler 线程

第 5 条意味着结算之后可能残留一个仍在运行的 Handler 执行线程。"受控后台线程"必须有具体策略，否则只是愿望：

- 泄漏线程继续占用它的 Worker slot，**不在结算时释放**。释放一个仍可能写文件、仍持有子进程的线程所占的额度，等于允许无限并发。
- Worker 池可用 slot 降到 0 时，Runtime 进入 **degraded 状态并显式告警**，而不是静默停止拉取 job。UI 与 §10 指标都要能看到"有 N 个 slot 被泄漏线程占住"。
- 泄漏线程退出时归还 slot。归还是唯一的恢复路径；不提供强制回收，Python 没有安全的线程终止手段。

**与 §4.1 的依赖必须写明**：Handler 执行线程之所以会不退出，正是因为它卡在 `process.wait()` 或 drain 线程上——而 §4.1 的 `ProcessHandle` 迁移就是消除这个原因的。

- 阶段二做对了 → 阶段四的线程泄漏是罕见异常，degraded 状态几乎不会出现。
- 阶段二没做 → 阶段四会**稳定泄漏**，每次 agent 超时留下一个线程，很快耗尽 Worker 池。

所以阶段二不只是"减少故障"，它是阶段四能否安全落地的前提。

### 4.5 Kernel 定时器兜底：接通已有的 NODE_TIMEOUT（可选项）

§4.4 的强制结算活在 Worker 执行线程内。Worker 线程卡死但 Orbit 主进程和 Timer 线程仍健康时，需要一个不依赖该 Worker 线程的持久化兜底。

先给结论：**这一节是独立决策项，不是阶段四的必做项**。§4.5.1 说明它真正独有的覆盖面只有一个窄场景，而同一场景有成本低得多的替代手段。下面先交代边界，再给成本对比，最后保留完整实施细节备用。

`durable_kernel.py` 里已经有一套持久化的兜底实现：`_durable_fire_timer` 的 `TimerPurpose.NODE_TIMEOUT` 分支会合成 `fail_attempt` 命令、走 retry policy、建 backoff timer、终结租约。**但全仓库没有任何地方创建这个 purpose 的 timer**，它是死代码。

必须明确它的保证边界：Orbit 当前把 Runtime、Worker 和 Timer 放在同一个进程中，因此 NODE_TIMEOUT 不是“进程外”执行器。

- Worker 线程卡死、Timer 线程和数据库仍健康时：Timer 可以在 `due_at` 附近触发。
- Orbit 整个进程退出或被 `kill -9` 时：Timer 不能准时执行，只能在服务重启、恢复扫描后补发。
- 数据库或整个进程阻塞时：Timer 同样不能提供实时截止保证，租约和重启恢复仍是最终隔离手段。

如果未来要求"Orbit 主进程死亡后仍在截止时间当场触发"，需要把 Timer Dispatcher 部署为独立进程；这不属于本方案当前阶段。

#### 4.5.1 实际覆盖面与成本对比

顺着上面的边界推下去，NODE_TIMEOUT 真正独有的覆盖面比直觉窄：

§4.4.2 的双线程控制结构落地后，覆盖面重新划分如下：

| 场景 | 谁兜底 |
|---|---|
| Handler 卡在 `executor.execute()` | `LeaseSupervisor` 负责**终止进程组**；§4.4.2 的控制线程负责**结算**。两者分工，不需要 timer |
| `LeaseSupervisor` 线程自身异常死亡 | §4.4.2 第 3 条：控制线程补发取消，第 4 条到期结算 |
| 数据库长时间阻塞 | 都不覆盖。NODE_TIMEOUT 的结算同样要写库，一起阻塞 |
| 整个 Orbit 进程退出 / 被强杀 | 只能重启后由恢复扫描补发，不是实时 |
| **Worker 控制线程本身死亡，且数据库健康** | **只有 NODE_TIMEOUT** |

早先认为 supervisor 死亡是 NODE_TIMEOUT 的独有场景，那是在双线程结构写入之前。控制线程本来就在等 supervisor 的异常退出信号，所以那一格已经被覆盖。剩下的独有场景收窄为控制线程自身死亡——比 supervisor 死亡更罕见一档，因为控制线程只做等待与结算，不执行 Handler 代码。

代价一侧，接通 NODE_TIMEOUT 是本方案最深的一处改动：改 kernel 的 `_durable_start_job`、在 complete/fail 路径加 cancel、改 fire 分支的重试语义、扩展 `StartJob` payload 契约并维护向后兼容分支。为一个更窄的场景动 kernel。

**建议**：阶段四实现 §4.4.2 即可，把 NODE_TIMEOUT 接通拆为独立的可选项。它的完整价值要等到 Timer Dispatcher 能独立于 Runtime 进程部署时才兑现——那时"进程外兜底"才名副其实，两件事一起做收益最大。下面的接通方案保留，作为决定要做时的实施细节。

#### 4.5.2 接通方案（可选项，非阶段四必做）

1. `_durable_start_job` 在 Attempt 进入 `RUNNING` 时调用 `_make_timer`，`purpose=TimerPurpose.NODE_TIMEOUT`，`dedupe_key=f"{job.job_id}:node_timeout:{attempt.attempt_number.value}"`，`due_at = settlement_deadline + TIMER_GRACE`（`TIMER_GRACE` 取 10s 量级）。

   `process_deadline` 只负责开始取消，Timer 必须给 Worker 留出完整的终止、管道回收和正常结算窗口——这是加 `TIMER_GRACE` 而不是恰好取 `settlement_deadline` 的原因。两者若同一时刻触发，正常路径下 Worker 的结算事务与 timer 会竞争同一个聚合：fencing token 和 `expected_version` 挡得住，但 §10 把 `node_timeout_timer_fired` 定义为"Worker 侧结算未能生效的次数"，正常竞争会把这个指标污染成无意义的噪音。留出宽限后，Worker 有确定的优先权，timer 每一次触发都真的意味着 Worker 失败了。

   注意 `due_at` 可以晚于 `node_deadline`。§3.1.1 的 `settlement_deadline <= node_deadline` 约束的是 Worker 的结算目标；timer 是"Worker 已经失败"的观测者，不受同一条预算约束。
2. Kernel 不认识 manifest，`settlement_deadline` 必须由 `StartJob` 命令的 payload 携带。`durable_runtime_service.start_job` 处已经能 resolve registry（`build_executor_request` 就在同一个服务里），在那里计算并放入 payload。
3. **向后兼容**：payload 缺 `settlement_deadline` 时不建 timer，行为与今天一致。旧事件回放不受影响。
4. 正常完成时取消该 timer（`_durable_complete_job` / `_durable_fail_job` 调 `cancel_timer`），避免已结算的 Attempt 被定时器二次触发。settlement gate 与 fencing token 已经能挡住重复写入，取消只是减少噪音。
5. **修改 fire 分支的语义**：现有实现无条件走 retry policy 自动重试，与 §4.3 冲突。需要按 `execution_safety` 分流——payload 里带上该字段，`UNKNOWN_ON_LEASE_LOSS` 直接结算 `unknown_external_result` 而不进 `RETRY_WAIT`。

同一处还有 `TimerPurpose.LEASE_RECOVERY`，同样只有 fire 分支没有创建点，租约兜底目前依赖 `app.py` 的 `_recovery_pass` 轮询。是否一并接通可以分开决策，但应记录为已知的实现/设计偏差。

### 4.6 工作区一致性

§1 的核心痛点是“无法确认工作区修改是否完成”，但仅靠 §4.3 判 unknown、§5 要求人工重跑并不解决它：人工点重跑后，新 Attempt 如果仍在**同一个 worktree** 上运行，面对的是上一次跑到一半的改动。§10 的“因超时而人工重跑后产生重复修改的事件数”这条指标，正是这个缺口的症状。

Attempt 开始前和强制结算时分别采集并持久化工作区快照：

- `git rev-parse HEAD`
- `git status --porcelain` 的摘要（文件数、新增/修改/删除计数、路径列表上限 N 条）
- 采集时刻与采集耗时

两份快照挂在 Attempt 上，随 `unknown_external_result` 一并展示。它们用于说明执行前后发生了什么，但仅凭 `git status` 仍不能证明某项改动一定由 Agent 产生，因为用户或其他进程可能同时修改共享工作区。

#### 4.6.1 隔离的判据

默认安全方案是让会修改代码的 Agent Attempt 在独立 worktree 中执行。但**"会修改代码"目前没有判据**：

- `AgentSpec.capabilities` 默认是 `("agent.invoke",)`，**不含 `workspace.write`**。带 `workspace.write` 的是 dev tools，不是 agent。按 capability 判不出来。
- Agent CLI 现在**不传 `cwd`**，继承 `orbit serve` 进程的工作目录。

因此必须先补一个判据，三选一：

- **A（推荐）**：给 `AgentSpec` 增加 `writes_workspace: bool`，由 agent 规格声明；讨论中的 CLI 默认为真。判据进 manifest，与 §4.3 的 `execution_safety` 同源。
- **B**：节点级配置开关，作者在编辑弹窗里选"这一步会改代码"。灵活但依赖作者正确性。
- **C**：全部 agent 节点一律隔离。最简单，但对只读节点是无谓开销，也改变了它们看到的工作目录。

#### 4.6.2 语义变更：Agent 看到的工作目录

隔离是**行为变更**，不是纯增强。Agent 从"看到用户当前工作区（含未提交改动）"变成"看到某个 commit 的干净副本"。对"帮我改这个 bug"这类基于用户当前状态的任务，后者可能不是用户要的。

方案必须显式选择其一并写进文档：

- 隔离 worktree 从 `HEAD` 创建（干净基线，丢失未提交改动的可见性）；
- 或从"当前工作区状态"创建（先 stash/commit 到临时分支再建 worktree，保留可见性，代价是多一次写操作）。

#### 4.6.3 结果如何交付回用户

**隔离解决了重跑污染，但制造了新问题**：Agent 成功完成后，worktree 里的改动怎么回到用户手里？只写重跑分支而不写正常路径，方案是不完整的。

四种可选交付方式，必须定一个：

| 方式 | 说明 | 代价 |
|---|---|---|
| 生成 patch Artifact | `git diff` 存为 Artifact，用户自行 apply | 最安全，但多一步人工操作 |
| 自动 merge 回当前分支 | 成功即合并 | 冲突处理复杂，且是对用户工作区的写操作 |
| 提交到临时分支并提示 | Agent 的改动落在 `orbit/attempt-<id>` 分支 | 语义清晰，用户可自行 merge/cherry-pick |
| 保留 worktree 路径 | UI 给出路径，用户自己进去看 | 零风险，但发现成本高 |

推荐"提交到临时分支 + 生成 patch Artifact"组合：前者可追溯，后者可消费，两者都不动用户当前工作区。

#### 4.6.4 重跑选择

重跑对话框显示基线、结束快照和 diff 摘要，并让用户显式选择：

- **继续当前 Attempt worktree**：保留上一次的部分修改，在同一隔离 worktree 中继续（默认）。
- **从基线创建新 worktree**：保留旧 worktree 供检查，从执行前记录的 commit 创建新的隔离 worktree 后重跑。

不得对用户共享工作区自动执行或在普通重跑流程中提供 `git reset --hard`、`git clean -fd`。清理隔离 worktree 也必须先确认其没有需要保留的 Artifact 或改动，并采用可恢复的归档/备份策略。

实现位置：`src/orbit/workspace/git.py` 增加只读快照和隔离 worktree 生命周期方法；`WorkspaceRunner` 已经持有 provider，采集路径现成。Agent 路径还需要把 worktree 路径作为 `cwd` 传给进程——这依赖 §4.1 的 `ProcessHandle` 迁移（`run()` 已有 `cwd` 参数）。

若第一阶段尚未实现 worktree 隔离，则 UI 只允许"保留当前工作区并继续"，不提供自动重置。

### 4.7 租约续期失败

连续续期失败需要记录。`LeaseSupervisor` 已经在内存里维护了 `renewal_failures`、`consecutive_renewal_failures`、`last_known_expiry` 三个字段，只是从未上报。续期失败很可能正是 SQLite 阻塞或不可写导致的，因此失败发生时不能依赖同一个 Runtime 数据库立即追加事件。

记录分两层：

1. **失败当下**：写结构化进程日志和内存指标，不访问 Runtime DB；记录失败次数、异常类型、最后成功续期时间和已知过期时间。
2. **数据库恢复或结算时**：best-effort 写入一条聚合摘要事件，记录失败时间范围、累计次数、最后异常、是否触发终止及是否完成结算。写摘要失败不得阻止取消或结算。

不为每个续期间隔写一条领域事件，避免数据库故障期间放大写压力。聚合摘要需要补齐：

- 失败次数（已有）。
- 异常类型和稳定错误码（需新增）。
- 最后一次成功续期时间（需新增）。
- 当时的已知租约过期时间（已有 `last_known_expiry`）。
- 是否成功触发进程组终止（需新增）。
- 是否完成强制结算（需新增）。

达到失败阈值后立即进入 §4.4 的强制结算流程，不能静默退出续期线程。

## 5. Workflow 生成与编辑约束

Workflow 生成提示词增加以下规则：

1. 每个 action 必须在有限时间内产生可消费的输出。
2. 步骤标题简短，步骤职责单一。
3. 编码步骤优先定向测试；全量测试默认拆为独立节点。
4. 每个 Agent 步骤必须允许部分完成时返回可解释结果。
5. 不得要求 Agent 无限修复直到测试全部通过；循环必须由 Workflow 的显式 back edge 和有界 policy 控制。
6. 最终报告、文档或计划默认输出 Markdown Artifact。

### 5.1 "预期最长耗时"旋钮已存在，接线即可

agent manifest 的 config schema 已经声明了这个字段：

```python
{"prompt": {"type": "string"},
 "timeout_seconds": {"type": "integer", "minimum": 1}}
```

但它当前**完全没有生效**：

- `build_executor_request` 计算 deadline 只读 `manifest.resource_profile.max_duration_seconds`，不看节点 config。
- `AgentCliAdapter.timeout_seconds` 是构造参数（`builtin_handlers` 传 1800），不读 config。

所以本项不是设计新旋钮，而是把已声明的字段接进 §3.1.1 的公式：`D = min(config.timeout_seconds or manifest.max_duration_seconds, manifest.max_duration_seconds)`。同时把 config schema 的 `minimum` 从 1 调整为统一的 `MIN_AGENT_DURATION_SECONDS`（建议默认 60），并由服务端再次校验 `D > cleanup_reserve`。编辑弹窗直接绑定这个 config 字段，明确显示该值包含进程终止和结算时间；实际硬截止由服务端计算，前端不直接控制租约参数。

**依赖 §4.1 缺口 5**：接通节点级 `D` 之后，每个 Attempt 的 `process_deadline` 各不相同，而 adapter 的超时仍是全局构造参数。必须先把 adapter 超时改为从 `ExecutorRequest.deadline` 派生（阶段二），否则会出现"节点配置 600s、CLI 仍等到 1800s"的不一致——CLI 会在 Runtime 已判超时之后才返回，把每一次超时都推成 late result。

## 6. 可观测性与 UI

运行详情中区分：

- `正在执行`：租约正常续期且 Handler 活跃。
- `正在收尾`：**仅在软截止能力落地后启用**。在只有硬截止的阶段，这个状态只能靠 `now >= soft_deadline` 推算，与 Agent 是否真的在收尾无关，属于谎报，不做。
- `正在终止`：Runtime 正在终止进程组。
- `结果未知`：已可能产生副作用，必须人工确认后重跑。

Attempt 详情显示：

- 开始时间、软截止、硬截止、结算截止和最终停止时间。
- 最后一条 Agent 输出时间。
- 最后一次租约续期时间。
- 终止原因、终止信号和清理耗时。
- 返回结果、部分结果或 unknown 的判定依据（含触发分流的 `execution_safety`）。
- 工作区快照摘要（§4.6）。

结构化记录分两类，**不能混为一谈**——§4.7 的第一层明确要求续期失败当下不访问 Runtime DB，把它和领域事件写在同一个列表里会诱导实现者在数据库已经不可写的时刻去写数据库。

**领域事件（写入 Runtime DB，可回放、可审计）**：

- `agent_execution_started`
- `agent_soft_deadline_reached`
- `agent_execution_returned`
- `agent_execution_forced_settlement`
- `node_timeout_timer_fired`
- `lease_renewal_failure_summary` — **聚合事件**，一次失败区间一条，含失败时间范围、累计次数、最后异常、是否触发终止、是否完成结算（§4.7 第二层）。不是每个续期间隔一条。
- `late_handler_result_rejected`
- `workspace_snapshot_captured`

**进程日志与内存指标（不写 DB，数据库故障期间仍可用）**：

- `agent_process_group_terminate_requested`
- `agent_process_group_killed`
- `lease_renewal_failed` — 每次续期失败当下写这里，这是 §4.7 第一层
- `handler_thread_leaked` — 当前泄漏线程数与被占用的 Worker slot 数，**状态量而非瞬时事件**，随 §4.4.3 的 degraded 状态一并暴露
- `process_drain_thread_leaked` — 同上，`ProcessHandle` 层的泄漏计数

日志只记录输出大小、摘要哈希和错误类别，不默认重复记录完整 Prompt、Agent 输出或秘密。

## 7. 代码改动范围

| 位置 | 改动 |
|---|---|
| `src/orbit/workflow/handlers/agent.py` | **核心**：`TrustedCliAgentClient` 从裸 `Popen` 迁移到 `ProcessHandle`（§4.1）；adapter 超时改为从 `ExecutorRequest.deadline` 派生；保存终止原因；传入 worktree `cwd`；静态收尾约束注入 `render_agent_prompt` |
| `src/orbit/platform/process.py` | 补 drain 线程泄漏计数；`kill_grace` 可配置；终止原因写入 `ProcessResult`；Windows 路径测试 |
| `src/orbit/workflow/worker/supervisor.py` | deadline/续期失败转换为显式终止信号；失败当下写日志/内存指标，恢复后生成聚合摘要；暴露异常退出信号 |
| `src/orbit/workflow/worker/runtime.py` | Worker 控制线程与 Handler 执行线程分离；并发监控 Handler/Supervisor/deadline；强制结算、单次 settlement gate、迟到结果处理；泄漏线程的 slot 占用与 degraded 状态（§4.4.3） |
| `src/orbit/web/app.py` | 暴露 Worker 池的 degraded 状态与泄漏 slot 计数，供 UI 与指标读取 |
| `src/orbit/workflow/application/durable_runtime_service.py` | §3.1 的时间计算与安全余量校验；接入 `config.timeout_seconds` |
| `src/orbit/workflow/catalogs/agent_discovery.py` | `AgentSpec` 增加 `writes_workspace`（§4.6.1 方案 A）；若采用 §3.2.2 方案 A，改 `result_schema_id` |
| `src/orbit/workspace/git.py` | 执行前/后只读快照；隔离 worktree 创建、保留与安全归档；结果交付（临时分支 + patch Artifact） |
| `src/orbit/workflow/authoring/generator.py` | 单步范围、主动收尾、全量测试拆分规则 |
| `src/orbit/static/workflow-ui/assets/app.js` | 终止、unknown 原因、时间信息、工作区快照、结果交付入口与重跑确认 |
| `src/orbit/workflow/runtime/durable_kernel.py` | **仅在决定实施 §4.5.2 时**：`_durable_start_job` 创建 NODE_TIMEOUT timer；complete/fail 时取消；fire 分支按 `execution_safety` 分流 |

相关领域对象需要新增稳定的停止原因字段，但应保持旧事件可回放；新增事件或字段必须提供 upcaster 或兼容默认值。`StartJob` payload 的新字段按 §4.5.2 第 3 条处理：缺失即退回旧行为。

## 8. 分阶段实施

### 阶段一：补齐证据

- 把 `LeaseSupervisor` 已有计数接入结构化日志和内存指标；数据库恢复或结算时 best-effort 写一条聚合摘要事件，并补齐 §4.7 的终止与结算结果。
- 在 Attempt 详情显示最后输出及最后续期时间。
- 为当前 unknown 场景增加可复现故障测试。

验收：发生同类问题时，可以明确区分 deadline、数据库续期失败、进程清理失败和 Worker 崩溃。

### 阶段二：把 Agent 路径接进进程组管理

§4.1 已经说明：进程树能力在 `platform/process.py` 里齐备，但 Agent CLI 走的是自己的裸 `Popen`，一项都没用上。这是 §1 故障的直接成因，也是本方案技术上最关键的一步。

- 把 `TrustedCliAgentClient._run` / `.cancel` / `._communicate_bounded` 迁移到 `ProcessHandle`。
- adapter 超时改为从 `ExecutorRequest.deadline` 派生（否则阶段三接通 `config.timeout_seconds` 后会不一致）。
- 修 `ProcessHandle.wait()` 的 drain 线程泄漏——否则迁移只是把泄漏换个位置。
- `kill_grace` 改为可配置，agent 节点取更长值，并同步进 `cleanup_reserve`。
- 终止原因写入 `ProcessResult`。
- 按 §9 的进程测试矩阵验证，重点是逃逸子进程与 Windows 路径。

验收：Agent 启动一个忽略 TERM 的子进程后，取消能在固定时间内清理整个进程树；孙进程持有 stdout 时 drain 线程不再无声泄漏，而是被计数上报。

### 阶段三：时间模型与安全校验

- 实现 §3.1 的两轴公式与 §3.1.3 的启动期校验。
- 接入 `config.timeout_seconds`（§5.1），统一 Schema/UI/服务端最小值并从总预算预留清理时间。
- 不通过校验的配置拒绝启动，不静默截断。

验收：任意合法 `max_duration_seconds`（60 / 300 / 900 / 1800 / 边界值）都得到单调递增的时间点，`settlement_deadline <= node_deadline`，且不越过租约不等式。

### 阶段四：强制结算

**前置条件：阶段二必须已完成。** 见 §4.4.3——Handler 执行线程不退出的原因就是卡在 `process.wait()` 或 drain 上，阶段二不做，本阶段会稳定泄漏线程而不是偶发。

- 将同步 Handler 调用改为"Worker 控制线程 + Handler 执行线程"（§4.4.2），由控制线程并发等待 Handler 结果、Supervisor 异常信号和 `settlement_deadline`。
- supervisor 到 Worker 的终止信号；settlement gate；强制结算。
- Supervisor 异常退出时由控制线程补发取消并继续收敛 Attempt。
- 实现 §4.4.3 的 slot 占用与 degraded 状态：泄漏线程不归还 slot，可用 slot 降到 0 时显式告警而非静默停摆。
- 按 `execution_safety` 实现 §4.3 的映射。
- 拒绝并审计迟到结果。

验收：达到截止时间后，Run 在结算余量内进入明确状态；`LeaseSupervisor` 线程异常退出时，Worker 控制线程仍能自行收敛该 Attempt；泄漏的 Handler 线程占用 slot 可观测，池耗尽时进入可见的 degraded 状态。

### 阶段四·可选：接通 NODE_TIMEOUT

独立决策项，不阻塞阶段五。实施细节见 §4.5.2，成本对比见 §4.5.1。

注意 §4.4.2 落地后，它的独有覆盖面已收窄为**Worker 控制线程自身死亡**——supervisor 死亡那一格已由控制线程覆盖。建议与 Timer Dispatcher 独立进程化一并评估，那时"进程外兜底"才名副其实。

验收：Worker 控制线程死亡时由 NODE_TIMEOUT 按时兜底；整个 Orbit 进程被强杀后，服务重启与恢复扫描能补发到期 Timer 并收敛 Run 状态；正常路径下 timer 因 `TIMER_GRACE` 而不触发，`node_timeout_timer_fired` 计数保持为零。

### 阶段五：工作区一致性

- 定 §4.6.1 的隔离判据（推荐 `AgentSpec.writes_workspace`）。
- 定 §4.6.2 的工作目录语义（从 `HEAD` 还是从当前工作区状态创建）。
- Attempt 执行前及强制结算时采集工作区快照。
- 会修改代码的 Agent Attempt 使用隔离 worktree（依赖阶段二的 `cwd` 传参能力）。
- 实现 §4.6.3 的结果交付：临时分支 + patch Artifact。
- 重跑对话框展示 diff 摘要与"继续原 worktree / 从基线创建新 worktree"选择。

验收：超时后人工重跑时，用户在点击前就知道工作区处于什么状态；成功完成的 Attempt 其改动有明确的、不触碰用户当前工作区的交付路径。

### 阶段六：主动收尾与生成约束

- 注入静态 Runtime 收尾约束。
- 决定并实施 §3.2.2 的 A 或 B 方案。
- 生成器默认拆分全量测试。
- 引入结构化 partial/blocked 结果。

验收：评测集中超时比例下降，且 Agent 在定向测试失败或预算不足时能够返回部分结果。

### 阶段七：UI 与运行指标

- 增加终止状态（"正在收尾"仅在软截止落地后）。
- Run 级预算与指标（§3.4）。
- 统计正常返回率、强制终止率、unknown 比率和清理耗时。

验收：用户不再把 unknown/waiting 误认为仍在执行。

## 9. 测试方案

### 单元测试

- §3.1 的时间公式在 `D = 60 / 300 / 600 / 900 / 1800 / 86400` 下均单调递增且非负，且 `settlement_deadline <= node_deadline`。
- `D < MIN_AGENT_DURATION_SECONDS`、`D <= cleanup_reserve` 以及 UI/Schema/服务端最小值不一致时均被拒绝。
- §3.1.2 的租约不等式校验能拒绝越界配置。
- 取消原因 × `execution_safety` 到 Attempt 状态的完整映射矩阵。
- 重复取消和重复结算幂等。
- 续期连续失败触发强制结算。
- Runtime DB 不可写时，续期失败仍能写入结构化日志和内存指标；数据库恢复后只追加一条聚合摘要事件。
- 迟到结果被 fencing token 拒绝。
- adapter 超时从 `ExecutorRequest.deadline` 派生，且不超过构造参数上限。
- `LeaseSupervisor` 线程异常退出后，Worker 控制线程在有界时间内检测到并强制结算。
- 泄漏的 Handler 线程占用 slot 不归还；可用 slot 降到 0 时 Runtime 进入 degraded 状态并告警，而不是静默停止拉取 job。
- 泄漏线程最终退出时归还 slot，Runtime 自动离开 degraded 状态。
- （仅阶段四·可选）`StartJob` payload 缺 `settlement_deadline` 时不建 timer，行为与旧版本一致。
- （仅阶段四·可选）timer 的 `due_at` 为 `settlement_deadline + TIMER_GRACE`；Worker 在余量内正常结算时 timer 触发计数为零。

### 进程测试

整个矩阵必须**同时覆盖 dev tools 路径与 Agent 路径**。这两条路径今天走的是不同的进程实现（§4.1），只测前者会给出虚假的绿灯——`platform/process.py` 的测试全部通过，而 Agent 仍在裸 `Popen` 上运行。阶段二迁移完成后，两条路径共用同一实现，届时这条要求自动满足。

- CLI 正常返回。
- CLI 卡死。
- CLI 忽略 `SIGTERM`。
- CLI 启动一个忽略信号的子进程。
- 子进程继承 stdout/stderr 并保持管道打开（**§1 故障的直接复现用例**，必须在 Agent 路径上通过）。
- 输出超过限制。
- 超时与用户取消同时发生。
- drain 线程超过 join 超时后被计数上报。

每个测试都必须验证进程树已清理、文件描述符已关闭，并且没有 `ResourceWarning`。

### Runtime 集成测试

- 正常结果在 deadline 前完成并释放租约。
- deadline 后在 settlement 余量内写入明确状态。
- `REPLAY_SAFE` handler 超时后自动重试；`UNKNOWN_ON_LEASE_LOSS` handler 超时后不自动重试。
- 租约续期失败时终止 Handler 并强制结算。
- `LeaseSupervisor` 线程异常退出后，Worker 控制线程仍使 Run 进入明确状态。
- Handler 执行线程超过 `settlement_deadline` 后迟到返回不会二次结算，且遗留线程占用的 Worker 容量不会被重复分配。
- （仅阶段四·可选）Worker **控制线程**死亡时，NODE_TIMEOUT timer 到期使 Run 进入明确状态。
- （仅阶段四·可选）Orbit 进程被强杀后，重启恢复扫描会补发已经到期的 NODE_TIMEOUT timer。
- Worker 在 Handler 已产生副作用后重启，恢复为 unknown 且不自动重试。
- late result 不覆盖 unknown。
- 用户选择重跑后生成新 Attempt，旧 Attempt 保持不可变。
- 执行前和强制结算后的工作区快照均可读回，隔离 worktree 的修改不会污染用户共享工作区。
- 成功完成的隔离 Attempt 其改动可通过临时分支或 patch Artifact 取回，且用户当前工作区未被写入。

### Workflow 生成测试

- 编码与全量测试被拆成独立节点。
- 自修复循环必须有明确最大次数。
- Agent 步骤包含主动收尾约束。
- 部分结果存在可达的失败、修复或人工处理路径。

## 10. 验收指标

上线后按 Agent、版本和 Workflow 统计：

- Agent 正常返回率。
- 主动 partial/blocked 返回率。
- deadline 强制终止率，按 `execution_safety` 分组。
- `unknown_external_result` 比率。
- 从触发取消到完成结算的 p50/p95。
- 进程树清理失败次数、drain 线程泄漏次数。
- 当前泄漏的 Handler 线程数、被占用的 Worker slot 数、处于 degraded 状态的时长占比（§4.4.3）。
- lease renewal failure 次数（进程指标）与 `lease_renewal_failure_summary` 事件数（失败区间数），两者含义不同，分别统计。
- NODE_TIMEOUT timer 触发次数。因为 `due_at` 留了 `TIMER_GRACE`，这个计数是干净的"Worker 侧结算未能生效"，正常竞争不会计入。
- 因超时而人工重跑后产生重复修改的事件数。
- Run 级：总时长、总成本、并行 agent 节点峰值。

最低验收标准：

1. 达到 `process_deadline` 后完成 Attempt 结算。**60 秒是外部验收上限**；§3.1.1 的公式给出的内部目标是 `cleanup_reserve`（当前 `kill_grace + SETTLEMENT_MARGIN = 32s`，agent 的 `kill_grace` 提到 10s 后为 40s），两者都必须满足，且结算不晚于 `node_deadline`。
2. Agent CLI 走 `ProcessHandle`：进程组清理测试无残留子进程、无未关闭管道警告、无静默泄漏的 drain 线程。孙进程持有 stdout 的场景必须通过。
3. 租约停止续期不再是状态变化的唯一机制；`LeaseSupervisor` 线程异常退出时由 Worker 控制线程兜底；若实施了阶段四·可选，控制线程也失效时由 NODE_TIMEOUT 兜底，Orbit 进程退出时由重启恢复扫描补发。
4. 可能有副作用的 unknown 执行绝不自动重试；`REPLAY_SAFE` 执行的超时仍走正常 retry policy。
5. UI 能明确显示"结果未知"及其工作区状态，而不是仅显示"等待中"。
6. 现有 Runtime、Workflow 和浏览器测试全部通过。

## 11. 推荐实施顺序

**阶段二优先级最高**，高于原本排在它前面的证据补齐。两个理由：

1. Agent CLI 走裸 `Popen`、取消只杀主进程，是 §1 故障的直接成因，也是唯一一处"改了立刻减少故障"的地方。
2. **它是阶段四的前置条件**。§4.4.3 说明：Handler 执行线程不退出的原因就是卡在 `process.wait()` 或 drain 上。阶段二不做就上阶段四，双线程结构会在每次 agent 超时后稳定泄漏一个线程，很快把 Worker 池耗尽——把一个偶发故障换成一个必然故障。

阶段一的证据补齐可以与阶段二并行。

阶段三（时间模型）必须先于阶段四：强制结算要在租约到期前完成，而这条余量正是 §3.1.2 的不等式；没有它，Worker 结算与 Reaper 回收会竞争同一个 Attempt 的终态。阶段三同时是阶段二 adapter 超时改造的消费者。

阶段四·可选（NODE_TIMEOUT）单独决策，不阻塞阶段五。§4.4.2 落地后它的独有覆盖面已收窄为 Worker 控制线程自身死亡——控制线程只做等待与结算、不执行 Handler 代码，这比 supervisor 死亡更罕见一档。把 kernel 改动留到 Timer Dispatcher 独立进程化时一并做，收益更大。

阶段五（工作区一致性）紧随阶段四：它是 §1 背景问题的另一半，只做结算不做工作区，用户仍然不敢重跑。注意它有三个前置决策（隔离判据、工作目录语义、结果交付方式），应在进入实施前定下来，否则会边写边改。

阶段六的提示词与 Workflow 拆分用于降低发生率，不能替代 Runtime 兜底。阶段七最后完成展示和指标闭环。
