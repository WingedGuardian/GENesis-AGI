"""Read-only PRAGMA facts for the infrastructure body schema.

Lives in db/crud because raw SQL belongs here — collectors must not hand-roll
SQL (repo rule). This is deliberately NOT the runtime's ``SerializedConnection``:
the profile collector runs in processes without a runtime (MCP server, CLI),
and a separate ``mode=ro`` URI connection (WAL-aware, unlike ``immutable=1``)
can never write or hold the runtime's lock.

Synchronous by design — callers run it via ``asyncio.to_thread``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

# Config pragmas worth hashing as facts — a changed journal_mode or page_size
# is a real configuration event.
FACT_PRAGMAS = (
    "journal_mode",
    "synchronous",
    "page_size",
    "mmap_size",
    "wal_autocheckpoint",
    "auto_vacuum",
    "cache_size",
    "user_version",
)


def read_pragma_facts(db_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(facts, metrics)`` for the SQLite database at ``db_path``.

    Facts: the FACT_PRAGMAS values. Metrics: freelist page count.
    Raises ``sqlite3.Error`` on connection/read failure — the caller owns
    degradation policy.
    """
    # timeout = SQLite busy-timeout: PRAGMA reads on a mode=ro connection can
    # still hit SQLITE_BUSY against the live writer's WAL checkpoints; 10s
    # rides out a checkpoint burst, then the caller degrades its section.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        facts: dict[str, Any] = {}
        for name in FACT_PRAGMAS:
            row = conn.execute(f"PRAGMA {name}").fetchone()  # noqa: S608 — fixed tuple of pragma names, no user input
            facts[name] = row[0] if row else None
        row = conn.execute("PRAGMA freelist_count").fetchone()
        metrics: dict[str, Any] = {"freelist_pages": row[0] if row else None}
        return facts, metrics
    finally:
        conn.close()
