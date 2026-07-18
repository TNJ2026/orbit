# 备份、恢复与升级

备份必须同时覆盖 SQLite 主库、WAL/SHM 一致性快照和 Artifact CAS。推荐暂停新 Claim，执行 WAL checkpoint，再复制数据库与 CAS manifest。恢复后先运行 `PRAGMA integrity_check`、外键检查、Event sequence、Snapshot checksum、Budget Ledger、PlanPatch/PlanVersion、Foreach count 和 Blob integrity。

Migration 只能顺序前进。v1–v6 不修改；v7 引入 Plan/Human/Budget，v8 增量扩展 Human 并加入 Foreach/Subflow，v9 仅包含 Capability/ACL/Audit/API Receipt。跨版本长 Run 依赖 Event Catalog/Upcaster，禁止删除旧 Event 类型。

回滚代码前必须确认旧代码能识别当前 Schema/Event；否则恢复升级前备份，而不是降级数据库。

