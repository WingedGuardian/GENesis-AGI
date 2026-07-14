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


async def test_same_triplet_raises(db):
    """The same (source, target, link_type) twice still violates the PK."""
    await memory_links.create(
        db, source_id="g1", target_id="g2", link_type="supports", created_at="2026-01-01",
    )
    with pytest.raises(IntegrityError):
        await memory_links.create(
            db, source_id="g1", target_id="g2", link_type="supports", created_at="2026-01-02",
        )


async def test_different_link_type_same_pair_persists(db):
    """DLI-04: a 2nd link of a DIFFERENT type between the same pair must persist.

    The old PK (source_id, target_id) silently dropped this; the fixed PK
    (source_id, target_id, link_type) keeps both edges.
    """
    await memory_links.create(
        db, source_id="g1", target_id="g2", link_type="supports", created_at="2026-01-01",
    )
    await memory_links.create(
        db, source_id="g1", target_id="g2", link_type="contradicts", created_at="2026-01-02",
    )
    links = await memory_links.get_links_for(db, "g1")
    types = sorted(link["link_type"] for link in links)
    assert types == ["contradicts", "supports"]


async def test_inter_candidate_links_dedupes_multi_type_pairs(db):
    """Adjacency boost is binary per pair — a multi-type pair counts once."""
    await memory_links.create(
        db, source_id="a", target_id="b", link_type="supports", created_at="2026-01-01",
    )
    await memory_links.create(
        db, source_id="a", target_id="b", link_type="contradicts", created_at="2026-01-02",
    )
    edges = await memory_links.inter_candidate_links(db, ["a", "b"])
    assert edges == [("a", "b")]  # deduped, not [("a","b"), ("a","b")]


async def test_delete_by_link_type(db):
    """delete(link_type=...) removes only that type; without it, all types go."""
    await memory_links.create(
        db, source_id="a", target_id="b", link_type="supports", created_at="2026-01-01",
    )
    await memory_links.create(
        db, source_id="a", target_id="b", link_type="contradicts", created_at="2026-01-02",
    )
    # Targeted delete removes only the typed link.
    assert await memory_links.delete(
        db, source_id="a", target_id="b", link_type="supports",
    ) is True
    remaining = await memory_links.get_links_for(db, "a")
    assert [link["link_type"] for link in remaining] == ["contradicts"]
    # Untyped delete removes whatever is left for the pair.
    assert await memory_links.delete(db, source_id="a", target_id="b") is True
    assert await memory_links.get_links_for(db, "a") == []


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


async def test_neighbors_of_dedupes_directions_and_types(db):
    """neighbors_of collapses multi-type rows + both directions to one row per
    neighbor with MAX(strength), strongest first."""
    # seed <-> n1 linked twice (two types, both directions), n2 weaker, n3 via inbound only
    await memory_links.create(
        db, source_id="seed", target_id="n1", link_type="supports",
        strength=0.8, created_at="2026-01-01",
    )
    await memory_links.create(
        db, source_id="n1", target_id="seed", link_type="extends",
        strength=0.92, created_at="2026-01-01",
    )
    await memory_links.create(
        db, source_id="seed", target_id="n2", link_type="supports",
        strength=0.76, created_at="2026-01-01",
    )
    await memory_links.create(
        db, source_id="n3", target_id="seed", link_type="supports",
        strength=0.85, created_at="2026-01-01",
    )
    rows = await memory_links.neighbors_of(db, ["seed"])
    ids = [r["memory_id"] for r in rows]
    assert ids == ["n1", "n3", "n2"]  # strongest-first, n1 once (max 0.92)
    assert rows[0]["strength"] == 0.92


async def test_neighbors_of_excludes_and_caps(db):
    for i, s in enumerate((0.9, 0.85, 0.8)):
        await memory_links.create(
            db, source_id="seed", target_id=f"n{i}", link_type="supports",
            strength=s, created_at="2026-01-01",
        )
    rows = await memory_links.neighbors_of(db, ["seed"], exclude=["n0"], limit=1)
    assert [r["memory_id"] for r in rows] == ["n1"]  # n0 excluded, capped to 1


async def test_neighbors_of_never_returns_seeds_or_empty(db):
    await memory_links.create(
        db, source_id="a", target_id="b", link_type="supports",
        strength=0.9, created_at="2026-01-01",
    )
    rows = await memory_links.neighbors_of(db, ["a", "b"])
    assert rows == []  # b is itself a seed, not a neighbor
    assert await memory_links.neighbors_of(db, []) == []


async def test_neighbors_of_oversized_seed_list_and_exclude_no_error(db):
    """600 seeds + a large exclude must not blow the SQL placeholder budget
    (exclusion is Python-side; seeds are capped) and truncated seeds must
    never come back as 'neighbors'."""
    await memory_links.create(
        db, source_id="seed_0", target_id="real_neighbor", link_type="supports",
        strength=0.9, created_at="2026-01-01",
    )
    seeds = [f"seed_{i}" for i in range(600)]
    exclude = [f"ex_{i}" for i in range(600)]
    rows = await memory_links.neighbors_of(db, seeds, exclude=exclude, limit=5)
    ids = [r["memory_id"] for r in rows]
    assert ids == ["real_neighbor"]
    assert not any(i.startswith("seed_") for i in ids)


async def test_neighbors_of_limit_zero_and_negative(db):
    await memory_links.create(
        db, source_id="a", target_id="b", link_type="supports",
        strength=0.9, created_at="2026-01-01",
    )
    assert await memory_links.neighbors_of(db, ["a"], limit=0) == []
    assert await memory_links.neighbors_of(db, ["a"], limit=-3) == []


async def test_neighbors_of_ties_break_deterministically(db):
    """Equal strengths order by neighbor id — expansion results (and the
    metrics built on them) must be reproducible across runs/insert order."""
    for tgt in ("n_c", "n_a", "n_b"):
        await memory_links.create(
            db, source_id="seed", target_id=tgt, link_type="supports",
            strength=0.8, created_at="2026-01-01",
        )
    rows = await memory_links.neighbors_of(db, ["seed"], limit=2)
    assert [r["memory_id"] for r in rows] == ["n_a", "n_b"]


async def test_neighbors_of_link_types_filter(db):
    """link_types restricts which edge types are followed — prod callers
    expanding into LLM context can exclude adversarial types."""
    await memory_links.create(
        db, source_id="seed", target_id="friend", link_type="supports",
        strength=0.9, created_at="2026-01-01",
    )
    await memory_links.create(
        db, source_id="seed", target_id="foe", link_type="contradicts",
        strength=0.95, created_at="2026-01-01",
    )
    all_rows = await memory_links.neighbors_of(db, ["seed"])
    assert {r["memory_id"] for r in all_rows} == {"friend", "foe"}
    safe = await memory_links.neighbors_of(
        db, ["seed"], link_types=("supports", "extends"),
    )
    assert [r["memory_id"] for r in safe] == ["friend"]
