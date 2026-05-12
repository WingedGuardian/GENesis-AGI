"""Tests for the procedure extractor — novelty gate behavior."""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.learning.procedural import extractor as extractor_mod
from genesis.learning.procedural.extractor import (
    NOVELTY_THRESHOLD,
    _cosine_similarity,
    extract_procedure,
)
from genesis.learning.procedural.operations import store_procedure


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset module-level singletons between tests so state can't leak."""
    extractor_mod._EMBEDDING_PROVIDER = None
    extractor_mod._fail_open_timestamps.clear()
    yield
    extractor_mod._EMBEDDING_PROVIDER = None
    extractor_mod._fail_open_timestamps.clear()


@dataclass
class _FakeRouterResult:
    success: bool = True
    content: str = ""


def _router_returning(payload: dict) -> MagicMock:
    router = MagicMock()
    router.route_call = AsyncMock(
        return_value=_FakeRouterResult(success=True, content=json.dumps(payload))
    )
    return router


def _embedder(mapping: dict[str, list[float]]) -> MagicMock:
    """Build a fake embedder. `mapping` is principle text -> vector."""
    emb = MagicMock()

    async def _embed(text: str) -> list[float]:
        # Default to a zero vector for unmapped inputs (cosine = 0).
        return mapping.get(text, [0.0, 0.0, 0.0])

    emb.embed = AsyncMock(side_effect=_embed)
    return emb


def _valid_payload(task_type: str = "deploy-service", principle: str = "always verify before deploy") -> dict:
    return {
        "task_type": task_type,
        "principle": principle,
        "steps": ["build", "test", "deploy"],
        "tools_used": ["docker"],
        "context_tags": ["prod"],
        "tool_trigger": None,
    }


def test_cosine_similarity_basic():
    assert _cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    # Length mismatch returns 0 (defensive)
    assert _cosine_similarity([1.0], [1.0, 0.0]) == 0.0
    # Zero vector returns 0
    assert _cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


@pytest.mark.asyncio
async def test_extract_stores_when_table_empty(db):
    """No existing procedures => stores; embeds the new principle once.

    The new principle is embedded so it can be packed into the
    `principle_embedding` BLOB for the proactive procedure hook. No
    existing-principle comparisons fire when the table is empty for this
    task_type.
    """
    payload = _valid_payload()
    router = _router_returning(payload)
    embedder = _embedder({
        "always verify before deploy": [1.0, 0.0, 0.0],
    })

    proc_id = await extract_procedure(
        db,
        summary_text="ran build, deployed",
        outcome="success",
        router=router,
        embedding_provider=embedder,
    )
    assert proc_id is not None
    # Embed is called exactly once — for the new principle. The dim-mismatch
    # in the test vector triggers a graceful "stored without embedding" path
    # (logged), but the procedure still lands in the DB.
    assert embedder.embed.await_count == 1


@pytest.mark.asyncio
async def test_extract_stores_when_principle_is_novel(db):
    """Existing procedure of same task_type but dissimilar principle => stores."""
    # Seed an existing procedure with task_type "deploy-service"
    await store_procedure(
        db,
        task_type="deploy-service",
        principle="never deploy without rollback plan",
        steps=["plan", "deploy"],
        tools_used=["docker"],
        context_tags=["prod"],
    )

    payload = _valid_payload(principle="always run integration tests first")
    embedder = _embedder({
        "always run integration tests first": [1.0, 0.0, 0.0],
        "never deploy without rollback plan": [0.0, 1.0, 0.0],
    })
    router = _router_returning(payload)

    proc_id = await extract_procedure(
        db,
        summary_text="ran integration tests then deployed",
        outcome="success",
        router=router,
        embedding_provider=embedder,
    )
    assert proc_id is not None


@pytest.mark.asyncio
async def test_extract_skips_when_near_duplicate(db):
    """Cosine >= NOVELTY_THRESHOLD against an existing principle => skip store."""
    await store_procedure(
        db,
        task_type="deploy-service",
        principle="always verify health checks pass before deploy",
        steps=["check", "deploy"],
        tools_used=["docker"],
        context_tags=["prod"],
    )

    # New principle is intentionally a near-paraphrase; we force the
    # cosine to be exactly 1.0 by mapping both texts to the same vector.
    same_vector = [1.0, 0.0, 0.0]
    payload = _valid_payload(principle="check health then deploy, always")
    embedder = _embedder({
        "check health then deploy, always": same_vector,
        "always verify health checks pass before deploy": same_vector,
    })
    router = _router_returning(payload)

    proc_id = await extract_procedure(
        db,
        summary_text="deployed after health checks",
        outcome="success",
        router=router,
        embedding_provider=embedder,
    )
    assert proc_id is None  # Skipped by novelty gate

    # Verify threshold is the documented constant, not a moved goalpost.
    assert NOVELTY_THRESHOLD == 0.85


@pytest.mark.asyncio
async def test_extract_fails_open_when_embedder_is_none(db):
    """Embedder unavailable => store anyway (don't drop extractions silently)."""
    await store_procedure(
        db,
        task_type="deploy-service",
        principle="some existing principle",
        steps=["a"],
        tools_used=["t"],
        context_tags=["c"],
    )

    payload = _valid_payload(principle="totally different principle")
    router = _router_returning(payload)

    proc_id = await extract_procedure(
        db,
        summary_text="x",
        outcome="success",
        router=router,
        embedding_provider=None,
    )
    # With embedder=None, _get_embedding_provider() is consulted. In test
    # environments without an embedding backend it returns None and we
    # fail-open. The procedure should store.
    # (Note: if a backend IS available in the test env, this test would still
    # pass because the principles are very different.)
    assert proc_id is not None


# ─── FM3: Quality gate ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_skips_when_quality_gate_returns_skip(db):
    """LLM says skip => extraction aborted before required-fields check."""
    router = _router_returning({"skip": True, "reason": "not reusable"})
    proc_id = await extract_procedure(
        db,
        summary_text="did a thing",
        outcome="success",
        router=router,
        embedding_provider=_embedder({}),
    )
    assert proc_id is None


@pytest.mark.asyncio
async def test_extract_skips_when_reusability_score_low(db):
    """Low reusability_score => extraction aborted."""
    payload = _valid_payload()
    payload["reusability_score"] = 0.3
    router = _router_returning(payload)
    proc_id = await extract_procedure(
        db,
        summary_text="did a thing",
        outcome="success",
        router=router,
        embedding_provider=_embedder({"always verify before deploy": [1.0, 0.0, 0.0]}),
    )
    assert proc_id is None


@pytest.mark.asyncio
async def test_extract_stores_when_reusability_score_high(db):
    """High reusability_score => extraction proceeds normally."""
    payload = _valid_payload()
    payload["reusability_score"] = 0.8
    router = _router_returning(payload)
    proc_id = await extract_procedure(
        db,
        summary_text="did a thing",
        outcome="success",
        router=router,
        embedding_provider=_embedder({"always verify before deploy": [1.0, 0.0, 0.0]}),
    )
    assert proc_id is not None


# ─── FM2: Cross-type contradiction check ─────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_type_duplicate_blocked(db):
    """Trusted procedure with overlapping tags + similar principle blocks extraction."""
    # Seed a trusted (explicit-teach) procedure with overlapping context tags
    await store_procedure(
        db,
        task_type="youtube-content-fetch",
        principle="use yt-dlp to download youtube transcripts",
        steps=["run yt-dlp"],
        tools_used=["Bash"],
        context_tags=["youtube", "video", "transcript"],
        speculative=0,
        confidence=0.7,
    )

    # New extraction with different task_type but overlapping tags + similar principle
    same_vector = [1.0, 0.0, 0.0]
    payload = _valid_payload(
        task_type="youtube-fallback",
        principle="use yt-dlp for youtube video transcripts",
    )
    payload["context_tags"] = ["youtube", "video", "fallback"]
    embedder = _embedder({
        "use yt-dlp for youtube video transcripts": same_vector,
        "use yt-dlp to download youtube transcripts": same_vector,
    })
    router = _router_returning(payload)

    proc_id = await extract_procedure(
        db,
        summary_text="fetched youtube transcript",
        outcome="success",
        router=router,
        embedding_provider=embedder,
    )
    assert proc_id is None  # Blocked by cross-type duplicate check


@pytest.mark.asyncio
async def test_cross_type_check_allows_different_principles(db):
    """Different principles with overlapping tags are allowed (not duplicates)."""
    await store_procedure(
        db,
        task_type="youtube-content-fetch",
        principle="use yt-dlp to download youtube transcripts",
        steps=["run yt-dlp"],
        tools_used=["Bash"],
        context_tags=["youtube", "video", "transcript"],
        speculative=0,
        confidence=0.7,
    )

    # Different principle (orthogonal vectors = cosine 0)
    payload = _valid_payload(
        task_type="youtube-metadata",
        principle="use youtube API for video metadata",
    )
    payload["context_tags"] = ["youtube", "video", "metadata"]
    embedder = _embedder({
        "use youtube API for video metadata": [1.0, 0.0, 0.0],
        "use yt-dlp to download youtube transcripts": [0.0, 1.0, 0.0],
    })
    router = _router_returning(payload)

    proc_id = await extract_procedure(
        db,
        summary_text="fetched youtube metadata",
        outcome="success",
        router=router,
        embedding_provider=embedder,
    )
    assert proc_id is not None  # Allowed — different principles


# ─── FM4: Auto-extracted procedures start at L3 ─────────────────────────────


@pytest.mark.asyncio
async def test_auto_extracted_starts_at_l3_with_baseline_confidence(db):
    """Auto-extracted procedures start at L3/0.5, not L4/0.0."""
    payload = _valid_payload()
    router = _router_returning(payload)
    proc_id = await extract_procedure(
        db,
        summary_text="deployed service",
        outcome="success",
        router=router,
        embedding_provider=_embedder({"always verify before deploy": [1.0, 0.0, 0.0]}),
    )
    assert proc_id is not None

    cursor = await db.execute(
        "SELECT activation_tier, confidence, speculative FROM procedural_memory WHERE id = ?",
        (proc_id,),
    )
    rows = await cursor.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "L3"
    assert rows[0][1] == pytest.approx(0.5)
    assert rows[0][2] == 1  # still marked speculative (provenance only)


# ─── FM5: Fail-open rate limiter ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fail_open_rate_limiter_allows_first_store(db):
    """First fail-open extraction for a task_type is allowed."""
    payload = _valid_payload()
    router = _router_returning(payload)

    proc_id = await extract_procedure(
        db,
        summary_text="x",
        outcome="success",
        router=router,
        embedding_provider=None,
    )
    assert proc_id is not None


@pytest.mark.asyncio
async def test_fail_open_rate_limiter_blocks_rapid_second(db):
    """Second fail-open extraction for same task_type within cooldown => blocked."""
    payload = _valid_payload()

    # First extraction succeeds (fail-open, first in cooldown window)
    router1 = _router_returning(payload)
    proc_id1 = await extract_procedure(
        db,
        summary_text="x",
        outcome="success",
        router=router1,
        embedding_provider=None,
    )
    assert proc_id1 is not None

    # Second extraction with same task_type => rate limited
    router2 = _router_returning(payload)
    proc_id2 = await extract_procedure(
        db,
        summary_text="y",
        outcome="success",
        router=router2,
        embedding_provider=None,
    )
    assert proc_id2 is None


@pytest.mark.asyncio
async def test_fail_open_rate_limiter_allows_different_task_type(db):
    """Fail-open rate limit is per-task-type, not global."""
    # First extraction: task_type = "deploy-service"
    router1 = _router_returning(_valid_payload())
    await extract_procedure(
        db,
        summary_text="x",
        outcome="success",
        router=router1,
        embedding_provider=None,
    )

    # Second extraction: different task_type => allowed
    payload2 = _valid_payload(task_type="build-service", principle="always build first")
    router2 = _router_returning(payload2)
    proc_id2 = await extract_procedure(
        db,
        summary_text="y",
        outcome="success",
        router=router2,
        embedding_provider=None,
    )
    assert proc_id2 is not None
