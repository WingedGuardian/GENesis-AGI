"""Tests for the embedding-backlog degradation alert (_check_embedding_backlog).

A pile of memories stuck at ``embedding_status='failed'`` is permanently non-
vector-searchable (keyword-only) and invisible to the rate-based embedding-
failure alert (the outage that created them is over). This probe counts the
standing backlog off ``memory_metadata`` (the durable mirror — the
``pending_embeddings`` queue is TTL-reaped) and surfaces it HYBRID: a modest
pile records a non-paging ``high`` observation (dashboard only); a large pile
records a ``critical`` one that the critical-observations job pages to Telegram.

Determinism: the cooldown globals are reset around every test and
``time.monotonic`` is pinned, so nothing depends on the wall clock.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.awareness import loop
from genesis.db.crud import observations as obs_crud
from genesis.db.schema import create_all_tables


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    loop._last_embed_backlog_alert_at = 0.0
    loop._last_embed_backlog_band = ""
    # Pin monotonic so the band-guarded cooldown is fully deterministic.
    monkeypatch.setattr(loop.time, "monotonic", lambda: 1000.0)
    yield
    loop._last_embed_backlog_alert_at = 0.0
    loop._last_embed_backlog_band = ""


async def _seed(db, status: str, n: int, *, start: int = 0) -> None:
    """Insert ``n`` memory_metadata rows with the given embedding_status."""
    rows = [
        (f"{status}-{start + i}", "2026-01-01T00:00:00", "episodic_memory", 0.5, status)
        for i in range(n)
    ]
    await db.executemany(
        "INSERT INTO memory_metadata "
        "(memory_id, created_at, collection, confidence, embedding_status) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    await db.commit()


async def _unresolved(db) -> list[aiosqlite.Row]:
    cur = await db.execute(
        "SELECT priority, content_hash FROM observations "
        "WHERE source='embedding_backlog_monitor' AND type='infrastructure_alert' "
        "AND resolved=0"
    )
    return list(await cur.fetchall())


async def _total(db) -> int:
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM observations WHERE source='embedding_backlog_monitor'"
    )
    return rows[0][0]


# -- Pure band mapping (no DB) -------------------------------------------------


def test_band_boundaries():
    assert loop._embed_backlog_band(50) == "50-199"
    assert loop._embed_backlog_band(199) == "50-199"
    assert loop._embed_backlog_band(200) == "200-999"
    assert loop._embed_backlog_band(999) == "200-999"
    assert loop._embed_backlog_band(1000) == "1000-4999"
    assert loop._embed_backlog_band(4999) == "1000-4999"
    assert loop._embed_backlog_band(5000) == "5000+"


# -- Threshold / priority matrix ----------------------------------------------


@pytest.mark.asyncio
async def test_below_low_no_alert_and_resolves(db):
    """failed < LOW → no observation; resolve path runs (harmless no-op)."""
    await _seed(db, "failed", loop._EMBED_BACKLOG_LOW - 1)
    # fts5_only is a healthy permanent state and must never count toward failed.
    await _seed(db, "fts5_only", 900, start=10_000)
    await loop._check_embedding_backlog(db)
    assert await _total(db) == 0


@pytest.mark.asyncio
async def test_modest_backlog_is_high_and_does_not_page(db):
    """LOW <= failed < HIGH → one 'high' row, INVISIBLE to the critical-obs poll."""
    await _seed(db, "failed", loop._EMBED_BACKLOG_LOW + 10)
    await loop._check_embedding_backlog(db)

    rows = await _unresolved(db)
    assert len(rows) == 1
    assert rows[0]["priority"] == "high"

    # The critical-observations job (Telegram) polls priority_filter=("critical",)
    # excluding INTERNAL_OBS_TYPES — a 'high' row must NOT be returned.
    surfaced = await obs_crud.get_unsurfaced(
        db,
        priority_filter=("critical",),
        exclude_types=tuple(obs_crud.INTERNAL_OBS_TYPES),
    )
    assert not any(o["source"] == "embedding_backlog_monitor" for o in surfaced)


@pytest.mark.asyncio
async def test_large_backlog_is_critical_and_pages(db):
    """failed >= HIGH → one 'critical' row, VISIBLE to the critical-obs poll."""
    await _seed(db, "failed", loop._EMBED_BACKLOG_HIGH + 5)
    await loop._check_embedding_backlog(db)

    rows = await _unresolved(db)
    assert len(rows) == 1
    assert rows[0]["priority"] == "critical"

    surfaced = await obs_crud.get_unsurfaced(
        db,
        priority_filter=("critical",),
        exclude_types=tuple(obs_crud.INTERNAL_OBS_TYPES),
    )
    assert any(o["source"] == "embedding_backlog_monitor" for o in surfaced)


@pytest.mark.asyncio
async def test_same_band_dedups_across_restart(db):
    """A repeat in the same band produces no 2nd row even when the in-memory
    cooldown is cleared (simulating a restart) — skip_if_duplicate is DB-backed."""
    await _seed(db, "failed", loop._EMBED_BACKLOG_LOW + 10)
    await loop._check_embedding_backlog(db)
    assert await _total(db) == 1

    # Simulate a restart: cooldown globals lost, DB row survives.
    loop._last_embed_backlog_alert_at = 0.0
    loop._last_embed_backlog_band = ""
    await loop._check_embedding_backlog(db)
    assert await _total(db) == 1  # DB dedup held


@pytest.mark.asyncio
async def test_band_escalation_high_to_critical_supersedes(db):
    """A worsening band transition (high → critical) writes the critical row
    within the same cooldown window (escalation bypass) AND supersedes the stale
    'high' row, so exactly one alert (the current band) stays active."""
    await _seed(db, "failed", loop._EMBED_BACKLOG_LOW + 10)
    await loop._check_embedding_backlog(db)

    # Grow the pile past HIGH — new (critical) band, same cooldown window.
    await _seed(db, "failed", loop._EMBED_BACKLOG_HIGH, start=500_000)
    await loop._check_embedding_backlog(db)

    rows = await _unresolved(db)
    assert [r["priority"] for r in rows] == ["critical"]  # high superseded
    assert await _total(db) == 2  # one resolved high + one active critical


@pytest.mark.asyncio
async def test_partial_recovery_critical_to_high_supersedes(db):
    """A PARTIAL recovery (critical → high, still >= LOW) supersedes the peak
    'critical' row so it does not linger, leaving only the current 'high' row —
    which does NOT page. Regression for the fluctuating-metric case."""
    await _seed(db, "failed", loop._EMBED_BACKLOG_HIGH + 5)
    await loop._check_embedding_backlog(db)
    assert [r["priority"] for r in await _unresolved(db)] == ["critical"]

    # Recover to a modest (high-band) pile — still above LOW.
    await db.execute("DELETE FROM memory_metadata WHERE embedding_status='failed'")
    await db.commit()
    await _seed(db, "failed", loop._EMBED_BACKLOG_LOW + 10, start=900_000)
    await loop._check_embedding_backlog(db)

    rows = await _unresolved(db)
    assert [r["priority"] for r in rows] == ["high"]  # critical superseded
    # The lingering critical must NOT still be visible to the paging poll.
    surfaced = await obs_crud.get_unsurfaced(
        db,
        priority_filter=("critical",),
        exclude_types=tuple(obs_crud.INTERNAL_OBS_TYPES),
    )
    assert not any(o["source"] == "embedding_backlog_monitor" for o in surfaced)


@pytest.mark.asyncio
async def test_recovery_resolves_and_reclears_cooldown(db):
    """Backlog drops below LOW → the standing alert resolves and the cooldown
    globals reset so a fresh spike re-alerts cleanly."""
    await _seed(db, "failed", loop._EMBED_BACKLOG_HIGH + 5)
    await loop._check_embedding_backlog(db)
    assert len(await _unresolved(db)) == 1

    # Drain: delete the failed rows, re-run — should resolve.
    await db.execute("DELETE FROM memory_metadata WHERE embedding_status='failed'")
    await db.commit()
    await loop._check_embedding_backlog(db)

    assert await _unresolved(db) == []
    assert loop._last_embed_backlog_alert_at == 0.0
    assert loop._last_embed_backlog_band == ""


@pytest.mark.asyncio
async def test_pending_is_context_not_a_trigger(db):
    """A pile of 'pending' (self-healing) with zero 'failed' must not alert —
    pending spikes are the rate-based alert's job, not this depth probe's."""
    await _seed(db, "pending", loop._EMBED_BACKLOG_HIGH + 100)
    await loop._check_embedding_backlog(db)
    assert await _total(db) == 0


@pytest.mark.asyncio
async def test_none_db_is_noop():
    await loop._check_embedding_backlog(None)  # must not raise


@pytest.mark.asyncio
async def test_body_swallows_errors(db, monkeypatch):
    """A failure inside the probe never raises into the tick."""

    async def _boom(_db):
        raise RuntimeError("reader down")

    monkeypatch.setattr("genesis.db.crud.memory.embedding_status_counts", _boom)
    await loop._check_embedding_backlog(db)  # swallowed
