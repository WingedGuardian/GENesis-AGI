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
manager shutdown reports unconfirmed closures. `close()` stops admission, cancels
active/waiting units and joins cleanup. It must run outside a unit. It cannot force
a stuck SQLite worker thread to terminate.

SQL without a live unit and saved cursor operations after scope exit raise
`DatabaseScopeError`. Cursors are closed at scope exit because a partially fetched
SELECT can pin the WAL even when commit/rollback alone releases nothing.

Focused scratch-database coverage lives in
`tests/test_db/test_request_database.py` and
`tests/test_db/test_request_database_lifecycle.py`. Four legacy middleware
controls assert the known interfering outcomes alongside the isolated outcomes;
a shared physical connection cannot meet the isolation contract. These primitive tests are not shared-service activation
or production qualification evidence.
