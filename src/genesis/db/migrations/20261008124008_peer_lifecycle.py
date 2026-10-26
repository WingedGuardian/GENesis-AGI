"""Peer lifecycle state and logical consent; caller owns commit."""


async def up(db):
    await db.execute("""
CREATE TABLE IF NOT EXISTS peer_segments (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES peer_tasks(id),
    queue_id TEXT NOT NULL UNIQUE REFERENCES direct_session_queue(id),
    generation INTEGER NOT NULL CHECK(generation>=0),
    session_id TEXT REFERENCES cc_sessions(id),
    working_dir TEXT NOT NULL,
    deadline_at REAL NOT NULL,
    reserved_s INTEGER NOT NULL CHECK(reserved_s BETWEEN 1 AND 7200),
    charged_s INTEGER NOT NULL DEFAULT 0 CHECK(charged_s>=0 AND charged_s<=reserved_s),
    status TEXT NOT NULL CHECK(status IN ('prepared','running','drained','blocked')),
    started_at TEXT,
    completed_at REAL,
    execution_elapsed_s REAL CHECK(execution_elapsed_s>=0),
    ended_at TEXT,
    UNIQUE(task_id,generation)
)
    """)
    await db.execute("""
CREATE TABLE IF NOT EXISTS peer_task_runtime (
    task_id TEXT PRIMARY KEY REFERENCES peer_tasks(id),
    hold_reason TEXT CHECK(hold_reason IN ('approval','provider','reconciliation')),
    hold_segment_id TEXT REFERENCES peer_segments(id),
    hold_capability TEXT,
    hold_digest TEXT,
    approval_id TEXT REFERENCES peer_operation_approvals(approval_id),
    park_id TEXT REFERENCES cc_rate_limit_parks(id),
    safe_error TEXT
)
    """)
    await db.execute("""
CREATE TABLE IF NOT EXISTS peer_task_consents (
    approval_id TEXT PRIMARY KEY REFERENCES peer_operation_approvals(approval_id),
    task_id TEXT NOT NULL REFERENCES peer_tasks(id),
    peer_id TEXT NOT NULL REFERENCES peers(peer_id),
    epoch TEXT NOT NULL,
    capability TEXT NOT NULL,
    operation_digest TEXT NOT NULL,
    consumed_at TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    UNIQUE(task_id,capability,operation_digest)
)
    """)
    await db.execute("""
CREATE TABLE IF NOT EXISTS peer_operations (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES peer_tasks(id),
    capability TEXT NOT NULL,
    operation_digest TEXT NOT NULL,
    immutable_read INTEGER NOT NULL CHECK(immutable_read IN (0,1)),
    segment_id TEXT NOT NULL REFERENCES peer_segments(id),
    status TEXT NOT NULL CHECK(status IN ('prepared','executing','completed','unknown')),
    result_json TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id,capability,operation_digest),
    CHECK(status!='completed' OR result_json IS NOT NULL)
)
    """)
