# Workflow Runtime 运维手册 1.0

适用范围：单机、单项目 SQLite Runtime。启动前先备份数据库和 Artifact CAS；启动后先运行只读完整性检查，再运行 Recovery dry-run，确认报告后才允许 Apply。

日常检查：未终结 Run 数、ready Job、过期 Lease、due Timer、Unknown Attempt、等待 HumanTask、Budget remaining、Artifact integrity。Timeline 必须按 Global Position 分页，禁止一次加载完整历史。

故障处置优先级：磁盘只读或已满时立即停止 Worker；数据库损坏时切换到备份副本验证；Provider/外部结果未知时创建人工接管任务，不自动重试；Budget 超支先保留实际账单，再按策略等待追加或终止；Secret 泄漏测试失败属于发布阻断。

所有 Repair 默认 dry-run。Apply 必须使用 `system:repair` Actor、稳定 Idempotency Key 和 Expected Version，通过已注册 Command submitter 执行，禁止直接编辑 projection。

