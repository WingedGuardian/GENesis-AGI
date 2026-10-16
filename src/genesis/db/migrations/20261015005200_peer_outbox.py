"""Retained addressed messages; existing SQLite backup owns retention."""

# Snapshot this migration. Later canonical schema changes need their own ID.
_TABLE = """
CREATE TABLE IF NOT EXISTS peer_outbox (
    peer_id TEXT NOT NULL REFERENCES peers(peer_id),
    epoch TEXT NOT NULL,
    message_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    acknowledged_at TEXT,
    PRIMARY KEY(peer_id,epoch,message_id)
)
"""
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_peer_outbox_collect ON peer_outbox(peer_id, epoch, created_at, message_id) WHERE acknowledged_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_peer_outbox_context ON peer_outbox(peer_id, epoch, context_id, created_at, message_id)",
)


async def up(db):
    await db.execute(_TABLE)
    for ddl in _INDEXES:
        await db.execute(ddl)
