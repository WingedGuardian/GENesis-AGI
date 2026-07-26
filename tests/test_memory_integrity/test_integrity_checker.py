"""Consistency-checker classification + fail-closed + status tests."""

from __future__ import annotations

import pytest

from genesis.memory.integrity import run_consistency_check

from .conftest import FakeQdrantClient, build_db, insert_memory

pytestmark = pytest.mark.asyncio


async def _db(tmp_path) -> str:
    path = str(tmp_path / "t.db")
    await build_db(path)
    return path


async def _run(path, qdrant, **kw):
    return await run_consistency_check(db_path=path, qdrant_client=qdrant, **kw)


async def test_empty_state_is_healthy(tmp_path):
    path = await _db(tmp_path)
    rep = await _run(path, FakeQdrantClient())
    assert rep.status == "healthy"
    assert rep.total_rows == 0
    assert rep.total_findings == 0


async def test_empty_metadata_with_orphan_points_not_healthy(tmp_path):
    """Metadata wiped/restored-stale but Qdrant still holds points → ghost_points
    surfaced, NOT a false 'healthy' early return (P1: don't skip the scan on an
    empty corpus)."""
    path = await _db(tmp_path)  # zero memory rows
    pts = {"episodic_memory": {f"orphan{i}": {} for i in range(60)}, "knowledge_base": {}}
    rep = await _run(path, FakeQdrantClient(pts))
    assert rep.counts["ghost_points"] == 60
    assert rep.total_rows == 0
    assert rep.status == "degraded"  # 60 >= pollution floor (50)


async def test_empty_everything_is_healthy(tmp_path):
    """Genuinely empty install (no metadata, no points) → healthy, no alarm."""
    path = await _db(tmp_path)
    rep = await _run(path, FakeQdrantClient({"episodic_memory": {}, "knowledge_base": {}}))
    assert rep.status == "healthy"
    assert rep.total_findings == 0


async def test_empty_metadata_qdrant_down_is_unknown(tmp_path):
    """Empty metadata + Qdrant unreachable must be 'unknown', never 'healthy'."""
    path = await _db(tmp_path)
    rep = await _run(path, FakeQdrantClient(raise_on="scroll"))
    assert rep.status == "unknown"


async def test_all_consistent_is_healthy(tmp_path):
    path = await _db(tmp_path)
    pts = {"episodic_memory": {}, "knowledge_base": {}}
    for i in range(10):
        mid = f"m{i}"
        await insert_memory(path, mid)
        pts["episodic_memory"][mid] = {}
    rep = await _run(path, FakeQdrantClient(pts))
    assert rep.status == "healthy"
    assert rep.total_findings == 0


async def test_lying_mirror_detected(tmp_path):
    """embedded rows with NO Qdrant point → lying_mirror, and >=5 → degraded."""
    path = await _db(tmp_path)
    pts = {"episodic_memory": {}, "knowledge_base": {}}
    for i in range(6):  # 6 embedded rows, none present in Qdrant
        await insert_memory(path, f"m{i}")
    rep = await _run(path, FakeQdrantClient(pts))
    assert rep.counts["lying_mirror"] == 6
    assert rep.status == "degraded"  # severe floor = 5
    assert set(rep.offender_sample["lying_mirror"]) == {f"m{i}" for i in range(6)}


async def test_lying_mirror_below_floor_is_healthy(tmp_path):
    path = await _db(tmp_path)
    pts = {"episodic_memory": {}, "knowledge_base": {}}
    for i in range(3):  # only 3 < severe floor 5
        await insert_memory(path, f"m{i}")
    rep = await _run(path, FakeQdrantClient(pts))
    assert rep.counts["lying_mirror"] == 3
    assert rep.status == "healthy"


async def test_collection_agnostic_existence(tmp_path):
    """A point living in a DIFFERENT collection than metadata claims is still a
    match (the collection column is documented-unreliable)."""
    path = await _db(tmp_path)
    await insert_memory(path, "m1", collection="episodic_memory")
    # point actually stored under knowledge_base
    pts = {"episodic_memory": {}, "knowledge_base": {"m1": {}}}
    rep = await _run(path, FakeQdrantClient(pts))
    assert rep.counts["lying_mirror"] == 0  # found in the other collection


async def test_ghost_points_detected(tmp_path):
    path = await _db(tmp_path)
    await insert_memory(path, "m1")
    pts = {"episodic_memory": {"m1": {}, "ghost1": {}, "ghost2": {}}, "knowledge_base": {}}
    rep = await _run(path, FakeQdrantClient(pts))
    assert rep.counts["ghost_points"] == 2
    assert set(rep.offender_sample["ghost_points"]) == {"ghost1", "ghost2"}


async def test_unexpected_vector_detected(tmp_path):
    """fts5_only rows must NOT have a Qdrant point."""
    path = await _db(tmp_path)
    await insert_memory(path, "m1", status="fts5_only")
    pts = {"episodic_memory": {"m1": {}}, "knowledge_base": {}}
    rep = await _run(path, FakeQdrantClient(pts))
    assert rep.counts["unexpected_vector"] == 1
    assert rep.counts["lying_mirror"] == 0  # fts5_only is NOT expected to have a vector


async def test_deprecated_divergence_detected(tmp_path):
    path = await _db(tmp_path)
    await insert_memory(path, "m1", deprecated=1)  # SQLite says deprecated
    pts = {"episodic_memory": {"m1": {}}, "knowledge_base": {}}  # payload says not
    rep = await _run(path, FakeQdrantClient(pts))
    assert rep.counts["deprecated_divergence"] == 1


async def test_fts_invisible_is_severe(tmp_path):
    """metadata rows with no FTS entry are a severe search-path absence."""
    path = await _db(tmp_path)
    pts = {"episodic_memory": {}, "knowledge_base": {}}
    for i in range(5):
        mid = f"m{i}"
        await insert_memory(path, mid, in_fts=False)  # no FTS row
        pts["episodic_memory"][mid] = {}
    rep = await _run(path, FakeQdrantClient(pts))
    assert rep.counts["fts_invisible"] == 5
    assert rep.status == "degraded"


async def test_qdrant_unreachable_is_unknown_not_degraded(tmp_path):
    """A dependency outage must NEVER be reported as data corruption."""
    path = await _db(tmp_path)
    for i in range(10):
        await insert_memory(path, f"m{i}")
    rep = await _run(path, FakeQdrantClient(raise_on="scroll"))
    assert rep.status == "unknown"
    assert rep.unknown_reason and "qdrant_unavailable" in rep.unknown_reason
    # FTS-side counts are computed; vector-side never fabricated as findings
    assert rep.total_findings == 0


async def test_pollution_threshold_uses_fraction(tmp_path):
    """Ghosts below max(pollution_min_count, fraction*total) stay healthy."""
    path = await _db(tmp_path)
    pts = {"episodic_memory": {}, "knowledge_base": {}}
    for i in range(100):
        mid = f"m{i}"
        await insert_memory(path, mid)
        pts["episodic_memory"][mid] = {}
    # 10 ghosts; pollution floor is 50 → healthy
    for g in range(10):
        pts["episodic_memory"][f"ghost{g}"] = {}
    rep = await _run(path, FakeQdrantClient(pts))
    assert rep.counts["ghost_points"] == 10
    assert rep.status == "healthy"


async def test_truncation_marks_lying_mirror_not_computed(tmp_path):
    """When the point budget is hit, lying_mirror is -1 (not computed), never a
    wrong count, and truncated is flagged."""
    path = await _db(tmp_path)
    pts = {"episodic_memory": {}, "knowledge_base": {}}
    for i in range(20):
        mid = f"m{i}"
        await insert_memory(path, mid)
        pts["episodic_memory"][mid] = {}
    rep = await _run(path, FakeQdrantClient(pts), max_points=5)
    assert rep.truncated is True
    assert rep.counts["lying_mirror"] == -1
