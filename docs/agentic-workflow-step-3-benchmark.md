# Agentic Workflow Step 3 容量基线

| 文档属性 | 值 |
| --- | --- |
| 版本 | 1.0 |
| 日期 | 2026-07-17 |
| 性质 | 开发机可重复基线，不是生产 SLA |
| Harness | `scripts/benchmark_workflow_persistence.py` |

## 环境与规模

- macOS 27.0 arm64，10 logical CPUs。
- Python 3.11.15，SQLite 3.53.1。
- 10,000 Runs，1,000,000 主 Run Events，另有 100 个单 Event 样本。
- Event Append 批量大小为 100；WAL 在报告采样前已 checkpoint。

## 结果

| 指标 | 结果 |
| --- | ---: |
| 单 Event Append P50 / P95 / P99 | 0.217 / 0.311 / 1.335 ms |
| 100 Event Batch P50 / P95 / P99 | 2.908 / 3.115 / 5.929 ms |
| Batch 吞吐 | 30,893 events/s |
| 100 / 1,000 / 10,000 Event Replay | 0.866 / 6.701 / 87.435 ms |
| 1M Run Global Stream，1,000/页 | 9,351.255 ms |
| 10k Run / 1M Event Integrity Scan | 3,971.410 ms |
| Snapshot Tail 5,000 / 1,000 / 100 Event 恢复 | 120.384 / 24.011 / 2.758 ms |
| Snapshot 写入 | 0.893–2.472 ms |
| SQLite 主文件 | 266,788,864 bytes |

## 决策

1. 保留 `run_events_by_run_position` 和 `run_events_by_aggregate`；当前查询计划在目标规模不需要新增索引。
2. `SnapshotPolicy.every_n_events` 默认值固定为 100，同时在 Run 进入 waiting、waiting_for_budget 或终态时建议生成 Snapshot。100 Event Tail 在本机约 2.8 ms；Kernel 可按部署测量调整 N。
3. Integrity Scan 是运维检查，不进入 Command 热路径；1M Event 完整扫描约 4 秒。
4. RunView 分页 API 继续使用 Global Position Cursor。1M Event 的 Python 对象完整物化约 9.4 秒，后续订阅和 UI 必须流式分页，禁止一次加载全部 Timeline。
5. Snapshot 仍是可丢弃缓存；任何性能优化不得改变 Event-only Replay 的事实来源地位。

当前 Integrity 实现会把检查范围内的 Event、Event ID 和 Causation Map 物化到内存，峰值内存为 O(events)；本轮只记录了时间和磁盘体积，没有记录可信的进程峰值内存。因此 1M Event 数字不能外推为无限容量结论。事件保留规模继续增长时，应改为按 Aggregate/Global Position Cursor 流式扫描，并对 Receipt 引用采用有界批量查询。

复现命令：

```bash
.venv/bin/python scripts/benchmark_workflow_persistence.py --events 1000000 --runs 10000
```
