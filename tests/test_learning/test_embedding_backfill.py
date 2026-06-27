"""Tests for promoter.backfill_missing_embeddings (LC1-B).

Procedures whose ``principle_embedding`` is NULL (a transient embed failure at
create time, or a legacy row) are invisible to the relevance-gated proactive
hook. The hourly backfill re-embeds them so they rejoin the surfacing pool. The
embedder is faked here — these are deterministic unit tests, no network.
"""

from __future__ import annotations

import pytest

from genesis.db.crud import procedural
from genesis.learning.procedural import promoter
from genesis.learning.procedural.embedding import EMBEDDING_DIM, unpack_embedding

pytestmark = pytest.mark.asyncio


class _FakeProvider:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    async def embed(self, text: str) -> list[float]:
        if self._fail:
            from genesis.memory.embeddings import EmbeddingUnavailableError

            raise EmbeddingUnavailableError("backends down")
        return [0.1] * EMBEDDING_DIM


def _patch_provider(monkeypatch, *, fail: bool = False) -> None:
    monkeypatch.setattr(
        "genesis.memory.embeddings.EmbeddingProvider",
        lambda *a, **k: _FakeProvider(fail=fail),
    )


async def _mk(db, pid, *, embed=None, tier="LIBRARY", deprecated=0, quarantined=0):
    await db.execute(
        "INSERT INTO procedural_memory "
        "(id, task_type, principle, steps, tools_used, context_tags, created_at, "
        " activation_tier, principle_embedding, deprecated, quarantined) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (pid, "t", "do x", "[]", "[]", "[]", "2026-01-01T00:00:00",
         tier, embed, deprecated, quarantined),
    )


async def test_backfill_repairs_null_embeddings(db, monkeypatch):
    _patch_provider(monkeypatch)
    await _mk(db, "p1", embed=None)                        # NULL → repaired
    await _mk(db, "p2", embed=b"x" * (EMBEDDING_DIM * 4))  # already embedded → untouched
    await db.commit()

    assert await promoter.backfill_missing_embeddings(db) == 1
    row = await procedural.get_by_id(db, "p1")
    assert row["principle_embedding"] is not None
    assert unpack_embedding(row["principle_embedding"]) is not None


async def test_backfill_skips_deprecated_and_quarantined(db, monkeypatch):
    _patch_provider(monkeypatch)
    await _mk(db, "dep", embed=None, deprecated=1)
    await _mk(db, "quar", embed=None, quarantined=1)
    await db.commit()
    assert await promoter.backfill_missing_embeddings(db) == 0


async def test_backfill_fail_open_on_embedder_outage(db, monkeypatch):
    _patch_provider(monkeypatch, fail=True)
    await _mk(db, "p1", embed=None)
    await db.commit()
    # Outage → 0 repaired, no exception raised, row stays NULL (retried next run).
    assert await promoter.backfill_missing_embeddings(db) == 0
    row = await procedural.get_by_id(db, "p1")
    assert row["principle_embedding"] is None


async def test_backfill_noop_when_all_embedded(db, monkeypatch):
    _patch_provider(monkeypatch)
    await _mk(db, "p1", embed=b"x" * (EMBEDDING_DIM * 4))
    await db.commit()
    assert await promoter.backfill_missing_embeddings(db) == 0
