# Deterministic Runtime Kernel 1.0

`RuntimeKernel.handle()` is the only write entry point for the new workflow
runtime. It consumes a frozen `CommandEnvelope`, checks a durable receipt before
optimistic concurrency, applies primary and kernel-owned secondary aggregate
reactions in one Unit of Work, and returns an immutable `CommandResult`.

Supported commands are `start_run`, `schedule_node` (system-only),
`start_attempt`, `complete_attempt`, `fail_attempt`, and `cancel_run`.

`DurableRuntimeKernel` extends this entry point with Job/Lease/Timer commands.
The production durable composition is `DurableRuntimeApplicationService` plus
`WorkerRuntime`/`TimerDispatcher`; scheduling a NodeRun atomically creates its
ready Job. Candidate scans are read-only. Claim, Start, Result, Expiry, Cancel,
Timer Fire, and recovery repair all submit Commands. Lease renewal is the only
operational CAS write that does not create an Event, and it cannot change a
business status.

`testing_driver.py` remains a Step 4 conformance harness, not a production
worker. Step 5 uses a Fake Executor through the Worker boundary; real Handler,
Tool, Agent, network, process, Artifact, and Usage behavior remains Step 6/7.

Lease authority requires a hashed bearer token, monotonically increasing fence,
current Attempt binding, and unexpired active Lease. Start-before-loss is safely
reclaimable; loss after execution starts is replayed only for explicitly
`replay_safe` work, otherwise the Attempt becomes `unknown_external_result`.

Snapshot creation occurs after the business commit. The coordinator compares the
latest compatible snapshot cursor before writing, because waiting and terminal
states make `SnapshotPolicy.should_snapshot()` remain true.
