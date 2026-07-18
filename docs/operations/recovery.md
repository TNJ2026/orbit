# Recovery 与人工接管

Recovery Manager 以 `run_id` cursor 分页扫描并受 deadline 限制。它检查过期 Lease/Timer、缺失责任、Unknown Attempt/Planner、Human deadline、Foreach 和 Subflow；Replay 只读取 Snapshot/Event，不调用 Handler、Planner 或外部系统。

安全可证明的项目可在 Apply 模式提交幂等系统 Command。无法证明外部结果、孤立 Subflow、损坏 Artifact 或冲突 PlanVersion 必须转为 scope 明确的 HumanTask。人工处置只能创建新 Attempt、补偿、取消或合法终止，不能覆盖既有外部事实。

恢复验收：重复扫描不重复推进；迟到结果受 Fence/终态约束；每项 Apply 都能从 Audit/Event 找到 Actor、原因和目标版本。

