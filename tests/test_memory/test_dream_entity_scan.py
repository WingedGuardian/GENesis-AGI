"""Integration tests for the evidence-gated auto-merge (spec ③).

Exercises ``run_entity_resolution`` end-to-end with a controlled candidate pair
and a real in-memory audit DB, mocking only Qdrant I/O. Proves the Level-4
done-condition: a low-evidence pair that previously auto-merged is now FLAGGED
(not deprecated); strong pairs still merge; the survivor is the load-bearing
memory.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.memory import dream_entity_scan

NOW = datetime(2026, 6, 23, tzinfo=UTC)


def _point(pid, *, confidence, retrieved_count, created_at):
    return {
        "id": pid,
        "payload": {
            "content": f"memory content for {pid}",
            "confidence": confidence,
            "retrieved_count": retrieved_count,
            "created_at": created_at.isoformat(),
            "wing": "memory",
            "room": "test",
        },
    }


async def _run(db, point_a, point_b, score, monkeypatch):
    monkeypatch.setattr(
        "genesis.qdrant.collections.batch_retrieve_vectors",
        MagicMock(return_value={point_a["id"]: [0.1] * 8,
                                point_b["id"]: [0.1] * 8}),
    )
    monkeypatch.setattr(
        "genesis.qdrant.collections.update_payload", MagicMock(),
    )
    # graph-cache invalidation is incidental to this test
    monkeypatch.setattr(
        "genesis.memory.graph.invalidate_graph_cache", MagicMock(),
        raising=False,
    )

    async def fake_find(*_a, **_k):
        return [(point_a, point_b, score)]

    monkeypatch.setattr(
        "genesis.memory.entity_resolution.find_dedup_candidates", fake_find,
    )

    buckets = {("memory", "test"): [point_a, point_b]}
    return await dream_entity_scan.run_entity_resolution(
        qdrant=MagicMock(), db=db, router=AsyncMock(), store=MagicMock(),
        run_id="test-run", dry_run=False, buckets=buckets,
    )


async def _audit_rows(db):
    cur = await db.execute(
        "SELECT action, llm_verdict, survivor_id FROM entity_resolution_audit"
    )
    return [dict(r) for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_low_evidence_pair_flagged_not_merged(db, monkeypatch):
    """Floor cosine + far apart + default confidence: previously an auto-merge,
    now flagged for review with no deprecation."""
    far = NOW - timedelta(days=40)
    a = _point("a", confidence=0.5, retrieved_count=0, created_at=NOW)
    b = _point("b", confidence=0.5, retrieved_count=0, created_at=far)

    report = await _run(db, a, b, 0.95, monkeypatch)

    assert report["auto_merged"] == 0
    assert report["low_evidence_skipped"] == 1
    rows = await _audit_rows(db)
    assert len(rows) == 1
    assert rows[0]["action"] == "flagged"
    assert rows[0]["llm_verdict"] == "low_evidence"


@pytest.mark.asyncio
async def test_strong_pair_still_auto_merges(db, monkeypatch):
    """Near-identical, close-in-time, confident pair still auto-merges."""
    a = _point("a", confidence=0.8, retrieved_count=0, created_at=NOW)
    b = _point("b", confidence=0.8, retrieved_count=0, created_at=NOW)

    report = await _run(db, a, b, 0.99, monkeypatch)

    assert report["auto_merged"] == 1
    assert report["low_evidence_skipped"] == 0
    rows = await _audit_rows(db)
    assert rows[0]["action"] == "auto_merge"


@pytest.mark.asyncio
async def test_survivor_is_the_load_bearing_memory(db, monkeypatch):
    """On a strong merge, the more-retrieved memory survives even if older
    (the survivor fix) — instead of the prior newest-wins behavior."""
    older = NOW - timedelta(days=1)
    a = _point("a", confidence=0.8, retrieved_count=9, created_at=older)
    b = _point("b", confidence=0.8, retrieved_count=0, created_at=NOW)

    report = await _run(db, a, b, 0.99, monkeypatch)

    assert report["auto_merged"] == 1
    rows = await _audit_rows(db)
    assert rows[0]["action"] == "auto_merge"
    assert rows[0]["survivor_id"] == "a"  # older but load-bearing survives


async def _meta(db, memory_id):
    cur = await db.execute(
        "SELECT deprecated, superseded_by, superseded_at, deprecated_at, "
        "dream_cycle_run_id FROM memory_metadata WHERE memory_id = ?",
        (memory_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


@pytest.mark.asyncio
async def test_merge_records_the_survivor_in_sqlite(db, monkeypatch):
    """A merged-away memory must name its survivor in SQLite, not only in the
    Qdrant payload: recall's "point forward" and the supersession chain read
    ``superseded_by``. ``deprecated_at`` stays NULL on purpose — it arms
    dream_link_repair's aged-edge prune, and an entity merge copies no edges
    onto the survivor, so stamping it would delete the retired memory's edges."""
    for pid in ("a", "b"):
        await db.execute(
            "INSERT INTO memory_metadata (memory_id, created_at, deprecated) "
            "VALUES (?, ?, 0)",
            (pid, NOW.isoformat()),
        )
    await db.commit()
    older = NOW - timedelta(days=1)
    a = _point("a", confidence=0.8, retrieved_count=9, created_at=older)
    b = _point("b", confidence=0.8, retrieved_count=0, created_at=NOW)

    report = await _run(db, a, b, 0.99, monkeypatch)

    assert report["auto_merged"] == 1
    retired = await _meta(db, "b")
    assert retired["deprecated"] == 1
    assert retired["dream_cycle_run_id"] == "test-run"
    assert retired["superseded_by"] == "a"
    assert retired["superseded_at"] is not None
    assert retired["deprecated_at"] is None
    survivor = await _meta(db, "a")
    assert survivor["deprecated"] == 0
    assert survivor["superseded_by"] is None


@pytest.mark.asyncio
async def test_merge_leaves_an_already_retired_loser_alone(db, monkeypatch):
    """Candidates come from Qdrant, so a row SQLite already retired (here an
    explicit supersede whose Qdrant mirror failed) can still pair up. The merge
    must not replace its successor or stamp this run, which a rollback would
    then undo, and must leave its Qdrant point for that supersede's repair."""
    from genesis.qdrant import collections as qdrant_collections

    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at, deprecated) "
        "VALUES ('a', ?, 0)",
        (NOW.isoformat(),),
    )
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at, deprecated, "
        "superseded_by, superseded_at) VALUES ('b', ?, 1, 'explicit-new', ?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    await db.commit()
    older = NOW - timedelta(days=1)
    a = _point("a", confidence=0.8, retrieved_count=9, created_at=older)
    b = _point("b", confidence=0.8, retrieved_count=0, created_at=NOW)

    report = await _run(db, a, b, 0.99, monkeypatch)

    assert report["auto_merged"] == 0
    assert report["already_retired"] == 1
    assert await _audit_rows(db) == []
    # Only the flag is re-asserted: no merged_into pointer to the survivor.
    qdrant_collections.update_payload.assert_called_once()
    assert qdrant_collections.update_payload.call_args.kwargs["payload"] == {
        "deprecated": True,
    }
    retired = await _meta(db, "b")
    assert retired["superseded_by"] == "explicit-new"
    assert retired["dream_cycle_run_id"] is None


@pytest.mark.asyncio
async def test_merge_into_a_retired_survivor_leaves_the_loser_alone(db, monkeypatch):
    """The survivor comes from Qdrant too. If SQLite has retired it, merging the
    live loser into it would take both out of recall. Only the retired
    survivor's Qdrant flag is re-asserted."""
    from genesis.qdrant import collections as qdrant_collections

    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at, deprecated, "
        "superseded_by) VALUES ('a', ?, 1, 'explicit-new')",
        (NOW.isoformat(),),
    )
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at, deprecated) "
        "VALUES ('b', ?, 0)",
        (NOW.isoformat(),),
    )
    await db.commit()
    older = NOW - timedelta(days=1)
    a = _point("a", confidence=0.8, retrieved_count=9, created_at=older)
    b = _point("b", confidence=0.8, retrieved_count=0, created_at=NOW)

    report = await _run(db, a, b, 0.99, monkeypatch)

    assert report["auto_merged"] == 0
    assert report["already_retired"] == 1
    qdrant_collections.update_payload.assert_called_once()
    assert qdrant_collections.update_payload.call_args.kwargs["point_id"] == "a"
    assert qdrant_collections.update_payload.call_args.kwargs["payload"] == {
        "deprecated": True,
    }
    assert (await _meta(db, "b"))["deprecated"] == 0
    assert (await _meta(db, "b"))["superseded_by"] is None


@pytest.mark.asyncio
async def test_a_live_loser_refused_by_a_retired_survivor_can_still_merge(db, monkeypatch):
    """A refusal must exclude the RETIRED side for the rest of the run. Excluding
    the live loser instead would skip its later pairs while the stale survivor
    stays eligible, so one retired point could block every merge it touches."""
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at, deprecated, "
        "superseded_by) VALUES ('a', ?, 1, 'explicit-new')",
        (NOW.isoformat(),),
    )
    for pid in ("b", "c"):
        await db.execute(
            "INSERT INTO memory_metadata (memory_id, created_at, deprecated) "
            "VALUES (?, ?, 0)",
            (pid, NOW.isoformat()),
        )
    await db.commit()
    older = NOW - timedelta(days=1)
    a = _point("a", confidence=0.8, retrieved_count=9, created_at=older)
    b = _point("b", confidence=0.8, retrieved_count=5, created_at=NOW)
    c = _point("c", confidence=0.8, retrieved_count=0, created_at=NOW)
    monkeypatch.setattr(
        "genesis.qdrant.collections.batch_retrieve_vectors",
        MagicMock(return_value={pid: [0.1] * 8 for pid in ("a", "b", "c")}),
    )
    monkeypatch.setattr("genesis.qdrant.collections.update_payload", MagicMock())
    monkeypatch.setattr(
        "genesis.memory.graph.invalidate_graph_cache", MagicMock(), raising=False,
    )

    async def fake_find(*_a, **_k):
        return [(a, b, 0.99), (b, c, 0.99)]

    monkeypatch.setattr(
        "genesis.memory.entity_resolution.find_dedup_candidates", fake_find,
    )
    report = await dream_entity_scan.run_entity_resolution(
        qdrant=MagicMock(), db=db, router=AsyncMock(), store=MagicMock(),
        run_id="test-run", dry_run=False, buckets={("memory", "test"): [a, b, c]},
    )

    assert report["already_retired"] == 1
    assert report["auto_merged"] == 1
    merged = await _meta(db, "c")
    assert merged["deprecated"] == 1
    assert merged["superseded_by"] == "b"
    assert (await _meta(db, "b"))["deprecated"] == 0


@pytest.mark.asyncio
async def test_a_failed_qdrant_mirror_heals_on_the_next_pass(db):
    """SQLite is written first. If the Qdrant write then fails, the point stays
    live and comes back as a candidate; the next pass must re-assert the flag
    rather than skip it forever."""
    for pid in ("a", "b"):
        await db.execute(
            "INSERT INTO memory_metadata (memory_id, created_at, deprecated) "
            "VALUES (?, ?, 0)",
            (pid, NOW.isoformat()),
        )
    await db.commit()
    import genesis.qdrant.collections as qdrant_collections

    failing = MagicMock(side_effect=RuntimeError("qdrant down"))
    working = MagicMock()
    original = qdrant_collections.update_payload
    try:
        qdrant_collections.update_payload = failing
        with pytest.raises(RuntimeError):
            await dream_entity_scan._deprecate_memory(
                MagicMock(), db, "b", survivor_id="a", run_id="run-1",
            )
        assert (await _meta(db, "b"))["dream_cycle_run_id"] == "run-1"

        qdrant_collections.update_payload = working
        applied = await dream_entity_scan._deprecate_memory(
            MagicMock(), db, "b", survivor_id="a", run_id="run-2",
        )
    finally:
        qdrant_collections.update_payload = original

    assert applied == "b"
    assert working.call_args.kwargs["payload"] == {"deprecated": True}
    assert (await _meta(db, "b"))["dream_cycle_run_id"] == "run-1"
