# Request-owned SQLite units

`genesis.db.request_database.RequestDatabase` is an opt-in internal facade for
services that retain a database object while concurrent requests need separate
transactions. This prerequisite does not activate a shared MCP service or change
existing stdio/in-process initialization.

Construct it with an existing database path and an explicit positive `max_units`.
Use `async with db.unit_of_work():` around work. Each outer unit opens a fresh
physical connection; nested scopes and joined child tasks share the current
unit. Separate request tasks use `independent=True` to avoid inheriting a parent
unit. Detached tasks must acquire their own unit; ordinary context inheritance
does not make detached work independent. Await joined tasks before leaving scope.

Use `spawn_database_task(db, coroutine, name=...)` for detached work. With this
facade it clears only the database lease, preserves other caller ContextVars,
and opens a fresh unit before running the body. Legacy connections keep ordinary
`tracked_task` scheduling. The manager tracks jobs before they start, closes
unstarted coroutines on cancellation/admission failure, and cancels and joins
jobs during shutdown. Best-effort jobs may be cancelled by shutdown; this is not
a durable queue. Never await an independent writer while holding a pending write
transaction on the parent connection.

Provider activity flushes, embedding heartbeats, deferred recall writes,
pending-bookmark processing and proactive trace/cache/counter tasks use this
helper. Inline work remains in its caller's unit. Deferred-recall reservations
and procedure-cache refresh guards are released even on cancellation before
the body starts. Ambient bookmark routing policy remains a separate activation
requirement; independent database ownership does not choose the caller's session.

The facade uses the standard WAL, busy timeout, cache and synchronous pragmas,
with foreign keys disabled for compatibility with existing MCP connections.
Existing-only opens retain quarantine admission checks. Owned connections never
auto-reconnect. Admission waits at most `checkout_timeout_s` (default 10 seconds),
then raises; it never falls back to another request's connection. The capacity
bound includes opening and closing connections owned by this manager, not other
independently opened connections in the process.

Normal scope return closes tracked cursors and commits pending work. Exceptions
and cancellation before finalization roll back pending work. Explicit CRUD commits
remain durable: this is connection isolation, not whole-tool atomicity, and it
cannot roll back Qdrant, files, or external actions. A commit already queued when
the caller is cancelled may complete; a lost reply is an unknown outcome and is
never a reason for automatic replay. `executescript` retains SQLite's implicit
commit semantics.

Opening and cleanup tasks are retained through cancellation. Capacity is released
only after confirmed closure. A close failure propagates and retains that capacity;
manager shutdown reports unconfirmed closures, including a unit whose task finished
without leaving it. `close()` stops admission, cancels active/waiting units and joins
cleanup. It must run outside a unit, and the caller may bound its wait. It cannot
force a stuck SQLite worker thread to terminate, and it waits on a unit whose task
swallows cancellation; a unit that swallows cancellation and exits normally commits.

SQL without a live unit and saved cursor operations after scope exit raise
`DatabaseScopeError`. Cursors are closed at scope exit because a partially fetched
SELECT can pin the WAL even when commit/rollback alone releases nothing.

Graph traversal with this facade declines the NetworkX projection before building
it and uses the existing SQL fallback on the current unit's connection. This sees
the unit's own pending link writes and avoids a cache keyed to a stable facade
whose underlying connection changes. Healthy selected FalkorDB traversal retains
its existing projected-data semantics; if it becomes unavailable, scoped fallback
uses SQL. Legacy NetworkX traversal and centrality are unchanged. SQL cycle
exclusion compares complete TEXT IDs, including prefixes, wildcard characters,
empty strings and embedded NULs, using delimited hex tokens.
The existing traversal telemetry writer explicitly commits through its database
handle. When that writer runs on the owning unit, preceding pending writes can
therefore become durable before the tool returns; a later tool error cannot undo
that prefix. Scoped routing does not remove existing explicit CRUD commits.

Focused scratch-database coverage lives in
`tests/test_db/test_request_database.py` and
`tests/test_db/test_request_database_lifecycle.py`. Four legacy middleware
controls assert the known interfering outcomes alongside the isolated outcomes;
a shared physical connection cannot meet the isolation contract. These primitive tests are not shared-service activation
or production qualification evidence.

## Detached work and admission

`spawn_database_task(db, coroutine, ...)` uses a fresh independent unit for a
`RequestDatabase`; legacy connections retain their tracked-task behavior. To
allow detached work, explicitly configure both `max_background_units` and
`max_background_jobs` as positive integers. Without both budgets, spawning is a
configuration error. `max_units` remains the foreground limit; the total physical
connection bound is `max_units + max_background_units` when background work is
enabled. Neither pool releases capacity until physical closure is confirmed.

Background jobs retain their whole-job unit, including non-database work.
Accepted jobs wait for background capacity without the foreground checkout
timeout. Queued plus running jobs are bounded synchronously before scheduling;
overflow raises `BackgroundTaskCapacityError` and closes the unstarted coroutine.
This bounds task count, not retained payload bytes or SQLite disk contention.
It is not a durable queue: cancellation, SQL failure, or shutdown can prevent an
accepted job from completing. Shutdown refuses new work and cancels/drains both
pools under the existing owner registry.

Recall queue pressure falls back to a fresh inline coroutine in both candidate
and zero-hit branches. Shutdown refusal does not request inline resource use.
Best-effort observations tolerate admission refusal; provider activity retains
its batch for the next record and plan bookmarks retain their pending marker.
Programming/configuration errors are not treated as capacity pressure. The first
service factory must explicitly budget both pools before wiring this facade.

Admission and producer regressions are covered in
`tests/test_db/test_background_admission.py`,
`tests/test_db/test_request_database_tasks.py`, and
`tests/test_memory/test_recall_deferral.py`.
