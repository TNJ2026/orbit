# 单机容量与 SLO 1.0

目标环境：macOS arm64、10 logical CPUs、Python 3.11、SQLite WAL；单项目 10,000 Runs、1,000,000 Events、10,000 ready Jobs、10,000 due Timers。该目标是本地产品 Gate，不是分布式 SLA。

| Workload | Gate | 2026-07-17 基线 |
| --- | ---: | ---: |
| 单 Event append P95/P99 | ≤ 2/5 ms | 0.311/1.335 ms |
| 100 Event batch throughput | ≥ 20k events/s | 30,893 events/s |
| 100 Event snapshot tail replay | ≤ 10 ms | 2.758 ms |
| 1M Event integrity scan | ≤ 10 s | 3.971 s |
| 10k Job/Timer bounded page scan | ≤ 250 ms | 约 90 ms |
| 全量自动测试 | 无失败 | 557 tests / 12.10 s（2026-07-18；仅代表自动测试 Gate，不代表发布 Gate） |

长 Timeline 必须分页，Snapshot 默认每 100 Events 或 waiting/终态生成。性能优化不得改变 Event/Receipt/Plan hash、确定性顺序或 Handler 静态成本上限。动态 Reservation Estimator 仍为 Draft：只能收紧估算，不能放宽 Resource Profile hard upper bound。

复现基线：运行 `scripts/benchmark_workflow_persistence.py`、`tests/test_workflow_durable_capacity.py` 和完整 unittest。报告必须包含 P50/P95/P99、吞吐、规模、硬件与 pass/fail。
