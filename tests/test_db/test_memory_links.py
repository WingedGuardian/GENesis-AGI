"""Tests for memory_links CRUD."""

from __future__ import annotations

from sqlite3 import IntegrityError

import pytest

from genesis.db.crud import memory_links


async def test_create_and_retrieve(db):
    pk = await memory_links.create(
        db, source_id="a", target_id="b", link_type="supports", created_at="2026-01-01",
    )
    assert pk == ("a", "b")
    links = await memory_links.get_links_for(db, "a")
    assert len(links) == 1
    assert links[0]["link_type"] == "supports"
    assert links[0]["strength"] == 0.5


async def test_count_links(db):
    await memory_links.create(
        db, source_id="c1", target_id="c2", link_type="extends", created_at="2026-01-01",
    )
    await memory_links.create(
        db, source_id="c3", target_id="c1", link_type="contradicts", created_at="2026-01-01",
    )
    assert await memory_links.count_links(db, "c1") == 2


async def test_get_links_for_both_directions(db):
    await memory_links.create(
        db, source_id="d1", target_id="d2", link_type="elaborates", created_at="2026-01-01",
    )
    await memory_links.create(
        db, source_id="d3", target_id="d1", link_type="supports", created_at="2026-01-01",
    )
    links = await memory_links.get_links_for(db, "d1")
    assert len(links) == 2


async def test_get_bidirectional_same_as_get_links_for(db):
    await memory_links.create(
        db, source_id="e1", target_id="e2", link_type="supports", created_at="2026-01-01",
    )
    links = await memory_links.get_bidirectional(db, "e1")
    assert len(links) == 1


async def test_delete(db):
    await memory_links.create(
        db, source_id="f1", target_id="f2", link_type="extends", created_at="2026-01-01",
    )
    assert await memory_links.delete(db, source_id="f1", target_id="f2") is True
    assert await memory_links.delete(db, source_id="f1", target_id="f2") is False


async def test_delete_nonexistent(db):
    assert await memory_links.delete(db, source_id="nope1", target_id="nope2") is False


async def test_duplicate_pk_raises(db):
    await memory_links.create(
        db, source_id="g1", target_id="g2", link_type="supports", created_at="2026-01-01",
    )
    with pytest.raises(IntegrityError):
        await memory_links.create(
            db, source_id="g1", target_id="g2", link_type="contradicts", created_at="2026-01-02",
        )


# --- Batch link count tests ---


async def test_batch_link_counts(db):
    """batch_link_counts returns (total, inbound) per memory_id."""
    # c1 -> c2, c1 -> c3, c3 -> c2
    await memory_links.create(
        db, source_id="c1", target_id="c2", link_type="supports", created_at="2026-01-01",
    )
    await memory_links.create(
        db, source_id="c1", target_id="c3", link_type="extends", created_at="2026-01-01",
    )
    await memory_links.create(
        db, source_id="c3", target_id="c2", link_type="supports", created_at="2026-01-01",
    )

    counts = await memory_links.batch_link_counts(db, ["c1", "c2", "c3"])
    # c1: 2 outbound, 0 inbound → total=2, inbound=0
    assert counts["c1"] == (2, 0)
    # c2: 0 outbound, 2 inbound → total=2, inbound=2
    assert counts["c2"] == (2, 2)
    # c3: 1 outbound (c3→c2) + 1 inbound (c1→c3) → total=2, inbound=1
    assert counts["c3"] == (2, 1)


async def test_batch_link_counts_empty(db):
    """Empty input returns empty dict."""
    counts = await memory_links.batch_link_counts(db, [])
    assert counts == {}


async def test_batch_link_counts_no_links(db):
    """Memories with no links get (0, 0)."""
    counts = await memory_links.batch_link_counts(db, ["orphan1", "orphan2"])
    assert counts["orphan1"] == (0, 0)
    assert counts["orphan2"] == (0, 0)


async def test_inter_candidate_links(db):
    """inter_candidate_links returns only edges within the candidate set."""
    await memory_links.create(
        db, source_id="a", target_id="b", link_type="supports", created_at="2026-01-01",
    )
    await memory_links.create(
        db, source_id="b", target_id="c", link_type="extends", created_at="2026-01-01",
    )
    await memory_links.create(
        db, source_id="d", target_id="e", link_type="supports", created_at="2026-01-01",
    )

    edges = await memory_links.inter_candidate_links(db, ["a", "b", "c"])
    assert ("a", "b") in edges
    assert ("b", "c") in edges
    assert ("d", "e") not in edges  # d not in candidate set
    assert len(edges) == 2


async def test_inter_candidate_links_empty(db):
    """Empty candidate set returns empty list."""
    edges = await memory_links.inter_candidate_links(db, [])
    assert edges == []
