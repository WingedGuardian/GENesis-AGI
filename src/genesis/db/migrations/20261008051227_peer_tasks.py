"""Peer receipts, quota and durable queue associations; runner owns commit."""


async def up(db):
    await db.execute("""
CREATE TABLE IF NOT EXISTS peer_tasks (
    id TEXT PRIMARY KEY,
    peer_id TEXT NOT NULL REFERENCES peers(peer_id),
    epoch TEXT NOT NULL,
    context_id TEXT NOT NULL,
    message_json TEXT NOT NULL,
    grants_json TEXT NOT NULL,
    grant_revision INTEGER NOT NULL,
    queue_id TEXT NOT NULL UNIQUE REFERENCES direct_session_queue(id) DEFERRABLE INITIALLY DEFERRED,
    state TEXT NOT NULL DEFAULT 'submitted' CHECK(state IN ('submitted','working','input_required','completed','failed','canceled','rejected')),
    slot_reserved INTEGER NOT NULL DEFAULT 1 CHECK(slot_reserved IN (0,1)),
    generation INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
    work_limit_s INTEGER NOT NULL CHECK(work_limit_s BETWEEN 1 AND 7200),
    work_elapsed_s INTEGER NOT NULL DEFAULT 0 CHECK(work_elapsed_s>=0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    expires_at TEXT NOT NULL,
    CHECK(state NOT IN ('completed','failed','canceled','rejected') OR slot_reserved=0)
)
    """)
    await db.execute("""
CREATE TABLE IF NOT EXISTS peer_receipts (
    peer_id TEXT NOT NULL REFERENCES peers(peer_id),
    epoch TEXT NOT NULL,
    message_id TEXT NOT NULL,
    intent_digest TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES peer_tasks(id),
    PRIMARY KEY(peer_id,epoch,message_id)
)
    """)
    await db.execute("""
CREATE TABLE IF NOT EXISTS peer_daily_admissions (
    peer_id TEXT NOT NULL REFERENCES peers(peer_id),
    epoch TEXT NOT NULL,
    utc_day TEXT NOT NULL,
    admissions INTEGER NOT NULL CHECK(admissions>0),
    PRIMARY KEY(peer_id,epoch,utc_day)
)
    """)
