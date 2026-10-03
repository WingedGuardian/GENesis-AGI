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
If failed-row reuse loses its eligibility race, the entire in-memory drop is
discarded, not just that batch. The next scan recovers any earlier durable
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
