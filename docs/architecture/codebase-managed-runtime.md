# Managed Codebase runtime: implementation and acceptance gates

Status: **in development; not an authorization to re-enable Codebase MCP**.
The emergency disabled launcher must remain in place until runtime acceptance.

## Why a larger per-client limit is insufficient

Upstream v0.10.8 uses a shared daemon, including for ordinary CLI indexing.
A capped CLI can attach to a daemon outside that cap. Native MCP also starts a
daemon automatically when none is available; checking status before launch
does not close the daemon-exit race.

The intended ownership boundary is one bounded service whose broker starts
every native MCP client and indexing submitter. Session shims only connect to
the broker. The daemon and workers then inherit that service's cgroup even
after daemonization. Dedicated `CBM_RUNTIME_DIR` and `CBM_CACHE_DIR` prevent
attachment to legacy endpoints. Private upstream process-role flags are not an
integration API. In UI-enabled
v0.10.8 builds, public `daemon start` enables the UI even if the persisted
`ui_enabled` setting is false (`src/main.c`, fresh-start branch). A broker-owned
analysis-only keeper passed an isolated startup/read/shutdown smoke test as an
alternative; full lifecycle and indexing acceptance remain pending. Before any
bootstrap, the broker must verify an explicitly UI-disabled cache configuration:
an embedded-UI daemon defaults to enabling the UI if that file cannot be opened.

The native analysis profile supplies read/navigation tools; the managed queue
owns explicit index requests. Automatic watchers/indexing must be disabled.
The service must refuse unverified executable, endpoint, cache, or containment
identity. A missing manager or invalid limit must not fall back to an uncapped
process or address-space-only limit.

## Delivery stages

1. **Queue prerequisites (locally verified, not merged):** keep pending, inflight,
   terminal outcome, retry budget, and full-index clocks in one transactional
   SQLite authority; preserve active ownership; bound orphan recovery; refuse
   database/runner lock failures. Preserve request coalescing and freeze semantics.
2. **Managed lifecycle:** broker and local shim, dedicated runtime/cache,
   disabled-state persistence through install/update, pinned executable,
   service-owned native children, bounded client count, verified shutdown.
3. **Managed indexing:** one global active job across tools/repos, asynchronous
   request/status interface, pressure admission and active cancellation, durable
   resource-failure quarantine that new commits cannot reset. Do not release
   ownership until the actual worker is terminal. Preserve the last valid graph.
4. **Existing PR reconciliation:** #1923 must cap daemon-owned work, #1915 must
   apply OOM priority to actual workers, and #1802 must distinguish deferred,
   paused, failed-refresh, unavailable, and healthy index states. Rebase claims
   of readiness on the final diff and fresh required reviews/CI, not old results.
5. **Observability:** verify deployed Guardian behavior, OOM deltas rather than
   lifetime counts, repeated short-stall warnings independent of recovery
   approval, and actual Telegram receipt during rollout.
6. **Post-review acceptance:** isolated lifecycle and tool E2E, staged production
   canary, then explicit re-enablement. Finish remaining GitNexus/Serena update
   validation separately; do not treat intentional partial FalkorDB integration
   as an outage or silently expand its scope.

## Queue prerequisite semantics

Queue state lives in `index-requests/queue.sqlite3`. Each mutation uses
`BEGIN IMMEDIATE` and commits pending/inflight/failed transitions atomically.
The database uses rollback journaling plus `synchronous=FULL`: both Python
runtimes exercised here embed SQLite versions in the documented WAL-reset
bug range, so WAL is intentionally forbidden until the runtimes receive the
upstream fix. No indexing occurs while holding a short database transaction.
The legacy `.queue.lock` remains only as a rolling-upgrade migration boundary.
Because an already-running pre-SQLite writer does not honor that lock, migration
first atomically renames each replaceable legacy path into a unique, recoverable
claim, then imports it idempotently. A later old-process replacement recreates
the original pathname and survives retirement of the claimed file. Neither lock
replaces the runner's execution lock or proves daemon workers terminated.

Normal SQLite acquisition gives up after 0.5 seconds. Enqueues then write a
unique, file-and-directory-fsynced spool entry rather than dropping the request
or waiting in a commit-sensitive caller. Terminal outcomes use the same durable
event inbox after their bounded five-second SQLite wait, but each event carries
the exact claim nonce. The importer coalesces enqueue events and applies an
outcome only to its matching inflight generation. Duplicate identical outcomes
are idempotent; conflicting outcomes fail closed to one bounded retry rather
than choosing a winner by file order. Database corruption and non-contention I/O
errors still propagate instead of being mislabeled as lock pressure. The async
GitNexus scheduling job performs marker I/O in a worker thread so waiting does
not stall the Genesis event loop. Last-resort disk reclamation establishes a
durable SQLite or spool rebuild request before deleting an index cache;
installation reports queue failure instead of claiming success if neither can
be recorded.

A second claim cannot replace an existing inflight row. A new request written
while indexing remains a separate pending row and therefore survives consume.
If the older inflight generation exhausts its attempt budget, only that
generation becomes failed; the newer pending request remains runnable. Orphan
recovery counts an unknown runner outcome against the existing five-attempt budget;
normal frozen/missing-tool deferrals still do not consume that budget. This is
only the prerequisite policy: immediate quarantine of resource failures and
deliberate retry are still pending managed-runtime work.
The full-escalation query has a three-way shell contract: exit 0 means due,
exit 1 means not due, and any other status restores the claimed request and
stops the tick. An operational queue error therefore cannot silently downgrade
an overdue full build to fast and then consume it.
Once the index entrypoint returns, the runner first persists the terminal
action on the owned inflight row, then applies the queue transition. A later
tick imports and replays either the SQLite row or its claim-bound durable event
before generic orphan recovery.
This prevents a successful index whose consume step lost queue access from
being rerun or charged as a crash, while a stale action cannot affect a newer
claim. Direct consume, restore, and terminal-outcome operations also require
the matching claim nonce, so no public mutation path bypasses ownership. Imported
numeric fields are normalized before binding and values outside the queue's
declared attempt range are quarantined. Schema checks make invalid new queue rows
unrepresentable. Malformed or stale legacy files are retained in bounded
failed/failed-outcome tables and
cannot block a separately valid pending request. Missing read/list state remains
empty, while an ownership mutation without its live claim fails loudly. Database
corruption, permission, and I/O errors propagate instead of being reported as an
empty queue.

## Resource and verification contract

The provisional test ceiling is **5 GiB for the aggregate Codebase service**,
not per session, with zero swap allowance. It is a test setting, not a measured
production requirement or permission to grow automatically. Validate visible
ancestor limits and dynamic headroom with a reserve; unknown capacity defers
work. Stop indexing on rising pressure without automatic resource-crash retry.

Use a tiny repository and private runtime/cache/queue in a bounded test service
before any production-repository rebuild. Inspect actual daemon and worker
cgroup membership, including concurrent startup and daemon-death races. Check
shutdown leaves no descendants and requests remain paused after resource failure.

Test coalescing, concurrency, freeze, cancellation, restart, malformed state,
headroom boundaries, cache preservation and freshness after successful rebuild.
Exercise all eleven native analysis tools and managed request/status operations.
Run targeted tests during implementation, full required CI before merge, and
repeat E2E after code review. Record production OOM counter baselines; require no
new production OOM kills and no dashboard probe failures (target p95 below 1s).

Rollout is disabled → one query client → one managed refresh → multiple clients
→ 24-hour canary → re-enabled. Passing unit tests or a service-manager property
probe alone cannot satisfy this gate.

## Confidence and remaining uncertainty

Confidence is high in the daemon-ownership requirement, based on v0.10.8 source,
and in queue defects reproduced by regression tests. Runtime readiness remains
unproven until actual membership, cancellation, freshness and pressure E2E tests
pass. No claim of zero regression risk is justified; retain the disabled
fallback and stop rollout on material deviations.

### Current verification record

- Initial regressions reproduced lost concurrent updates, overwritten inflight
  ownership, unbounded orphan retries, and fail-open runner locking.
- Review found an unbounded-lock/event-loop regression. Both held-lock and
  heartbeat tests failed before the bounded-wait/off-thread fixes. Later
  adversarial rounds reproduced malformed-state wedging, queue/delete races,
  lost post-index outcomes, and a retry-cap partial-transition escape. Later
  review found that file replacement still lacked crash-durability syncs and
  leaked temporary JSON could be consumed as work. Because the defects were
  concentrated in the same growing file state machine, the queue was replaced
  with SQLite rather than further expanding the multi-file protocol.
- Current affected run: **134 passed**, including rollback-journal/integrity
  assertions, independent CLI concurrency, transaction rollback injection,
  claim-output-to-terminal-spool cross-process contention, conflicting-outcome
  fail-closed behavior, every runner result path, disk reclamation and critical
  failure propagation, legacy migration, async responsiveness and job
  integration. Targeted Ruff and shell-syntax checks pass; whole-tree gates are
  rerun before each push.
- A harmless capped-service property probe passed. A separate native lifecycle
  probe used private runtime/cache directories and a 512 MiB/no-swap service,
  but its eight-second `daemon start` deadline expired before MCP checks. Upstream
  allows 30 seconds for startup, so this is NOT proof of an upstream startup bug.
  The isolated service was cleaned up and no probe processes remained.
- A subsequent isolated analysis-keeper probe passed initialization, the exact
  eleven-tool analysis allowlist, `list_projects`, actual keeper/daemon cgroup
  membership and absence of a daemon-owned TCP listener. Closing the keeper
  ended the session-managed daemon; the later public stop call found none left.
  The service peak was 22,859,776 bytes with zero OOM events. This is an empty-cache
  read-only smoke measurement, NOT a production indexing memory requirement.
- No worker-containment, managed-runtime E2E, full CI, merge, production deployment
  or canary success is claimed. The next gate is implementing and testing the
  broker and queue integration, including keeper-death and worker-cancellation
  races, before any production-repository refresh.
