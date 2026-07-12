"""CRUD for capability_shadow_events — the WS5 Discord capability-gate SHADOW store.

Observe-only: rows are gate DECISIONS + routing refs + a bounded content excerpt —
NEVER a hold/approval. Written best-effort from THREE processes: the genesis-server
(``outreach/pipeline._deliver``), the outreach MCP subprocess (``outreach_poll``),
and the discord-bot MCP subprocess (``send_reply``) — all against the same
``genesis.db`` (WAL + busy_timeout).

Subprocess writers do NOT run migrations, so ``record()`` guards on table existence
(cached per-process) and returns False if the table isn't there yet (the brief
pre-migration window). It NEVER creates the table — migration 0044 is the sole schema
authority, so there is no inline-DDL-vs-migration divergence surface.
"""

from __future__ import annotations

import aiosqlite

COLUMNS = (
    "id", "observed_at", "path", "channel", "cell_domain", "cell_verb",
    "cell_risk_class", "cell_state", "would_hold", "target", "content_preview",
    "content_hash",
)

# Per-process cache: once the table is confirmed present we stop re-checking. Only the
# TRUE result is cached — a missing table (pre-migration) is re-checked on every call so
# a subprocess writer self-heals the moment the server migration lands.
_table_verified = False


async def _table_available(db: aiosqlite.Connection) -> bool:
    global _table_verified
    if _table_verified:
        return True
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='capability_shadow_events'"
    )
    exists = await cursor.fetchone() is not None
    if exists:
        _table_verified = True
    return exists


async def record(
    db: aiosqlite.Connection,
    *,
    id: str,
    observed_at: str,
    path: str,
    channel: str,
    cell_domain: str,
    cell_verb: str,
    cell_risk_class: str,
    cell_state: str | None,
    would_hold: bool,
    target: str | None,
    content_preview: str | None,
    content_hash: str | None,
) -> bool:
    """Insert one shadow observation. Returns False (no-op) if the table doesn't exist
    yet (subprocess pre-migration window); never creates it."""
    if not await _table_available(db):
        return False
    await db.execute(
        "INSERT INTO capability_shadow_events "
        "(id, observed_at, path, channel, cell_domain, cell_verb, cell_risk_class, "
        "cell_state, would_hold, target, content_preview, content_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            id, observed_at, path, channel, cell_domain, cell_verb, cell_risk_class,
            cell_state, 1 if would_hold else 0, target, content_preview, content_hash,
        ),
    )
    await db.commit()
    return True


async def count(db: aiosqlite.Connection) -> int:
    cursor = await db.execute("SELECT COUNT(*) FROM capability_shadow_events")
    row = await cursor.fetchone()
    return row[0] if row else 0


async def list_recent(
    db: aiosqlite.Connection, *, limit: int = 100, offset: int = 0,
) -> list[dict]:
    """Recent shadow observations, newest first (for review). Assumes a Row factory."""
    lim = max(1, min(int(limit), 500))
    off = max(0, int(offset))
    cursor = await db.execute(
        "SELECT * FROM capability_shadow_events "
        "ORDER BY observed_at DESC LIMIT ? OFFSET ?",
        (lim, off),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def summary(db: aiosqlite.Connection) -> list[dict]:
    """Per-door / per-cell volume + would_hold breakdown (COUNTS only — no content).

    The observation deliverable: how much autonomous Discord traffic flows through each
    door and how much a live gate would hold, so the ENFORCE stage can size the posture.
    """
    cursor = await db.execute(
        "SELECT path, cell_domain, cell_verb, cell_risk_class, would_hold, "
        "COUNT(*) AS n FROM capability_shadow_events "
        "GROUP BY path, cell_domain, cell_verb, cell_risk_class, would_hold "
        "ORDER BY n DESC"
    )
    return [dict(r) for r in await cursor.fetchall()]


async def prune_capability_shadow_events(
    db: aiosqlite.Connection,
    *,
    older_than_days: int = 45,
    now: str,
) -> int:
    """Delete rows older than *older_than_days* relative to ISO ``now``.

    Retention for the unbounded shadow log (wired into ``disk_hygiene.sh``),
    mirroring ``crud.immunity_shadow.prune_immunity_shadow_events``. ``now`` is
    injected (never wall-clock here) so the cutover is deterministic and
    testable. No-ops (returns 0) before migration 0044's table exists; never
    creates it. Returns the number of rows deleted.
    """
    if not await _table_available(db):
        return 0
    cutoff = _iso_days_before(now, older_than_days)
    cursor = await db.execute(
        "DELETE FROM capability_shadow_events WHERE observed_at < ?", (cutoff,)
    )
    await db.commit()
    return cursor.rowcount if cursor.rowcount is not None else 0


def _iso_days_before(now_iso: str, days: int) -> str:
    """Return the ISO8601 timestamp *days* before ``now_iso``."""
    from datetime import datetime, timedelta

    dt = datetime.fromisoformat(now_iso)
    return (dt - timedelta(days=days)).isoformat()
