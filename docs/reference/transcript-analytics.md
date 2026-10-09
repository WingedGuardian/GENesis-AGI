# Transcript analytics

This optional local CLI collects Claude Code JSONL from projects under
`~/.claude/projects`, including subagents, into
`~/.genesis/analytics/transcripts`. Workflow journals are excluded from analysis.
The running Genesis database is not involved. Codex sessions are outside v1.
Collection defaults to disabled. Enable deliberately in the private
`~/.genesis/config/transcript_analytics.local.yaml` overlay:

```yaml
enabled: true
# Optional measured override; otherwise 25% of effective memory capacity.
# ram_bytes: 3221225472
```

The canonical `config/transcript_analytics.yaml` must remain installed.
`GENESIS_TRANSCRIPT_ANALYTICS_DISABLED=1` or a `DISABLED` file in the configured
data directory stops collection without rewriting the persistent opt-in.
The environment kill switch preserves configured paths and resource settings;
the canonical configuration and private overlay must still validate.
Resource admission can defer work with exit75; unavailable enforcement exits69.
Explicit RAM caps must meet the shared 16 MiB launcher minimum. A percentage
that computes a smaller cap is refused with exit69 rather than increased.
This startup minimum does not guarantee that a given analytics workload fits.
Heavy commands require an enforced, registered systemd scope with a one-hour
runtime limit. Uncapped launches are refused. First cancellation requests
cooperative shutdown; a second cancellation forcibly stops the owned scope.
Admission rereads configuration while holding its lease before spawning work.

Install the pinned `transcript-analytics` extra in the development environment
before using the CLI. Timer installation and activation arrive in the dependent
installer change; this change does not install scheduled collection.

```bash
python -m genesis transcripts status
python -m genesis transcripts ingest --since 14
python -m genesis transcripts derive
python -m genesis transcripts sql 'SELECT error_class,count(*) FROM tool_calls GROUP BY error_class' --csv
python -m genesis transcripts verify
python -m genesis transcripts prune --before 2025-01-01
```

`ingest` derives a snapshot unless `--no-derive` is supplied. After skipping,
run `derive`. `derive --if-stale` checks freshness under the same writer lock
used for publication. SQL normally uses the compatible published snapshot,
including a stale snapshot, and discloses freshness, coverage and collection
gaps on stderr. `--live` reconstructs views from compatible source tables and
exposes `raw_<table>` views. Incompatible retained sources remain on disk but
are excluded from those views. Human-readable SQL escapes terminal control
characters in values and column names; CSV preserves machine-readable values.

`sql --manifest PATH --window LABEL --baseline COMMIT` writes private atomic
JSON provenance. The window is a label; apply the actual filter in SQL.
Accepted source identities remain selected across renames until explicit
pruning. Retention and matching UUIDs do not establish ownership: executor,
delegation and context views keep unresolved attribution and explicit coverage
denominators. See [attribution](transcript-analytics-attribution.md).

`verify` compares sorted rows and hashes across snapshot and live views. It
closes the snapshot engine before opening the live engine, retaining the writer
and publication locks and the same selected catalog across both phases.
It checks internal consistency; independent parser fixtures specify expected
deduplication, error classes and final token usage. Schema5 and views version16
require re-extraction or rebuilding when older material is incompatible.
Evidence retrieval and encrypted backup/recovery arrive in dependent changes.
