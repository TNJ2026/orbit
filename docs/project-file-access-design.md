# Workflow 项目文件访问设计

状态：**已实现**。

本文描述 `orbit serve --agent-project-access` 的当前生产语义。目标是让一个 Workflow Run 的 Agent、App 委托和 Harness 委托看到同一份完整项目文件，同时避免同一 Run 内或不同 Run 之间并发写同一目录。

## 1. 唯一的公开策略

Workflow 只需声明 `workspace_access`，推荐使用空配置：

```yaml
policies:
  - id: project
    kind: workspace_access
    config: {}
```

`isolation`、`files`、`protect` 等旧字段暂时仍可读取，以便已发布定义通过结构校验，但不再决定生产运行方式。`mode: read_only` 仍保持只读意图：Git 使用不回流的 worktree；非 Git direct 因无法对任意 Agent CLI 强制只读而拒绝。项目类型由 Runtime 检测，只使用一个授权开关：

```shell
orbit serve --workspace-path /absolute/project --agent-project-access
```

没有该开关时，带 `workspace_access` 的 Workflow 可以发布，但启动或恢复会明确失败，不得降级到空 scratch 或 App 当前 cwd。

## 2. Runtime 选择规则

### Git 项目

- 必须是可用的 Git 仓库且至少有一个 commit。
- 源 checkout 必须干净，包括没有未提交和未跟踪文件。否则 worktree 从 `HEAD` 创建后并不包含“当前完整项目”，Runtime 会在首次获取时拒绝并提示 commit/stash。
- 每个 Run 创建一个独立 Git worktree；key 是 `run_id`，不是 `node_id`。
- 同一 Run 的所有 `agent.*`、`app.delegate`、`harness.subagent` 节点共享该路径，因而前一步写入的文件对后一步可见。
- 不自动把 Run worktree 的改动合并回源 checkout。worktree 保留到清理周期，集成必须是显式动作；Orbit 不能在用户未授权时改写源分支。

### 非 Git 项目

- 不复制项目，也不使用文件 allowlist；所有相关节点直接以真实项目根目录为 cwd。
- Agent 获得完整读写能力。Orbit 不声称能把任意外部 Agent CLI 限制为只读。
- 不提供自动回滚；运行记录明确标记 `unprotected_direct`，并说明整个项目都没有自动恢复覆盖。大目录不会因创建恢复副本而耗尽磁盘。
- 项目占用使用跨进程文件锁和持久化 claim。同一项目一次只允许一个 Run；Runtime 崩溃留下的 claim 必须在确认 Agent 已停止后显式恢复或解除。

## 3. 串行与图约束

项目访问是 Run 级属性。只要任一节点引用 `workspace_access`，该 Run 的所有 Agent、App 和 Harness 节点都使用同一个 Runtime 选定路径。

因此语义编译器拒绝能够同时到达多个上述节点的 `route_mode: parallel` 扇出。普通纯工具节点仍可并行；限制的是会共享可写项目 workspace 的 Agent 分支。已有 `dev_tool` / `dev_tool_write` 使用自己的独立 worktree，不能与此模式混用，也在发布时拒绝。

## 4. App 与 Harness 委托协议

Runtime 在委托请求中携带：

```json
{
  "workspace": {
    "kind": "git_worktree | direct",
    "path": "/absolute/runtime-selected/path",
    "project_root": "/absolute/original/project",
    "access": "read_write",
    "run_id": "langgraph_run:..."
  }
}
```

宿主必须把 `workspace.path` 设为该委托步骤的 cwd。它不能替换为聊天会话 cwd；无法选择该目录时必须让委托明确失败。跨会话身份接管不在本设计范围内。

## 5. 能力与兼容边界

- Git grant 使用部署能力 `workspace.read`；非 Git direct 使用 `workspace.project.read` 与 `workspace.project.write`。
- 部署能力只进入 `HandlerRegistration.granted_capabilities`，不得写入 `HandlerManifest`，否则会改变 manifest fingerprint 并破坏已发布版本绑定。
- `FileAllowlistGrant`、`workspace.read.files`、旧读写开关及其配额参数已经删除。CLI 和 `create_app()` 只接受 `agent_project_access`。
- Hub 授权文件中的旧布尔 `true` 或 `legacy_read` 不再视为有效授权；只有重新执行 `hub register --agent-project-access` 才写入 `read_write`。因此旧授权不会在升级后静默变成非 Git direct-write。

## 6. 生命周期与失败原则

1. 编译 Workflow，确定是否需要 Run 级项目 workspace，并校验并行图冲突。
2. 启动 Run 时校验绑定 Handler 的部署能力。
3. 非 Git direct 在执行前取得项目 claim；Git worktree 在首个相关节点准备时按 Run 获取。
4. 节点执行或 App/Harness claim 始终使用同一路径。
5. Run 进入 `completed`、`failed` 或 `cancelled` 后结算并释放 direct claim；`unknown` 不释放，因为外部 Agent 可能仍在写。
6. 结算、摘要或 Git 观察失败必须记录为 unavailable，但不能让 Run 永久卡住或永久持锁。

安全边界：Git worktree 隔离源 checkout，但不是 OS 沙箱；非 Git direct 会真实改写项目且无通用撤销。两种模式都不约束 Agent 访问项目目录之外的位置，实际限制仍取决于宿主和 Agent CLI 自身的 sandbox。
