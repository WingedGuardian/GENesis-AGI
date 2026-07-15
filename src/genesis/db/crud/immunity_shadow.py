"""CRUD for immunity_shadow_events — the WS-3 B1 immunity-gate SHADOW store.

Observe-only: rows are gate DECISIONS + provenance refs (which gate, at which
site, for which origin_class) — NEVER recalled content, NEVER a block. In
shadow mode the recalled item still reaches the prompt exactly as before (it
was already wrapped by ``wrap_external_recall``); this store only records that
a live gate WOULD have acted, so the ENFORCE stage (B4) can size the blast
radius first.

Written best-effort from TWO writer kinds against the same ``genesis.db``
(WAL + busy_timeout):

- the genesis-server async runtime (recall/inject sites) → :func:`record`
  (aiosqlite);
- the ``UserPromptSubmit`` proactive-memory hook, a foreground sync process →
  :func:`record_sync` (stdlib ``sqlite3``).

Neither writer runs migrations, so both record paths guard on table existence
(cached per-process) and return ``False`` if the table isn't there yet (the
brief pre-migration window). They NEVER create the table — migration 0055 is
the sole schema authority, mirrored in ``db/schema/_tables.py`` for the
fresh-DB path, so there is no inline-DDL-vs-migration divergence surface.
"""

from __future__ import annotations

import sqlite3

import aiosqlite

COLUMNS = (
    "id",
    "observed_at",
    "gate",
    "mode",
    "origin_class",
    "would_block",
    "source_kind",
    "source_ref",
    "detail",
    "process",
)

_INSERT = (
    "INSERT INTO immunity_shadow_events "
    "(id, observed_at, gate, mode, origin_class, would_block, source_kind, "
    "source_ref, detail, process) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_TABLE_PROBE = "SELECT name FROM sqlite_master WHERE type='table' AND name='immunity_shadow_events'"

# Per-process existence caches — one per connection flavor. Only the TRUE
# result is cached; a missing table (pre-migration) is re-checked every call so
# a subprocess writer self-heals the moment the server migration lands.
_table_verified = False
_table_verified_sync = False


def _row_values(kw: dict) -> tuple:
    return (
        kw["id"],
        kw["observed_at"],
        kw["gate"],
        kw["mode"],
        kw["origin_class"],
        1 if kw["would_block"] else 0,
        kw["source_kind"],
        kw["source_ref"],
        kw["detail"],
        kw["process"],
    )


# ── async writer (genesis-server runtime) ──────────────────────────────────
async def _table_available(db: aiosqlite.Connection) -> bool:
    global _table_verified
    if _table_verified:
        return True
    cursor = await db.execute(_TABLE_PROBE)
    exists = await cursor.fetchone() is not None
    if exists:
        _table_verified = True
    return exists


async def record(
    db: aiosqlite.Connection,
    *,
    id: str,
    observed_at: str,
    gate: str,
    mode: str,
    origin_class: str,
    would_block: bool,
    source_kind: str | None,
    source_ref: str | None,
    detail: str | None,
    process: str | None,
) -> bool:
    """Insert one shadow observation. Returns False (no-op) if the table doesn't
    exist yet (subprocess pre-migration window); never creates it."""
    if not await _table_available(db):
        return False
    await db.execute(_INSERT, _row_values(locals()))
    await db.commit()
    return True


# ── sync writer (UserPromptSubmit proactive-memory hook) ────────────────────
def _table_available_sync(conn: sqlite3.Connection) -> bool:
    global _table_verified_sync
    if _table_verified_sync:
        return True
    exists = conn.execute(_TABLE_PROBE).fetchone() is not None
    if exists:
        _table_verified_sync = True
    return exists


def record_sync(
    conn: sqlite3.Connection,
    *,
    id: str,
    observed_at: str,
    gate: str,
    mode: str,
    origin_class: str,
    would_block: bool,
    source_kind: str | None,
    source_ref: str | None,
    detail: str | None,
    process: str | None,
) -> bool:
    """Sync sibling of :func:`record` for the stdlib-``sqlite3`` proactive hook.
    Same table-absent guard; same columns; never creates the table."""
    if not _table_available_sync(conn):
        return False
    conn.execute(_INSERT, _row_values(locals()))
    conn.commit()
    return True


# ── read / aggregation / retention ─────────────────────────────────────────
async def count(db: aiosqlite.Connection) -> int:
    cursor = await db.execute("SELECT COUNT(*) FROM immunity_shadow_events")
    row = await cursor.fetchone()
    return row[0] if row else 0


async def count_would_block(
    db: aiosqlite.Connection,
    *,
    gate: str,
    since: str,
) -> int:
    """Would-block rows for *gate* at/after ISO ``since`` (observability)."""
    cursor = await db.execute(
        "SELECT COUNT(*) FROM immunity_shadow_events "
        "WHERE gate = ? AND would_block = 1 AND observed_at >= ?",
        (gate, since),
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def count_enforced_interventions(
    db: aiosqlite.Connection,
    *,
    gate: str,
    since: str,
) -> int:
    """Rows for *gate* where the gate ACTED — dropped items or refused writes —
    at/after ISO ``since``. THE auto-demote signal (B4, Codex round-6 on
    #1048): wrap-only observation rows (allowed external content that was
    delimited and returned, e.g. an explicit memory_recall of KB) must NEVER
    count toward demotion, or normal research usage would flip the gate back
    to shadow. Detail markers: ``refused`` (gate-3 evidence/state refusals),
    ``enforced_drops`` (gate-4 pushed-surface drops)."""
    cursor = await db.execute(
        "SELECT COUNT(*) FROM immunity_shadow_events "
        "WHERE gate = ? AND mode = 'enforce' AND observed_at >= ? "
        "AND (json_extract(detail, '$.refused') = 1 "
        "     OR COALESCE(json_extract(detail, '$.enforced_drops'), 0) > 0)",
        (gate, since),
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def list_recent(
    db: aiosqlite.Connection,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Recent shadow observations, newest first (for review). Assumes a Row factory."""
    lim = max(1, min(int(limit), 500))
    off = max(0, int(offset))
    cursor = await db.execute(
        "SELECT * FROM immunity_shadow_events ORDER BY observed_at DESC LIMIT ? OFFSET ?",
        (lim, off),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def summary(
    db: aiosqlite.Connection,
    *,
    since: str | None = None,
) -> list[dict]:
    """Per-gate / per-site volume (COUNTS only — no content).

    The observation deliverable: how much external content reaches each
    action-capable inject site, so the ENFORCE stage can size the posture.
    Optionally bounded to rows at/after ISO ``since``.
    """
    where = "WHERE observed_at >= ?" if since else ""
    params = (since,) if since else ()
    cursor = await db.execute(
        "SELECT gate, source_ref, would_block, COUNT(*) AS n "
        f"FROM immunity_shadow_events {where} "
        "GROUP BY gate, source_ref, would_block "
        "ORDER BY n DESC",
        params,
    )
    return [dict(r) for r in await cursor.fetchall()]


async def prune_immunity_shadow_events(
    db: aiosqlite.Connection,
    *,
    older_than_days: int = 45,
    now: str,
) -> int:
    """Delete rows older than *older_than_days* relative to ISO ``now``.

    Retention for the unbounded shadow log (wired into ``disk_hygiene.sh``).
    ``now`` is injected (never wall-clock here) so the cutover is deterministic
    and testable. Returns the number of rows deleted.
    """
    if not await _table_available(db):
        return 0
    cutoff = _iso_days_before(now, older_than_days)
    cursor = await db.execute("DELETE FROM immunity_shadow_events WHERE observed_at < ?", (cutoff,))
    await db.commit()
    return cursor.rowcount if cursor.rowcount is not None else 0


def _iso_days_before(now_iso: str, days: int) -> str:
    """Return the ISO8601 UTC timestamp *days* before ``now_iso``."""
    from datetime import datetime, timedelta

    dt = datetime.fromisoformat(now_iso)
    return (dt - timedelta(days=days)).isoformat()
