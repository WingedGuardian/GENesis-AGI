# Validator MCP writers

The separate `validator` server profile admits nine health and nine memory
tools. The ordinary `external` profile keeps its five health and eight memory
tools. This is capability admission, not authentication or host isolation.
The dedicated launcher and its complete qualification remain prerequisites;
changing this profile does not activate the ordinary Codex configuration.

`memory_store` retains the existing content/source, collection and supersedes
interface. It uses the existing MemoryStore pipeline, including its existing
multi-store partial-failure limits. It adds no session, charter or transcript
registration. An authorized memory write is a state change, not recall.

`follow_up_create` requires a stable ASCII alphanumeric/underscore/hyphen
`request_key` of1–128characters, content, reason, work_state and
strategy=`user_input_needed`. It cannot request schedules, pins, tasks, goals
or session provenance. Source is fixed to `codex_interactive` with a NULL
source_session. Retry an uncertain creation with the same unchanged key while
its dedup row is retained; changed requests and ineligible replays refuse.
Replay reports the current stored status rather than inventing pending.

`follow_up_list` and `follow_up_update` cover eligible private records across
clients. Eligibility excludes public links, held/posted publication requests,
scheduled/linked work and nonstandard statuses/lanes. Missing classification
tables refuse. Update accepts status, resolution_notes, blocked_reason and
priority; it rechecks eligibility in the UPDATE. Pins cannot be cleared and
pinned records cannot be completed/failed. Notes-only updates preserve status.
Listing limits the returned page and labels the limit; counts use the same
predicate/filters in a separate read. Concurrent changes can separate the reads.
The dashboard's mine view includes both foreground and Codex attended sources;
system excludes both, preserving existing NULL behavior.

`pr_verification_close` accepts `evidence_json`, independent `pr`, optional
`park`, `note`, `verdict` expectation and `dry_run`. Duplicate JSON keys, strict
model errors, PR mismatch and verdict-policy refusals precede DB work. Both raw
UTF-8 and canonical evidence are capped at256KiB. The verdict is derived;
closing decisions close an OPEN obligation and other decisions record a
nonclosing attempt with the same note prefixes as the CLI. Closed obligations
cannot be rewritten. A real attempt increments a counter and is not idempotent:
after an uncertain response, re-read stored evidence/count before retrying.

Every call reloads `config/codex_validator.yaml` and the strict local overlay.
The tracked base must be exactly `dry_run_only: true`. Missing/malformed/base
false/nonboolean/unknown keys force preview. The user overlay at
`~/.genesis/config/codex_validator.local.yaml` wins over the repository sibling;
only its valid exact boolean false enables writes. The existing operator
settings domain `codex_validator` is the supported lever after separately
authorized calibration. No flag is cleared by this PR or by the MCP request.
Validators cannot call settings_update. Caller dry_run always forces preview.
The gate covers nonclosing attempt notes as well as closure.

Preview uses only SELECTs on the existing service handle. It neither commits
nor opens/closes a writer connection, and it may observe shared pending state.
The dedicated validator-health bootstrap disables new activity persistence on
its fresh tracker before attaching the existing DB handle, and initializes
instrumentation without a transaction DB. Tool metrics remain in memory and
historical provider SELECT fallback still works. Ordinary/default/external health
and memory initialization keep their existing defaults. This is construction-time
policy, not a safe hot toggle or a read-only tracker/server guarantee; explicit
reap and the separate job-health fallback retain their existing behavior.
Real verification/private writes open their own admitted existing-only handle
to the service-owned DB path; no caller path, migration, new database or WAL
mode change. They cannot commit another coroutine's pending transaction.
SQLite can refuse a concurrent write while that transaction is pending.
Connection cleanup finishes the same tracked close under repeated cancellation.
Cancellation can follow a persisted commit; absence of a response proves no
rollback. A completed operation stays completed if later cleanup fails.

Task-augmented execution is forbidden for these tools. Profile middleware sets
and resets the server-selected role for ordinary calls; no argument or ambient
conversation identifier grants validator authority. No public publishing,
dispatch, session ledger, charter, outreach or settings tool is added.
