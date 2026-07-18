# M1B 行为清单：Process 与 Workspace

> 阶段：M1B（抽离 Process 与 Workspace 能力）
> 上级方案：[workflow-runtime-clean-migration-plan.md](../workflow-runtime-clean-migration-plan.md)
> 方案任务 1 明文要求："先建立行为清单，不直接搬运闭包或 Store 调用"。

本文是 `platform/process.py` 与 `workspace/git.py` 的行为契约来源。左列是旧实现里被观察到的行为，右列是新模块必须保证的语义——**不是**要求逐行移植。

## 1. 进程能力

### 1.1 进程组隔离

| 旧行为 | `server.py` | 新契约 |
|---|---|---|
| 子进程放进独立进程组 | `_detached_process_kwargs()`：POSIX `start_new_session=True`，Windows `CREATE_NEW_PROCESS_GROUP` | `ProcessHandle.spawn()` 始终隔离进程组，调用方无法关闭 |

理由：不隔离时终止会误杀 orbit 自身进程组。

### 1.2 父子关系快照（终止前）

| 旧行为 | `server.py` | 新契约 |
|---|---|---|
| 四路降级探测 pid→ppid | `_snapshot_ppids_{windows,ps,libproc,procfs}` 按序尝试 | 保留四路降级；任一可用即返回，全失败返回空表 |
| 后代 pid 遍历 | `_descendant_pids()` 从快照建 children 图后 DFS | 同语义；**必须在终止前快照** |

理由（旧代码注释已记录）：父进程一死，子进程 reparent 到 init，树链丢失。先快照才能补杀逃逸的 `setsid` 子进程。

### 1.3 终止

| 旧行为 | `server.py` | 新契约 |
|---|---|---|
| 优雅终止 | `_terminate_pid_tree()`：POSIX `killpg(SIGTERM)`；Windows `taskkill /T /F` | `terminate()`；Windows 无优雅组信号，强制即优雅 |
| 强制终止 | `_kill_process_group()`：先快照后代 → `killpg(SIGKILL)` → 逐个补杀 → Windows `taskkill /F /T` + `proc.kill()` 兜底 | `kill()` 同序；异常全部吞掉（进程可能已退出） |

### 1.4 流式输出

| 旧行为 | `server.py` | 新契约 |
|---|---|---|
| 逐块读取不等进程结束 | `_stream_process_output()`：`os.read(fd, 4096)` 循环 | 同语义，块大小可配 |
| 读端被关闭时停止 | 捕获 `OSError`/`ValueError` 后 break | 保留：kill 路径会关闭读端来解除阻塞 |
| 解码容错 | `errors="replace"` | 同 |
| 逃逸子进程持有管道 | 旧实现靠 kill 时关读端解除 wedge（`test_runner_not_wedged_by_child_holding_pipe`） | **必须保留**：这是真实事故场景 |

新增（旧实现没有，方案任务 2 要求）：

- **输出上限**：超限截断并标记 `truncated`，防止输出炸弹。
- **脱敏接口**：写日志/回传前过一遍 redactor。

### 1.5 旧实现中不迁移的部分

| 旧行为 | 不迁移的原因 |
|---|---|
| `_append_run_file(run, ...)` 把输出写进 `task_runs.log_dir` | 方案 §4.2：Runner 日志改写 Artifact |
| `_parse_run_tokens()` 从 stdout 抠 token 数 | 方案 §4.2：用量走 UsageMeter/Budget Ledger，不靠正则解析 |
| `_read_run_output_tail()` / `_run_last_output_at()` 扫日志目录 | 卡死检测改用 Runtime 事实（lease/heartbeat），不扫文件 |

## 2. Workspace（Git worktree）能力

### 2.1 标识

| 旧行为 | `server.py` | 新契约 |
|---|---|---|
| 分支名 | `_worktree_branch(task_id)` → `orbit/task-{int}` | `workspace_ref`（不透明字符串）→ `orbit/ws-{slug}`；**不接受整数 task_id** |
| 目录 | `_task_worktree_dir()` → `.orbit/worktrees/task-{int}` | `.orbit/worktrees/{slug}` |

方案任务 3 明文：使用 `run_id/node_run_id` 或独立 `workspace_ref`，不接受旧 task_id。

### 2.2 供给

| 旧行为 | `server.py` | 新契约 |
|---|---|---|
| 幂等 acquire | `_ensure_task_worktree()`：已存在则重挂，不重建 | 保留——引擎会在租约过期后重跑同一步 |
| 陈旧注册清理 | 先 `prune` 被 SIGKILL 留下的注册 | 保留 |
| 无 git / 无提交时降级 | 返回 `None`，调用方回落 project_root | 保留，但改为显式结果对象而非 `None` |
| 基点 | `_worktree_base_ref()`：`HEAD`；unborn HEAD 返回 `None` | 保留 |
| 并发不同 task 同时 add | 旧实现允许 | 保留并加测试 |

### 2.3 卫生

| 旧行为 | `server.py` | 新契约 |
|---|---|---|
| state 目录写进 gitignore | `_ensure_state_dir_gitignored()`：worktree/日志不进仓库 | 保留 |
| 回收 | `_remove_task_worktree()` / `_sweep_task_worktrees(store, ...)` | 保留 remove；sweep **去掉 Store 参数**，改为按存活 workspace_ref 集合清理 |

### 2.4 新增（方案任务 5）

| 能力 | 原因 |
|---|---|
| 路径穿越拒绝 | `workspace_ref` 来自上游，必须校验解析后仍在 worktrees 根内 |
| symlink 逃逸拒绝 | 同上 |
| 脏工作区处理 | 集成前需明确语义 |
| 重复 acquire/release | 幂等性必须有测试而非靠注释 |
| 崩溃清理 | SIGKILL 后的陈旧注册 |

## 3. 依赖方向

```text
workflow/handlers ──uses──▶ platform/process.py
workflow/environments ──uses──▶ workspace/git.py
                                      │
                          两者都不 import engine / store / workflow domain
```

Gate M1B 要求：Process/Workspace 模块不 import 旧 Engine 或 Store。

## 4. 测试归属

`tests/test_worktree.py` 80 个测试里，真正属于本阶段的是 **22 个**（M0 基线 §5.2 已纠正方案的按文件假设）：

| 类 | 数量 | 迁移目标 |
|---|---:|---|
| `WorktreeLifecycleTests` | 5 | `workspace/git.py` |
| `GitProvisioningTests` | 10 | `workspace/git.py` |
| `WorktreeSweepTests` | 1 | `workspace/git.py` |
| `ProcessControlPortabilityTests` | 4 | `platform/process.py` |
| `DescendantPidSnapshotTests` | 2 | `platform/process.py` |

另有 `test_workflow_engine.py::AutoRunnerTests` 的 19 个进程执行/取消/流式场景，其中进程相关部分同属本阶段。
