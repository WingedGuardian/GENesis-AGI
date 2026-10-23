# Durable transcript analytics

## Store and concurrency contract

`store.ingest(projects, data)` publishes one complete six-table Parquet record
under `sources/<key>/<record>/`, then atomically replaces that source's `current`
symlink. Files and containing directories are fsync'd before the selector is
published. An interrupted refresh never overwrites the accepted record.
`build_source`, ingest and prune use the same exclusive writer flock.
Collection opens the source leaf without following symlinks, including a leaf
replaced after discovery. Directory durability fencing currently opens every
ancestor of the data directory; execute-only ancestors are unsupported.
During first publication, a concurrent reader may briefly report the new source
as excluded until its selector exists. It never admits an incomplete record.

This **directory/current-link layout is an owner-approved deviation** from the
original per-file replacement spec in #3425. Six independent replacements are
not an atomic replacement of a complete source record.

Readers capture and validate each source once with `select_sources`. A captured
`SourceRecord` supplies every table path and the original metadata; readers must
not re-resolve `current` for successive tables or evidence. There is no catalog,
lease, restore epoch, reader lock or persisted reader registry. Selection is per
source, not a global all-sources transaction.

All physical revisions remain until explicit prune, including records superseded
by an hourly refresh. Disk usage therefore grows with refreshes. **Pruning is
maintenance: stop readers first.** `prune` removes the complete retained history
of expired sources whose original transcript is gone, after checking the bound
projects root. Ordinary ingestion never retires accepted history automatically.
Pruning validates every retained directory revision and any shadowed flat
record. Every record must have a known timestamp before the cutoff; unreadable,
incompatible, incomplete or escaping records are retained. Symlinked source
directories are never traversed for deletion. A present but invalid `current`
selector excludes the source rather than resurrecting its shadowed flat data.
Reserved `.current.<decimal-pid>` symlinks left by interrupted swaps do not block
maintenance eligibility; their targets are never followed during deletion.

## Historical records

Old flat files are read in place and left intact when a newer directory record
shadows them. `history.py` lists exact audited schema/extraction/scrubber tuples,
not a wildcard compatibility bridge:

- Flat schema 1 from `27a1513b`.
- Schema 5 from `f693174b` and the #3002 producer at `1a9c9c22`.

Each format must match all six audited physical schemas and consistent original
source/fingerprint/record stamps. Unknown or inconsistent formats remain on disk
and are explicitly excluded; they are never declared current.

Read adapters add typed nulls for absent fields without changing stored data or
restamping its metadata. Original values, including scrub/error flags, are kept.
Missing identities, hashes, usage availability and scrub-failure evidence are
unknown, not synthesized success or attribution. Later query coverage reports the
original provenance and unavailable facts. A historical record is never current
for ingestion, even when its transcript fingerprint happens to match: if the
transcript exists it can be re-extracted; if permanently gone its audited
historical observations remain usable.

## Live queries

`query.connect(data, live=True)` binds all raw tables to one captured source selection.
DuckDB's later lazy reads and query evidence use those same immutable paths and
metadata. Live and deduplicated views retain the existing reconciliation rules;
hooks/events aggregate scrub failures over every copy. Recomputed tool latency
and fallback latency are nonnegative or unknown.

Tool observations are identified by their enclosing event and tool-use ID, so
multiple tool blocks on one JSON record remain separate. Cross-file results
require a matching call observation or a shared known context; ambiguous results
remain orphaned. Delegation evidence is scoped to its context and requires one
distinct spawning observation before confirming a parent. Scrub failures are
retained when any call copy or linked result reports one.
Paired results retain raw child IDs for search, but cannot prove or contradict
lineage because extraction did not retain their independent result context.
Without independent companion evidence their spawn association stays unresolved
and excluded from confirmed coverage. Unpaired results retain their own context
and remain eligible for the existing producer checks.
Unknown historical actors keep distinct structured source-local identities and
do not create lineage nodes. Missing content hashes remain unknown and count as
excluded content; observed conflicting hashes remain conflicts, including mixed
known/missing copies. Ownership and supported attribution use the same complete
evidence envelope, including every call copy used by delegation. Missing call or
result envelopes are distinct from present envelopes whose hashes are unknown;
validity remains false in both cases. Hook/event ownership also requires every
observed actor. Conflicting tool observations retain stable display values without
resolving ownership, and duplicate orphan results preserve any scrub-failure flag.
Views version 24 invalidates caches containing the previous derived semantics;
stored source schemas, extraction stamps and original provenance are unchanged.

`source_coverage` exposes original per-source provenance and unavailable
historical facts. `run_query(..., manifest_path=...)` records the same captured
coverage alongside result denominators, the query views version and scrubbed query labels. Missing
historical identity is unresolved, not attributed to a current executor.

Each connection uses its own 0700 spill directory under `TA_DUCKDB_TMP` or
`~/tmp/transcript-analytics/duckdb`; an unowned or symlinked root is rejected.
The independent storage tests require no query/reconciliation modules.

## Disposable snapshots

`derive.build(data)` holds the same writer flock while capturing the sources
and writing all thirty shared derived and reconciliation views into `derived/.staging/<stamp>/`.
It fsyncs every Parquet file, the manifest and the directory, publishes the
complete `derived/<stamp>/`, then atomically replaces `derived/current`.
The immediately previous snapshot is retained; older cache directories and
interrupted staging are disposable. No durable source is removed by a build.
Rebuilding removes malformed staging and selector entries by their actual type;
cache symlinks are unlinked without inspecting or deleting their targets.

`query.connect(data)` and `run_query` prefer a snapshot whose manifest matches
both the current views version and the captured inputs signature. Every table
is read completely into connection-private DuckDB tables in one transaction,
with its schema checked against the manifest. A missing, corrupt, unreadable,
incompatible or stale snapshot rolls back and uses live views over the same
captured source selection, emitting one `snapshot_fallback` event. This also
catches damaged data pages with intact Parquet footers. A successful connection
no longer depends on disposable files, so cache cleanup cannot break later
queries. Full reads trade connection-start work and private spill for catching
all snapshot read errors before results reach the caller.

Use `run_query(..., live=True)` (CLI `sql --live`) for raw views; snapshots
contain derived views only. The first release also requires `--live` for harmless
raw-looking SQL text, including `SELECT 'raw_tool_calls'`, comments and search
patterns such as `LIKE '%raw_%'`. Its text check is case sensitive, so uppercase
raw relation queries can behave differently after cache fallback. Use `--live`
consistently for these queries. This is a deferred SQL usability limitation, not
a security boundary. The private spill rules also
apply to snapshot materialization. `derive.is_current` checks the complete
cache, not just file existence.

After backup restore, call `derive.discard_after_restore(data)`: it deletes
only `derived/` and syncs its parent directory before returning, without changing source records, original stamps, root binding
or inventory. Live queries work immediately and snapshots rebuild from durable
records even when their original transcripts are permanently gone. Additional backup and restore command wiring is deferred; it does not gate
collection, live search or opportunity scanning.
