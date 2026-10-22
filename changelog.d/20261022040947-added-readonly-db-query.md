- **`~/genesis/.venv/bin/python -m genesis db query "<sql>"` runs one read-only SQL
  statement** against
  the Genesis database. SQLite itself enforces read-only (a `mode=ro` URI,
  `PRAGMA query_only`, and an allowlist authorizer), so reads whose text mentions
  a write keyword now work, while writes, `ATTACH`, `VACUUM INTO`, transactions
  and state-changing pragmas are refused. FTS5 tables work; rtree tables are not
  supported. A runaway query is aborted after 60 seconds (`--timeout`, `0` for
  none). Supports `--param`, `--json`, `--limit` (truncation reported on stderr)
  and `--db`. A refused statement exits 4.
