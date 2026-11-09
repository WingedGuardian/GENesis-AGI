# Inbox replay safety

Provider overloads (HTTP 529) with no known-work or MCP evidence retry the same
already-approved invocation after 30, 120 and 300 seconds. Exhaustion leaves an
inbox batch retriable without consuming its retry budget. Genuine 429/quota,
timeout and cancellation handling remain separate.

Overloads reporting more than one CLI turn, or carrying MCP backend evidence,
instead raise `CCReplayUnsafeError`. Stream truncation inherits that boundary.
Conversation recovery, roster failover and durable rate-limit parking must not
repeat these full-tools invocations. Tool-less conversational contingency can
still provide a reply; it does not re-execute CC work.

The inbox persists these outcomes as failed rows with the literal
`replay_unsafe:` error prefix, preserving retry count. The hold is written before
independent session-finalization bookkeeping. Retry selectors, abandonment and
cached-row reuse preserve the hold even if `max_retries` increases. Readable held
item blocks suppress duplicate deltas, but do not enter the completed baseline.
An unreadable held payload blocks dispatch for that source file, including
after an edit; unrelated source files can still run. An owner alert identifies
the held batch without exposing URL query strings or raw diagnostic text.
If a new-row insertion or failed-row reuse loses its eligibility race, the entire
in-memory drop is discarded, not just that batch. The next scan recovers any earlier durable
pending rows and rebuilds the delta with the hold excluded.

## Inspect and deliberately release one batch

From the repository root, using that checkout's Python environment:

```sh
PYTHONPATH=src .venv/bin/python scripts/inbox_replay_hold.py --item EXACT_BATCH_ID
PYTHONPATH=src .venv/bin/python scripts/inbox_replay_hold.py --item EXACT_BATCH_ID --release --acknowledge-replay
```

The default database is the existing install database (`genesis_db_path()`);
`--db /absolute/path/genesis.db` selects an existing database explicitly.
Inspection is read-only and prints no item URLs or raw error text. Review the
batch and any actions it may already have executed before acknowledging replay.
Release uses the canonical guarded async connector with `existing_only=True`:
it cannot create a replacement if the database disappears between inspection
and writable open, and retains quarantine checks before and after opening.

Release refuses absent/non-held items, unreadable item boundaries, missing or
changed source content, and a concurrently changed row. It resets only the
selected hold's retry budget, preserves its payload and diagnostic, and dispatches
nothing. The next normal inbox scan performs normal approval and drop claiming.
Repeating the command does not release it again. There is no bulk release or
force flag; changed-source or corrupt-payload cases need a separately reviewed
operator repair, not an automatic guess at item boundaries.

## Retry ownership

Retry counts follow the complete ordered batch of logical item texts, using the
queue's existing tracking-parameter normalization. Inserting or reordering items
cannot assign a different batch's failures to new work. An unchanged batch reuses
its own row and retry count; regrouped batches start fresh. Annotations and item
boundaries remain part of identity, rather than matching URL alone.

Missing, corrupt or ambiguous legacy identities are retained as history while
reconstructed batches get new rows and bounded retry budgets through normal
approval. A legacy single-line item can still match. Subsequent unchanged retries
reuse the reconstructed rows, so they do not get a new budget on every scan.
Once replacement work completes, exhausts its budget or is removed, the existing
empty-delta cleanup removes obsolete retry eligibility while retaining history.

Both insertion and reuse atomically check that every still-unused selected retry
row remains eligible, including unmatched history. Reuse also checks its exact
stored payload. Losing either write discards the whole in-memory drop; earlier
committed rows remain recoverable on restart. This preserves the selection-to-write
boundary, not a global content claim or protection from a hold written afterward.

## Corrupt payloads during approval resume

A corrupt versioned payload encountered while resuming an approved batch is
marked `batch_items_corrupt_restart`. Its retry count is kept below the active
`max_retries`, including when that cap was lowered while approval was pending.
Approval resume consumes the old site grant before releasing the durable
awaiting marker, whether the row will dispatch or be invalidated. If lookup or
consumption fails, the row, retry count and payload stay parked. All new,
modified and retry work for the inbox site waits for another scan; otherwise a
different file could reuse that same grant. A healthy sibling can still resume
if its own consumption succeeds. No new persistent hold is created.

After successful retirement, item boundaries are re-derived under fresh
approval into new rows with fresh bounded budgets; the corrupt historical row
is not repurposed. Existing lowered-cap normalization of that historical row
still applies. This does not change the legacy approval-resume fallback or
gate-disabled behavior. Consumption
committed before an interrupted row update is safe to observe again on the
next scan. The row claim still precedes the evaluator call.
This recovery does not release `replay_unsafe:` holds; unreadable held payloads
still need deliberate operator repair as described above.

This does not add atomic retirement for a pending approval resolved concurrently
with source deletion, or expected-state predicates for stale inbox-row updates.

## Limits

Unknown or unusable turn counts remain retryable by explicit accepted policy.
That is not evidence that no tools ran: plain-text 529s without a CLI result can
replay work. Classification reads the last actual result event in raw stdout
(including before trailing diagnostics), never JSON-looking decoded answer prose,
and preserves stderr and structured MCP evidence.

This is not exactly-once execution. Arbitrary crashes before a hold is committed,
database write failure, source-file races during operator release, and external
side effects without idempotency remain outside that guarantee. No schema migration
or recovery of historical unknown-work failures is performed.
