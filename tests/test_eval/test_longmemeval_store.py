"""Tests for the LongMemEval ephemeral store factory + haystack ingest (WS-1 A4).

Uses a deterministic token-hashing fake embedder so recall ranking is
predictable without any network/model dependency. Semantic recall quality is
validated by the real end-to-end oracle run, not here — these tests pin the
*wiring*: build → ingest (first_party) → recall surfaces the evidence turn →
temp files cleaned up.
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

import pytest

from genesis.eval.longmemeval.dataset import LongMemEvalInstance, Turn
from genesis.eval.longmemeval.ingest import ingest_haystack
from genesis.eval.longmemeval.store import ephemeral_store
from genesis.qdrant.collections import VECTOR_DIM

_TOKEN = re.compile(r"[a-z0-9]+")


class _HashingEmbedder:
    """Deterministic bag-of-tokens hashing embedder. Shared tokens → cosine>0."""

    tracker = None

    async def embed(self, text: str) -> list[float]:
        vec = [0.0] * VECTOR_DIM
        for tok in _TOKEN.findall(text.lower()):
            h = int(hashlib.sha1(tok.encode()).hexdigest(), 16)  # noqa: S324 - test hash, not security
            vec[h % VECTOR_DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(t) for t in texts]


def _instance() -> LongMemEvalInstance:
    return LongMemEvalInstance(
        question_id="q1",
        question_type="single-session-user",
        question="What degree did I graduate with?",
        answer="Business Administration",
        question_date="2023/05/23 (Tue) 19:11",
        haystack_dates=["2023/05/01 (Mon) 10:00"],
        haystack_session_ids=["s1"],
        haystack_sessions=[
            [
                Turn("user", "I love hiking in the mountains every weekend.", False),
                Turn(
                    "user",
                    "I graduated with a degree in Business Administration.",
                    True,
                ),
                Turn("assistant", "Congratulations on your degree!", True),
                Turn("user", "My favorite food is sushi and ramen.", False),
                Turn("assistant", "", False),  # empty content must be skipped
            ],
        ],
        answer_session_ids=["a1"],
    )


@pytest.mark.asyncio
async def test_ephemeral_store_builds_and_cleans_up():
    workdir: Path | None = None
    async with ephemeral_store(embedding_provider=_HashingEmbedder()) as es:
        assert es.store is not None
        assert es.retriever is not None
        workdir = es.workdir
        assert workdir.exists()
    # after exit: temp workdir removed (no leak)
    assert workdir is not None
    assert not workdir.exists()


@pytest.mark.asyncio
async def test_ingest_stores_turns_first_party_and_skips_empty():
    async with ephemeral_store(embedding_provider=_HashingEmbedder()) as es:
        result = await ingest_haystack(es.store, _instance())
        # 4 non-empty turns stored (the empty assistant turn skipped)
        assert result.stored_count == 4
        assert len(result.evidence_memory_ids) == 2


@pytest.mark.asyncio
async def test_recall_surfaces_evidence_turn():
    inst = _instance()
    async with ephemeral_store(embedding_provider=_HashingEmbedder()) as es:
        result = await ingest_haystack(es.store, inst)
        hits = await es.retriever.recall(
            "degree graduate",
            source="episodic",
            limit=5,
            rerank=False,
        )
        ids = {h.memory_id for h in hits}
        # at least one gold evidence turn is retrieved
        assert result.evidence_memory_ids & ids


@pytest.mark.asyncio
async def test_with_linker_ingest_creates_memory_links():
    """with_linker + auto_link wires the real MemoryLinker: near-identical
    turns link, the links land in the ephemeral SQLite, and es.db exposes it."""
    from tests.test_eval.lme_fixtures import make_linkable_instance

    async with ephemeral_store(
        embedding_provider=_HashingEmbedder(),
        with_linker=True,
    ) as es:
        await ingest_haystack(es.store, make_linkable_instance(), auto_link=True)
        rows = await es.db.execute_fetchall("SELECT COUNT(*) FROM memory_links")
        assert rows[0][0] >= 1


@pytest.mark.asyncio
async def test_default_store_has_no_linker_and_no_links():
    """Without with_linker, auto_link=True must stay a no-op (no silent links)."""
    from tests.test_eval.lme_fixtures import make_linkable_instance

    async with ephemeral_store(embedding_provider=_HashingEmbedder()) as es:
        await ingest_haystack(es.store, make_linkable_instance(), auto_link=True)
        rows = await es.db.execute_fetchall("SELECT COUNT(*) FROM memory_links")
        assert rows[0][0] == 0


@pytest.mark.asyncio
async def test_expand_neighbors_skips_dangling_links():
    """A memory_links edge whose neighbor no longer resolves must be skipped
    silently — never crash expansion, never surface a ghost id."""
    from genesis.db.crud import memory_links as memory_links_crud
    from genesis.eval.longmemeval.runner import _expand_neighbors
    from tests.test_eval.lme_fixtures import make_linkable_instance

    async with ephemeral_store(
        embedding_provider=_HashingEmbedder(),
        with_linker=True,
    ) as es:
        result = await ingest_haystack(es.store, make_linkable_instance(), auto_link=True)
        (seed_id, *_rest) = sorted(result.evidence_memory_ids)
        # Fabricate a dangling edge: strongest link points at a ghost memory.
        await memory_links_crud.create(
            es.db,
            source_id=seed_id,
            target_id="ghost-no-such-memory",
            link_type="supports",
            strength=0.99,
            created_at="2026-01-01",
        )
        expanded = await _expand_neighbors(es.db, [seed_id], k=5)
        expanded_ids = {mid for mid, _ in expanded}
        assert "ghost-no-such-memory" not in expanded_ids
        # the real linked sibling still expands
        assert expanded_ids & (result.evidence_memory_ids - {seed_id})
