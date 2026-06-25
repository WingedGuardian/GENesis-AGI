"""Tests for the relative knowledge-base score floor (audit MEM-004).

The floor must be scale-invariant: it drops the weak KB tail relative to the
strongest KB hit in the same result set, so the single best KB result always
survives regardless of whether scores are positional (reranker-on) or
RRF/FTS-mapped (reranker-off). A fixed absolute floor could not do this.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class _R:
    """Minimal RetrievalResult stand-in (core.py result shape)."""

    score: float
    collection: str


def _obj_score(r: _R) -> float:
    return r.score


def _obj_is_kb(r: _R) -> bool:
    return r.collection == "knowledge_base"


def _dict_score(r: dict) -> float:
    return r.get("score", 0.0)


class TestRelativeKbFloor:
    def test_keeps_strongest_kb_hit(self):
        """The single best KB result must always survive — never wipe all KB."""
        from genesis.mcp.memory._scoring import relative_kb_floor

        results = [
            _R(1.0, "episodic_memory"),
            _R(0.33, "knowledge_base"),  # top KB
            _R(0.05, "knowledge_base"),  # weak tail: 0.05 < 0.2*0.33
        ]
        kept = relative_kb_floor(
            results, ratio=0.2, score_of=_obj_score, is_kb=_obj_is_kb,
        )
        assert _R(0.33, "knowledge_base") in kept
        assert _R(0.05, "knowledge_base") not in kept

    def test_non_kb_always_kept(self):
        """Episodic results are never floored, even below the KB floor."""
        from genesis.mcp.memory._scoring import relative_kb_floor

        results = [_R(0.01, "episodic_memory"), _R(1.0, "knowledge_base")]
        kept = relative_kb_floor(
            results, ratio=0.2, score_of=_obj_score, is_kb=_obj_is_kb,
        )
        assert _R(0.01, "episodic_memory") in kept

    def test_noop_when_no_kb(self):
        from genesis.mcp.memory._scoring import relative_kb_floor

        results = [_R(0.5, "episodic_memory"), _R(0.01, "episodic_memory")]
        kept = relative_kb_floor(
            results, ratio=0.2, score_of=_obj_score, is_kb=_obj_is_kb,
        )
        assert kept == results

    def test_noop_when_ratio_zero(self):
        """ratio <= 0 disables the floor entirely."""
        from genesis.mcp.memory._scoring import relative_kb_floor

        results = [_R(1.0, "knowledge_base"), _R(0.001, "knowledge_base")]
        kept = relative_kb_floor(
            results, ratio=0.0, score_of=_obj_score, is_kb=_obj_is_kb,
        )
        assert kept == results

    def test_scale_invariance(self):
        """Same survivors whether scores are positional or RRF-scaled down."""
        from genesis.mcp.memory._scoring import relative_kb_floor

        base = [
            _R(1.0, "knowledge_base"),
            _R(0.5, "knowledge_base"),
            _R(0.1, "knowledge_base"),  # 0.1 < 0.2 -> dropped
        ]
        scaled = [_R(r.score * 0.05, r.collection) for r in base]  # RRF-like
        keep_base = relative_kb_floor(
            base, ratio=0.2, score_of=_obj_score, is_kb=_obj_is_kb,
        )
        keep_scaled = relative_kb_floor(
            scaled, ratio=0.2, score_of=_obj_score, is_kb=_obj_is_kb,
        )
        assert [r.score for r in keep_base] == [1.0, 0.5]
        assert [round(r.score, 4) for r in keep_scaled] == [0.05, 0.025]

    def test_dict_shape_all_kb(self):
        """Works on plain dicts (knowledge.py shape), where every item is KB."""
        from genesis.mcp.memory._scoring import relative_kb_floor

        results = [
            {"unit_id": "a", "score": 1.0},
            {"unit_id": "b", "score": 0.5},
            {"unit_id": "c", "score": 0.1},  # 0.1 < 0.2 -> dropped
        ]
        kept = relative_kb_floor(
            results, ratio=0.2, score_of=_dict_score, is_kb=lambda r: True,
        )
        assert [r["unit_id"] for r in kept] == ["a", "b"]

    def test_preserves_order(self):
        """The floor filters but never re-sorts."""
        from genesis.mcp.memory._scoring import relative_kb_floor

        results = [
            _R(0.5, "knowledge_base"),
            _R(1.0, "episodic_memory"),
            _R(1.0, "knowledge_base"),
        ]
        kept = relative_kb_floor(
            results, ratio=0.2, score_of=_obj_score, is_kb=_obj_is_kb,
        )
        assert [(r.score, r.collection) for r in kept] == [
            (0.5, "knowledge_base"),
            (1.0, "episodic_memory"),
            (1.0, "knowledge_base"),
        ]
