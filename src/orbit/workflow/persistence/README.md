# Workflow Persistence 1.0

This package is the stable persistence boundary for the deterministic runtime.

## Transaction invariants

- SQLite writes only occur inside `SQLiteUnitOfWork` and require explicit `commit()`.
- A command's projection, events, branch tokens, and receipt share one transaction.
- Repositories never create business events or advance a state machine.
- Projection `aggregate_version` must equal the corresponding event-stream head.
- `run_events` and `execution_plans` are immutable; snapshots are append-only and
  may be explicitly deleted because they are caches.
- Snapshot generation is a separate post-business-commit transaction by default.
- `SnapshotPolicy.should_snapshot` is a level-triggered advisory decision: waiting
  and terminal states keep returning `True`. The Step 4 Kernel must deduplicate
  using the last snapshot cursor/status transition and must not write a snapshot
  merely because the same unchanged state is evaluated again.
- `get` returns `None` for a missing record; duplicate creation raises
  `RepositoryAlreadyExistsError`; stale updates raise `ConcurrencyConflictError`.
- Raw events are never rewritten. Upcasting only occurs in a sealed read pipeline.
- The Memory adapters implement the same structural ports for contract tests;
  SQLite remains the durable production adapter.
- `node_attempts` intentionally has no duplicate `run_id`; ownership is normalized
  through `node_runs`. Run-scoped Attempt reads and deletion must join through
  `node_runs`. Changing this requires a new reviewed migration, not an edit to v2.
- `check_database` currently materializes the scoped Event rows and Event-ID/
  causation maps in memory, so its peak memory is O(events). It is an offline
  operations tool, not a request-path API. A larger production retention window
  must replace this with cursor-based streaming and bounded receipt lookups.

The stable error-code mapping is `PERSISTENCE_ERROR_REGISTRY`. Driver exceptions
must be translated at an adapter boundary before they reach the Runtime Kernel.
