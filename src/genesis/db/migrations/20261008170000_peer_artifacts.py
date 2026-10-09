"""Owned peer result snapshots; caller owns commit."""


async def up(db):
    await db.execute("""
CREATE TABLE IF NOT EXISTS peer_artifacts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE REFERENCES peer_tasks(id),
    segment_id TEXT NOT NULL UNIQUE REFERENCES peer_segments(id),
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes>=0),
    summary TEXT NOT NULL,
    tools_summary TEXT NOT NULL,
    created_at TEXT NOT NULL
)
    """)
