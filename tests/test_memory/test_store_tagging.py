"""Tests for source_pipeline provenance tagging on memory store/retrieval."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.store import MemoryStore
from genesis.memory.types import RetrievalResult


@pytest.fixture()
def embedding_provider():
    ep = MagicMock()
    ep.embed = AsyncMock(return_value=[0.1] * 1024)
    ep.enrich = MagicMock(return_value="episodic: test content")
    return ep


@pytest.fixture()
def qdrant():
    return MagicMock()


@pytest.fixture()
def db():
    return AsyncMock()


@pytest.fixture()
def store(embedding_provider, qdrant, db):
    return MemoryStore(
        embedding_provider=embedding_provider,
        qdrant_client=qdrant,
        db=db,
    )


@pytest.mark.asyncio()
async def test_store_with_source_pipeline_includes_in_payload(store):
    """source_pipeline should appear in the Qdrant upsert payload."""
    with patch("genesis.memory.store.upsert_point") as mock_upsert, \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        await store.store("test content", "src", source_pipeline="recon")

    payload = mock_upsert.call_args.kwargs["payload"]
    assert payload["source_pipeline"] == "recon"


@pytest.mark.asyncio()
async def test_store_without_source_pipeline_omits_from_payload(store):
    """When source_pipeline is None, key should be absent (sparse storage)."""
    with patch("genesis.memory.store.upsert_point") as mock_upsert, \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        await store.store("test content", "src")

    payload = mock_upsert.call_args.kwargs["payload"]
    assert "source_pipeline" not in payload


def test_strip_kv_prefix():
    """#17: defensive strip of a leaked ``wing=``/``room=`` prefix."""
    from genesis.memory.store import _strip_kv_prefix

    assert _strip_kv_prefix("wing=channels", "wing") == "channels"
    assert _strip_kv_prefix("wing:channels", "wing") == "channels"
    assert _strip_kv_prefix("room=uncategorized", "room") == "uncategorized"
    assert _strip_kv_prefix("channels", "wing") == "channels"  # already clean
    assert _strip_kv_prefix(None, "wing") is None
    assert _strip_kv_prefix("", "wing") == ""
    # Only a leading ``key=`` is stripped — a non-matching key/mid-string '=' is kept
    assert _strip_kv_prefix("a=b", "wing") == "a=b"


@pytest.mark.asyncio()
async def test_store_strips_leaked_wing_prefix(store):
    """A leaked ``wing=channels`` value is sanitized before it reaches the Qdrant
    payload, the FTS5 wing tag, and the memory_metadata write."""
    with patch("genesis.memory.store.upsert_point") as mock_upsert, \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        await store.store(
            "test content", "src", wing="wing=channels", room="room=uncategorized",
        )

    payload = mock_upsert.call_args.kwargs["payload"]
    assert payload["wing"] == "channels"
    assert payload["room"] == "uncategorized"
    assert "wing:channels" in payload["tags"]
    assert "wing:wing=channels" not in payload["tags"]
    # The memory_metadata companion write gets the sanitized value too
    assert mock_mem.create_metadata.call_args.kwargs["wing"] == "channels"
    assert mock_mem.create_metadata.call_args.kwargs["room"] == "uncategorized"


@pytest.mark.asyncio()
async def test_store_source_pipeline_various_values(store):
    """All expected source_pipeline values should work."""
    for pipeline in ("conversation", "reflection", "recon", "harvest", "mail"):
        with patch("genesis.memory.store.upsert_point") as mock_upsert, \
             patch("genesis.memory.store.memory_crud") as mock_mem:
            mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
            mock_mem.upsert = AsyncMock(return_value="id")
            mock_mem.create_metadata = AsyncMock(return_value=None)
            await store.store("test content", "src", source_pipeline=pipeline)

        payload = mock_upsert.call_args.kwargs["payload"]
        assert payload["source_pipeline"] == pipeline


def test_retrieval_result_includes_source_pipeline():
    """RetrievalResult dataclass should accept and expose source_pipeline."""
    result = RetrievalResult(
        memory_id="test-id",
        content="test",
        source="src",
        memory_type="episodic",
        score=0.9,
        vector_rank=1,
        fts_rank=None,
        activation_score=0.5,
        payload={"source_pipeline": "reflection"},
        source_pipeline="reflection",
    )
    assert result.source_pipeline == "reflection"


def test_retrieval_result_source_pipeline_defaults_none():
    """RetrievalResult.source_pipeline should default to None."""
    result = RetrievalResult(
        memory_id="test-id",
        content="test",
        source="src",
        memory_type="episodic",
        score=0.9,
        vector_rank=1,
        fts_rank=None,
        activation_score=0.5,
        payload={},
    )
    assert result.source_pipeline is None


def test_extraction_kwargs_include_source_pipeline():
    """extractions_to_store_kwargs should include source_pipeline='harvest'."""
    from genesis.memory.extraction import Extraction, extractions_to_store_kwargs

    extraction = Extraction(
        content="Test entity",
        extraction_type="entity",
        confidence=0.8,
        entities=["Test"],
    )
    kwargs = extractions_to_store_kwargs(extraction)
    assert kwargs["source_pipeline"] == "harvest"


# --- Controlled-vocabulary enforcement for `wing` ---------------------------
# Before this, the wing branch only tested falsiness, so ANY explicit string
# reached the FTS5 `wing:` tag, the Qdrant payload and memory_metadata.wing —
# and classify_life_domain() returns "personal" for an unknown wing, so one bad
# value corrupted the life domain too. essential_knowledge.py filtered junk
# wings on READ, which hid the problem rather than preventing it.


@pytest.mark.asyncio()
async def test_store_drops_unknown_wing_and_auto_classifies(store, caplog):
    """An out-of-vocabulary wing must never be persisted. The store COERCES
    (rather than raising) because internal callers pass LLM-derived values."""
    with patch("genesis.memory.store.upsert_point") as mock_upsert, \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        with caplog.at_level("WARNING"):
            await store.store("test content", "src", wing="portfolio")

    from genesis.memory.taxonomy import WINGS

    payload = mock_upsert.call_args.kwargs["payload"]
    assert payload["wing"] != "portfolio"
    assert payload["wing"] in WINGS, payload["wing"]
    assert "wing:portfolio" not in payload["tags"], payload["tags"]
    # The companion metadata write must not carry it either.
    assert mock_mem.create_metadata.call_args.kwargs["wing"] != "portfolio"
    assert mock_mem.create_metadata.call_args.kwargs["wing"] in WINGS
    # The second half of the defect: classify_life_domain() returns "personal"
    # for an unknown wing, so a bad value corrupts a DERIVED field too. There is
    # no life_domain column on memory_metadata — the payload is the only place
    # this is observable.
    from genesis.memory.taxonomy import LIFE_DOMAINS

    assert payload["life_domain"] in LIFE_DOMAINS, payload["life_domain"]
    # Silent coercion would be its own trap — the drop is logged, by THIS
    # logger at WARNING (any WARNING anywhere mentioning the value would
    # otherwise satisfy this).
    assert any(
        r.name == "genesis.memory.store"
        and r.levelname == "WARNING"
        and "portfolio" in r.getMessage()
        for r in caplog.records
    ), caplog.text


@pytest.mark.asyncio()
async def test_store_preserves_a_valid_explicit_wing(store):
    """The guard must not disturb the supported case — an explicit, valid wing
    still wins outright over auto-classification."""
    with patch("genesis.memory.store.upsert_point") as mock_upsert, \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        mock_mem.upsert = AsyncMock(return_value="id")
        mock_mem.create_metadata = AsyncMock(return_value=None)
        await store.store("test content", "src", wing="career", room="applications")

    payload = mock_upsert.call_args.kwargs["payload"]
    assert payload["wing"] == "career"
    assert "wing:career" in payload["tags"]
    assert mock_mem.create_metadata.call_args.kwargs["wing"] == "career"
