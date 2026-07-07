"""Per-UTC-day OMI snapshot-db writer.

Writes normalized OMI utterances into ``omi_YYYYMMDD.db`` files under the SAME
snapshots dir the reader/GC/runner are pinned to, using the EXACT 10-column
``ambient_transcripts`` schema so a home ``SnapshotSource`` consumes them unchanged.
The filename matches the GC regex ``^omi_\\d{8}\\.db$`` and its bare id
(``omi_YYYYMMDD``) is what ``runner._snapshot_id_from_path`` yields and the dashboard
reveal reconstructs.

Connections are opened PER CALL, never held: a connection kept open past the
day-rollover GC deletion would leave unlinked-open files + orphan ``-wal``/``-shm``.
Writes are milliseconds at the legit ≤~1 req/s rate. The day-db is append-only —
row-level dedup happens upstream in the ingest orchestrator via
``omi_state.seen_segments`` (the schema has no segment_id column, by design, to stay
byte-compatible with home snapshots).
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from genesis.attention.omi_normalize import NormalizedRow
from genesis.attention.snapshot import _DEFAULT_DEST as SNAPSHOTS_DIR

# Byte-compatible with a home ambient_*.db snapshot: same 10 columns, same order,
# same types/defaults (verified against a live snapshot's PRAGMA table_info).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS ambient_transcripts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    text          TEXT NOT NULL,
    duration_s    REAL,
    speaker_label TEXT,
    provenance    TEXT NOT NULL DEFAULT 'ambient_overheard',
    source        TEXT,
    meta          TEXT,
    is_user       INTEGER,
    speaker_name  TEXT
);
CREATE INDEX IF NOT EXISTS idx_ambient_ts ON ambient_transcripts(ts);
"""


def snapshot_id_for(recv_ts: float) -> str:
    """Bare snapshot id for a receive time: ``omi_YYYYMMDD`` (UTC day)."""
    return "omi_" + datetime.fromtimestamp(recv_ts, UTC).strftime("%Y%m%d")


def day_db_path(recv_ts: float, *, snapshots_dir=None) -> Path:
    """Absolute path of the day-db file for ``recv_ts``."""
    base = Path(snapshots_dir).expanduser() if snapshots_dir else Path(SNAPSHOTS_DIR).expanduser()
    return base / f"{snapshot_id_for(recv_ts)}.db"


def insert_rows(rows: list[NormalizedRow], *, recv_ts: float, snapshots_dir=None) -> int:
    """Append normalized rows to the day-db for ``recv_ts``; return rows written.

    A fresh connection is opened and closed here (never held across the GC
    boundary). Empty input is a no-op — no file is created.
    """
    rows = list(rows)
    if not rows:
        return 0
    path = day_db_path(recv_ts, snapshots_dir=snapshots_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA)
        conn.executemany(
            "INSERT INTO ambient_transcripts "
            "(ts, text, duration_s, speaker_label, provenance, source, meta, is_user, speaker_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (r.ts, r.text, r.duration_s, r.speaker_label, r.provenance,
                 r.source, r.meta, r.is_user, r.speaker_name)
                for r in rows
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return len(rows)
