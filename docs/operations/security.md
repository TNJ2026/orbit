# 安全模型与发布 Gate

信任边界：Kernel、预注册第一方 Handler 和本地 SQLite/CAS 属可信计算基；Planner、Human 输入、第三方 CLI、网络和 Artifact 内容均不可信。默认拒绝未声明 Capability、Artifact ACL、Secret scope、网络、文件路径和外部写。

Capability delegation 只能缩小 permission 与 scope；撤销立即影响新访问。Secret 只通过 Resolver 暂时解析，日志、Prompt、Error、stderr、Planner raw response 和 Artifact metadata 必须递归扫描或脱敏。外部写必须匹配 Run/Node/Capability/request hash 的已完成 Approval Fact。

不可信 CLI 使用 Sandbox Policy：可执行文件 allowlist、固定 root/cwd、最小环境、CPU/进程/输出/时间上限。macOS 通过 `sandbox-exec` 约束文件、网络和 fork，但不能由当前进程可靠执行硬内存限制；请求硬内存隔离时默认拒绝运行。生产接入不可信代码必须使用额外容器或 VM，当前实现不得宣称为完整跨平台 Sandbox。

发布阻断：路径穿越、symlink、网络绕过、输出炸弹、Secret 泄漏、跨 Run/Item/Subflow Artifact 访问或权限扩大测试任一失败。
