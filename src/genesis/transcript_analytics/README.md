# Durable transcript analytics

## Store and concurrency contract

`store.ingest(projects, data)` publishes one complete six-table Parquet record
under `sources/<key>/<record>/`, then atomically replaces that source's `current`
symlink. Files and containing directories are fsync'd before the selector is
published. An interrupted refresh never overwrites the accepted record.
`build_source`, ingest and prune use the same exclusive writer flock.
Collection opens the source leaf without following symlinks, including a leaf
replaced after discovery, and admits only regular descriptors without blocking.
Agent-sidecar fingerprinting uses the same admission and a 1 MiB read ceiling;
missing sidecars retain their existing meaning. Collection uses one captured
canonical projects root and isolates individual source stat/read failures, writing
an incomplete inventory while continuing with the other sources. Corrupt root
bindings require correction; they cannot silently become an unbound store.
Accepted residue for the local first release: the extractor reopens sidecar
metadata after fingerprint admission. Concurrent adversarial sidecar replacement
or growth in that gap is outside this release's scope; admission is not a
filesystem sandbox. Parser signatures and accepted historical records are unchanged.
Directory durability fencing currently opens every
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

The independent storage tests require no query/reconciliation modules. Query and
snapshot functionality follow in the dependent A2 and B parts of #3425.
