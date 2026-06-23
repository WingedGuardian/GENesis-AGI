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
