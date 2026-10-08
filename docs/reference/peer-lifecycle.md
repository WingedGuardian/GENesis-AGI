# Peer lifecycle state foundation

This module supplies private transactional state. The
[runtime owner](peer-runtime.md) installs the coordinator after restart recovery;
state alone does not enable execution or result disclosure.

`PeerLifecycleState` separates the accepted request from individual execution
attempts. Each attempt reserves the original request's remaining work allowance,
pins a private working directory and deadline, and fences stale callers with an
exact task, segment and generation binding. Unknown elapsed work charges the
whole reservation. Unconfirmed cleanup retains capacity and requires
reconciliation; only confirmed drain permits terminalization or continuation.
The controller must prove both process-scope and broker drain before reporting
clean settlement. The installed coordinator supplies that controller.

An individual, timely dashboard or authorized Telegram-button approval is
consumed atomically with its continuation queue entry. A durable logical consent
then covers the exact unchanged request or operation across attempts, within the
original request expiry and cumulative allowance. Current relationship epoch,
active status, cancellation and admitted/current grants remain mandatory checks.
Conversation consent permits processing and answering that exchange; it does not
authorize separate resource reads. Changed resource versions or arguments require
their own exact operation digest and applicable permission.

`PeerOperationState` distinguishes prepared, executing, completed and unknown
outcomes from consent. An exact immutable read interrupted in an earlier attempt
may retry after that attempt drains and authority is rechecked. Unknown
consequential effects cannot retry automatically. Completed receipts also require
current authorization before reuse. Operation receipt storage is separate from
full-result [owned artifact storage](peer-results.md).

Completion time and execution duration have separate fields from cleanup time.
The runner captures the first authoritative streaming result before cleanup;
its no-result fallback conservatively uses invocation return. Slow cleanup
cannot retroactively classify a timely result as late. The private coordinator
supplies provider continuation and owned publication. The
[runtime owner](peer-runtime.md) adds restart recovery and readiness. See
[coordinator](peer-coordinator.md).
