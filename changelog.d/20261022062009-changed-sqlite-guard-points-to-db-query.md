- **The direct-`sqlite3`-write block now names the read-only route.** When the
  push guard blocks a `sqlite3` command that mentions a write keyword, its
  message now points read-only queries at
  `~/genesis/.venv/bin/python -m genesis db query "<sql>"`, where SQLite itself
  enforces read-only. Detection is unchanged.
