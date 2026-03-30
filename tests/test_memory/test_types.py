from __future__ import annotations

import pytest

from genesis.memory.types import (
    ActivationScore,
    LinkRecord,
    MemoryRecord,
    RetrievalResult,
    UserModelSnapshot,
)


def test_memory_record_frozen():
    r = MemoryRecord(
        id="m1", content="hello", source="test", memory_type="observation",
        tags=("a",), confidence=0.9, created_at="2026-01-01T00:00:00+00:00",
        retrieved_count=0, link_count=0,
    )
    assert r.id == "m1"
    with pytest.raises(AttributeError):
        r.id = "m2"  # type: ignore[misc]


def test_activation_score_frozen():
    a = ActivationScore(
        memory_id="m1", base_score=0.8, recency_factor=1.0,
        access_frequency=0.0, connectivity_factor=0.0, final_score=0.4,
    )
    assert a.final_score == 0.4
    with pytest.raises(AttributeError):
        a.final_score = 0.0  # type: ignore[misc]


def test_retrieval_result_fields():
    r = RetrievalResult(
        memory_id="m1", content="x", source="s", memory_type="t",
        score=0.5, vector_rank=1, fts_rank=None, activation_score=0.3,
        payload={"k": "v"},
    )
    assert r.vector_rank == 1
    assert r.fts_rank is None
    assert r.payload == {"k": "v"}


def test_link_record_frozen():
    link = LinkRecord(
        source_id="a", target_id="b", link_type="related",
        strength=0.7, created_at="2026-01-01T00:00:00+00:00",
    )
    assert link.strength == 0.7
    with pytest.raises(AttributeError):
        link.strength = 0.0  # type: ignore[misc]


def test_user_model_snapshot_fields():
    s = UserModelSnapshot(
        model={"pref": "dark"}, version=3, evidence_count=12,
        synthesized_at="2026-01-01T00:00:00+00:00",
    )
    assert s.version == 3
    assert s.model["pref"] == "dark"
