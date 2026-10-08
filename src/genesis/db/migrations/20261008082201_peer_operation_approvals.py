"""Durable peer operation approval intents; migration runner owns commit."""


async def up(db):
    await db.execute("""
CREATE TABLE IF NOT EXISTS peer_operation_approvals (
    approval_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES peer_tasks(id),
    peer_id TEXT NOT NULL REFERENCES peers(peer_id),
    epoch TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    capability TEXT NOT NULL,
    operation_digest TEXT NOT NULL,
    description TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    notification_attempts INTEGER NOT NULL DEFAULT 0,
    notified_at TEXT,
    notification_error TEXT,
    UNIQUE(task_id,generation,capability,operation_digest)
)
    """)
