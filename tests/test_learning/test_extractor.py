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
def _reset_embedding_provider_singleton():
    """Reset the module-level embedder cache between tests so state from one
    test (or a real EmbeddingProvider built on first use) can't leak into the
    next.
    """
    extractor_mod._EMBEDDING_PROVIDER = None
    yield
    extractor_mod._EMBEDDING_PROVIDER = None


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
