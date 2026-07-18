# Step 5 Durable Execution 容量基线

| 属性 | 值 |
| --- | --- |
| 日期 | 2026-07-17 |
| 场景 | 单机、单项目、本地 Runtime |
| 数据量 | 10,000 ready Jobs + 10,000 due Timers |
| 扫描页大小 | 100 |
| 自动化测试 | `tests/test_workflow_durable_capacity.py` |

基线测试构造 10,000 个 Job 和 10,000 个 Timer，然后分别执行有界 Claim/Due Scan。当前开发机测试主体耗时约 0.09 秒，完整测试进程约 0.17 秒；返回结果严格限制为 100 条，没有物化 Event Timeline。

此数字只用于发现复杂度退化，不是生产 SLA。SQLite 并发安全由 Expected Version、CAS、Partial Unique Index 和 Fencing 保证；Memory Adapter 基线用于验证排序和分页算法不会退化为每次插入 O(N) 的唯一性扫描。

后续重新评估条件：

- 单项目 ready Job 或 due Timer 稳态超过 10,000。
- Lease History 超过 100,000。
- Claim/Due Scan p95 超过 Worker Poll Interval 的 25%。
- 从单机 SQLite 扩展为跨主机 Worker。
