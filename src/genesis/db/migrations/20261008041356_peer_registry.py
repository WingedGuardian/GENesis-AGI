"""Peer relationships, explicit authentication selection and bounded grants.

Self-contained, idempotent DDL; the runner owns transaction commit/rollback.
No credential values or automatic grants are stored here.
"""

_TABLES = (
    """
CREATE TABLE IF NOT EXISTS peer_settings (
    id INTEGER PRIMARY KEY CHECK(id=1),
    mode TEXT NOT NULL CHECK(mode IN ('disabled','fallback','sam')),
    sam_realm TEXT,
    service_url TEXT
)
    """,
    """
CREATE TABLE IF NOT EXISTS peers (
    peer_id TEXT PRIMARY KEY,
    epoch TEXT NOT NULL,
    same_owner INTEGER NOT NULL CHECK(same_owner IN (0,1)),
    daily_allowance INTEGER NOT NULL CHECK(daily_allowance > 0),
    token_name TEXT UNIQUE,
    sam_realm TEXT,
    sam_node TEXT,
    principal TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(sam_realm,sam_node)
)
    """,
    """
CREATE TABLE IF NOT EXISTS peer_grants (
    peer_id TEXT NOT NULL REFERENCES peers(peer_id),
    capability TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('allow','ask','deny')),
    PRIMARY KEY(peer_id,capability)
)
    """,
)


async def up(db):
    for ddl in _TABLES:
        await db.execute(ddl)
