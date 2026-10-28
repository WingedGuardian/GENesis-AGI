# Transcript analytics

The optional local CLI collects Claude Code project and subagent JSONL from
`$CLAUDE_HOME/projects` into `$GENESIS_HOME/analytics/transcripts` (defaulting to
`~/.claude/projects` and `~/.genesis/analytics/transcripts`). Codex logs are outside
v1. It uses the optional `transcript-analytics` package extra and systemd user
scopes; the running Genesis database is not involved.

The shipped `config/transcript_analytics.yaml` defaults to disabled. Opt in through
the existing private `~/.genesis/config/transcript_analytics.local.yaml` overlay:

```yaml
enabled: true
```

The canonical configuration must remain installed even with a private opt-in.
Explicit `projects_dir` and `data_dir` settings, including explicit tilde paths,
override these defaults. The existing private overlay location above is unchanged.
Defaults cap each admitted command at 25% of effective memory and CPU capacity;
`ram_bytes`, `ram_pct` and `cpu_pct` provide explicit overrides. Admission can defer
work with exit 75. Unavailable resource enforcement refuses a launch with exit 69.
Very small CPU caps can exceed the existing 30-second startup deadline.

With the optional extra available, manual use follows the existing CLI:

```bash
python -m genesis transcripts status
python -m genesis transcripts ingest
python -m genesis transcripts sql 'SELECT tool,count(*) FROM tool_calls GROUP BY tool' --live --csv
python -m genesis transcripts verify
```

`ingest` refreshes source records and derives a disposable snapshot unless
`--no-derive` is supplied. `derive` builds it separately. Cached SQL uses only a
compatible, current snapshot; a stale, missing or unreadable cache falls back to
live views over the same captured source selection. `verify` requires an available
current cache and compares schemas, counts and row digests with forced live views.
Excluded sources keep verification incomplete.

Plain `sql` sees the derived views only, whether or not the cache is current; use
`sql --live` for the raw tables and reconciliation intermediates. A query's
`--window` is a provenance label; filter timestamps in SQL for an actual analysis
window. `--since` selects source files during ingestion
and does not filter the records returned by SQL. Use a distinct `--manifest PATH`
for each query so later queries do not overwrite earlier evidence.

`sql --max-rows N` bounds the retained preview, while the footer and manifest
still count the complete result. The command drains the query once, so expensive
SQL can still reach its resource or runtime limit. CSV streams every row using
the existing CSV format; an interrupted export may leave partial output and
exits with an error without publishing a new success manifest. Status reads only
confined snapshot metadata; use `verify` for complete cache/live validation.
Status reports individual source stat/fingerprint failures in `source_errors`
and continues inspecting the remaining sources.

`GENESIS_TRANSCRIPT_ANALYTICS_DISABLED=1` or a `DISABLED` file in the configured
data directory blocks analytics commands while retaining saved opt-in. Status
reports disabled; timer invocations skip without failing. First cancellation asks
for cooperative shutdown; a second cancellation forcibly stops the owned scope.
Wait for supervised commands and their scopes to finish before destructive
uninstallation. The separate installer/retirement rollout is required before
unattended hourly collection; this CLI does not activate scheduling.

Accepted revisions remain until explicit maintenance. Stop readers before pruning.
Historical adapters retain original provenance and mark unavailable facts unknown.
Paired tool results keep their raw child IDs searchable, but those IDs alone cannot
establish or contradict an agent relationship because their independent result
context was not retained. Independent companion and scoped unpaired-result proof
remain eligible; unsupported relationships stay unknown.
See the [store and reader contract](../../src/genesis/transcript_analytics/README.md)
for source selection, retention, reconciliation and snapshot details.
