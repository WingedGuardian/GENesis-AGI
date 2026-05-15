"""Tests for memory_links.prune_weak()."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from genesis.db.crud import memory_links
from genesis.db.schema import create_all_tables


@pytest.fixture
async def db():
    import aiosqlite

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = None
    await create_all_tables(conn)
    yield conn
    await conn.close()


def _old_date(days_ago: int = 60) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _recent_date() -> str:
    return datetime.now(UTC).isoformat()


@pytest.mark.asyncio
async def test_prune_weak_deletes_old_weak_links(db):
    """Links with strength <= 0.3 and age > 30d are pruned."""
    await memory_links.create(
        db, source_id="a", target_id="b",
        link_type="related_to", strength=0.2,
        created_at=_old_date(60),
    )
    pruned = await memory_links.prune_weak(db, max_strength=0.3, min_age_days=30)
    assert pruned == 1


@pytest.mark.asyncio
async def test_prune_weak_spares_strong_links(db):
    """Links with strength > 0.3 are kept regardless of age."""
    await memory_links.create(
        db, source_id="a", target_id="b",
        link_type="supports", strength=0.8,
        created_at=_old_date(60),
    )
    pruned = await memory_links.prune_weak(db, max_strength=0.3, min_age_days=30)
    assert pruned == 0


@pytest.mark.asyncio
async def test_prune_weak_spares_recent_weak_links(db):
    """Weak links younger than min_age_days are kept."""
    await memory_links.create(
        db, source_id="a", target_id="b",
        link_type="related_to", strength=0.1,
        created_at=_recent_date(),
    )
    pruned = await memory_links.prune_weak(db, max_strength=0.3, min_age_days=30)
    assert pruned == 0


@pytest.mark.asyncio
async def test_prune_weak_returns_zero_on_empty(db):
    """No links to prune returns 0."""
    pruned = await memory_links.prune_weak(db)
    assert pruned == 0


@pytest.mark.asyncio
async def test_prune_weak_boundary_strength(db):
    """Links at exactly max_strength are pruned (<=, not <)."""
    await memory_links.create(
        db, source_id="a", target_id="b",
        link_type="related_to", strength=0.3,
        created_at=_old_date(60),
    )
    pruned = await memory_links.prune_weak(db, max_strength=0.3, min_age_days=30)
    assert pruned == 1
