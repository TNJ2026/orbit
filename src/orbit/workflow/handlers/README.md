# Handler Runtime 1.0

Handler Runtime 是可信第一方节点实现与 Durable Kernel 之间的执行边界。组合根通过 `HandlerRuntimeBuilder` 注册精确的 `HandlerManifest` 和实现，完成 Secret、Schema、CLI preflight 后 Seal Registry；运行中的 Workflow IR/ExecutionPlan 必须携带匹配的逐 Handler Manifest 指纹。

生产接入顺序：

1. 为 Handler 发布新的精确 SemVer 和 Manifest，不允许同版本静默替换契约。
2. 在编译 Catalog 与 Execution Registry 注册同一 Manifest；Tool 还必须注册精确的 ToolManifest。
3. 只把受信实现、Secret 映射和 Agent CLI 命令放入组合根，不能从 DSL 或 Workflow 输入动态加载模块、URL、Shell 或凭据。
4. 启动 Worker 前调用 Builder.build；预检失败时停止启动，不降级到其他版本。
5. Handler 只使用 HandlerContext 能力；不得导入 Runtime Repository、Kernel 或 Application Service。

Step 12 前这里不是恶意代码沙箱。Capability/Secret 声明用于 fail-closed 注册和审计；ArtifactWriter 在 Step 7 前拒绝写入，流式 Usage 在 Step 10 前只保存在内存，最终 Usage 由 Durable 结果事务写入 Event。
