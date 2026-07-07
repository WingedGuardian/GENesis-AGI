"""Advisory state for the OMI ingest path: delivery/segment dedup + per-uid anchor.

Lives at ``~/.genesis/attention/omi_state.db`` — deliberately OUTSIDE the
``snapshots/`` tree (that dir holds only snapshot-shaped files the GC/reveal/runner
scan). The store is ADVISORY: there is no cross-file atomicity with the day-dbs
(SQLite can't provide it under WAL anyway), and losing it costs at most one
duplicate row or ~2s of timestamp jitter on a single batch — both engine-tolerable.

Three tables:
  * ``idempotency_keys`` — OMI reuses an ``Idempotency-Key`` header across a batch's
    retries; this is the dominant duplicate source. Short TTL (retries fire within
    seconds).
  * ``seen_segments`` — per-segment uuid dedup, a 7-day backstop for anything the
    key layer misses.
  * ``anchor`` — the per-uid ``(epoch0, max_end)`` reconstructed wall-clock anchor.

All age logic is driven by a caller-supplied ``now`` so callers stay wall-clock
independent (and testable). Access is serialized by the ingest service's single
data-path lock, so one shared connection is safe.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

IDEMPOTENCY_TTL_S = 600  # 10 min — OMI reuses the key only across a batch's retries
SEEN_SEGMENTS_TTL_S = 7 * 86400  # 7 days — segment-uuid dedup horizon

DEFAULT_STATE_PATH = Path("~/.genesis/attention/omi_state.db").expanduser()


class OmiState:
    """SQLite-backed advisory dedup + anchor store. One connection, serialized use."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path).expanduser() if path is not None else DEFAULT_STATE_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                key     TEXT PRIMARY KEY,
                seen_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS seen_segments (
                segment_id TEXT PRIMARY KEY,
                seen_at    REAL NOT NULL
            );
            -- Anchor decision reads ONLY epoch0; max_end (monotonic within a kept
            -- anchor, reset on re-anchor) and updated_at are DIAGNOSTIC — a record
            -- of how far the current conversation got, not inputs to decide_anchor
            -- (which uses each batch's own max-end). Kept for debugging the anchor.
            CREATE TABLE IF NOT EXISTS anchor (
                uid        TEXT PRIMARY KEY,
                epoch0     REAL NOT NULL,
                max_end    REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )
        self._conn.commit()

    # ── idempotency (delivery-level) dedup ─────────────────────────────────
    def is_duplicate_delivery(self, key: str | None, *, now: float) -> bool:
        """True if ``key`` was already seen (fresh). Records it on first sight.

        A missing/empty key is never a duplicate (we cannot dedup it — bias to
        insertion). Prunes expired keys on every call so the table stays bounded.
        """
        if not key:
            return False
        self._conn.execute(
            "DELETE FROM idempotency_keys WHERE seen_at < ?", (now - IDEMPOTENCY_TTL_S,)
        )
        cur = self._conn.execute("SELECT 1 FROM idempotency_keys WHERE key=?", (key,))
        if cur.fetchone() is not None:
            self._conn.commit()
            return True
        self._conn.execute(
            "INSERT OR REPLACE INTO idempotency_keys(key, seen_at) VALUES(?, ?)", (key, now)
        )
        self._conn.commit()
        return False

    # ── segment-uuid dedup ─────────────────────────────────────────────────
    def seen_segment_ids(self, ids, *, now: float) -> set[str]:
        """Return the subset of ``ids`` already recorded and still fresh (read-only).

        ``None``/empty ids are ignored — a segment with no uuid can't be deduped, so
        it is treated as unseen (inserted). Expired entries read as unseen.
        """
        wanted = [i for i in ids if i]
        if not wanted:
            return set()
        placeholders = ",".join("?" for _ in wanted)
        cur = self._conn.execute(
            f"SELECT segment_id FROM seen_segments "  # noqa: S608 — placeholders only
            f"WHERE segment_id IN ({placeholders}) AND seen_at > ?",
            (*wanted, now - SEEN_SEGMENTS_TTL_S),
        )
        return {r[0] for r in cur.fetchall()}

    def record_segment_ids(self, ids, *, now: float) -> None:
        """Record segment uuids as seen (idempotent) and prune expired entries."""
        wanted = [i for i in ids if i]
        if wanted:
            self._conn.executemany(
                "INSERT OR REPLACE INTO seen_segments(segment_id, seen_at) VALUES(?, ?)",
                [(i, now) for i in wanted],
            )
        self._conn.execute(
            "DELETE FROM seen_segments WHERE seen_at < ?", (now - SEEN_SEGMENTS_TTL_S,)
        )
        self._conn.commit()

    # ── per-uid anchor ─────────────────────────────────────────────────────
    def get_anchor(self, uid: str) -> tuple[float, float] | None:
        """Return ``(epoch0, max_end)`` for ``uid``, or ``None`` if unanchored."""
        cur = self._conn.execute("SELECT epoch0, max_end FROM anchor WHERE uid=?", (uid,))
        row = cur.fetchone()
        return (row[0], row[1]) if row else None

    def set_anchor(self, uid: str, epoch0: float, max_end: float, *, now: float) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO anchor(uid, epoch0, max_end, updated_at) VALUES(?, ?, ?, ?)",
            (uid, epoch0, max_end, now),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
