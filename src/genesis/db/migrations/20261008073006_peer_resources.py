"""Explicit immutable resource snapshots; migration runner owns commit."""


async def up(db):
    await db.execute("""
CREATE TABLE IF NOT EXISTS peer_resources (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
)
    """)
