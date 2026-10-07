# Transcript analytics

This optional feature collects Claude Code JSONL from every project under
`~/.claude/projects`, including subagents, into
`~/.genesis/analytics/transcripts`. It excludes workflow journals from analysis.
The running Genesis database is not involved. Codex sessions are outside v1.

Enable deliberately in `~/.genesis/config/transcript_analytics.local.yaml`:

```yaml
enabled: true
# Optional measured override; otherwise 25% of effective memory capacity.
# ram_bytes: 3221225472
```

Use the normal update/bootstrap deployment path to install the pinned
`transcript-analytics` extra and refresh the hourly user timer. Other installs
remain disabled. `GENESIS_TRANSCRIPT_ANALYTICS_DISABLED=1` or a `DISABLED` file
in the data directory stops collection. Resource admission can defer work with
exit75; unavailable enforcement exits69. There is no uncapped fallback.

```bash
python -m genesis transcripts status
python -m genesis transcripts ingest --since 14
python -m genesis transcripts derive
python -m genesis transcripts sql 'SELECT error_class,count(*) FROM tool_calls GROUP BY error_class' --csv
python -m genesis transcripts evidence --tool-use-id example
python -m genesis transcripts verify
python -m genesis transcripts prune --before 2025-01-01
```

`ingest` derives a snapshot unless `--no-derive` is supplied. SQL normally uses
the compatible published snapshot, even when stale; stderr discloses freshness,
coverage and latest collection gaps. `--live` reconstructs views from compatible
source tables and exposes `raw_<table>` views. Incompatible historical sources
remain on disk but are excluded. `verify` compares sorted rows and hashes across
all snapshot/live views; it is an internal-consistency check, not an independent
parser correctness oracle. Regression fixtures independently specify expected
deduplication, error classes and final token usage.

`sql --manifest PATH --window LABEL --baseline COMMIT` writes private atomic
JSON provenance. The window is a label: apply its filter in SQL. The
opportunity-scan skill saves HTML plus matching manifests under the existing
output directory. Missing skill attribution is unknown; a next successful Bash
call alone is not evidence of recovery.

Evidence defaults to five surrounding records and 64KiB total output. It checks
the referenced tool ID, scrubs content, and falls back to locally available v2
encrypted archives when live context is absent or changed. Decryption requires
`GENESIS_BACKUP_PASSPHRASE` in the environment. It never downloads an archive.

Set `GENESIS_BACKUP_TRANSCRIPT_SCOPE=all` in the existing backup environment to
retain every project's JSONL and agent metadata as encrypted v2 objects; the
legacy default remains `main`. Vanished sources retain their last archive.
Enabled analytics joins the existing extra-directory backup, excluding derived
snapshots and staging. Restore serializes with analytics and invalidates its
snapshot. Legacy flat archives remain supported; v2 preserves original paths
and source modification times. Use the existing restore dry run and sandbox
verification before relying on a new installation's recovery path.
