# Transcript analytics: attribution and metric coverage

The offline Parquet/DuckDB store separates physical source placement, claimed
message identity, streaming state, executor and delegation. It does not assign
ownership by transcript chronology or by the source chosen for display.

`turns` contains reconciled candidate rows and all physical source references.
The manifest labels its denominator as candidate rows and reports uncertain
candidate identities separately. Complete message
fingerprints include all message fields and nested usage. A nonterminal copy can
upgrade to one terminal payload only when immutable fields and present request
IDs agree. Different terminal payloads leave usage unknown; token fields are
never maximized or assembled from different records. Missing record UUIDs are
source-local. Ordered fragment sequences select a representation only when one
sequence contains every other sequence in order; incompatible assembly excludes
usage and content-dependent counts. A terminal record without a usage object
keeps usage unavailable; a partial vector preserves missing fields as unknown.

`executor_id` and `attribution_status` expose confirmed, inferred, unresolved or
conflicting attribution. Confirmed means a unique actor in the observed source
set, not proof that no deleted source ever held the event. A contiguous shared
prefix in a containing main transcript supports structural inference. Copies
across contexts, unresolved child-only copies and contradictory content remain
visible. `actor_lineage` resolves the spawning call's producer before assigning
an immediate parent; contradictions and cycles exclude the parent edge.

`executors` is an exclusive count. `delegation_rollups` includes each executor
and its established ancestors, counting distinct turns. `context_rollups` counts
distinct turns in each observed containing context. These inclusive views can
overlap and must not be added to executor totals. Unresolved ownership remains
in global `turns` counts. `metric_coverage` exposes separate included/excluded
denominators for content, assembly, usage, every token field, executor, tool calls
and tool results. Token-field denominators count non-null validated values.
`raw_*` views are available only with live query mode and retain observations.

Titles select the latest timestamp as an instant, including repeated values.
Unknown-context session summaries retain their call counts and scrub-failure flags.

Ingestion repairs previously stored generations before applying `--since` to
new sources. Missing or unreadable originals preserve existing files; incompatible
generations are explicitly excluded. Companion presence/content changes require
a new generation. Timestamps used for chronology are normalized to UTC; missing
session identities use source-scoped fallback values. Materialized snapshots
include the same attribution and coverage views as live queries.
