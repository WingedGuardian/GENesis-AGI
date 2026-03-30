"""Tests for genesis.pipeline.triage."""

from __future__ import annotations

import pytest

from genesis.pipeline.profiles import ResearchProfile
from genesis.pipeline.triage import TriageFilter
from genesis.pipeline.types import ResearchSignal, SignalStatus, Tier


def _make_signal(content: str, **kwargs) -> ResearchSignal:
    """Helper to create a test signal."""
    defaults = {
        "id": f"sig-{id(content)}",
        "source": "test",
        "profile_name": "test_profile",
        "content": content,
        "tier": Tier.COLLECTION,
        "status": SignalStatus.RAW,
    }
    defaults.update(kwargs)
    return ResearchSignal(**defaults)


def _make_profile(**kwargs) -> ResearchProfile:
    defaults = {
        "name": "test_profile",
        "relevance_keywords": [],
        "exclude_keywords": [],
    }
    defaults.update(kwargs)
    return ResearchProfile(**defaults)


class TestTriageFilterKeywordFallback:
    async def test_filters_by_relevance_keywords(self):
        triage = TriageFilter()
        profile = _make_profile(relevance_keywords=["bitcoin", "ethereum", "crypto"])
        signals = [
            _make_signal("Bitcoin price surges to new high"),
            _make_signal("Cat videos are trending today"),
            _make_signal("Ethereum gas fees drop significantly"),
        ]
        result = await triage.triage(signals, profile)
        contents = [s.content for s in result]
        assert any("Bitcoin" in c for c in contents)
        assert any("Ethereum" in c for c in contents)
        assert not any("Cat" in c for c in contents)

    async def test_excludes_by_exclude_keywords(self):
        triage = TriageFilter()
        profile = _make_profile(
            relevance_keywords=["bitcoin", "ethereum"],
            exclude_keywords=["scam"],
        )
        signals = [
            _make_signal("Bitcoin price analysis"),
            _make_signal("Bitcoin scam warning alert"),
            _make_signal("Ethereum development update"),
        ]
        result = await triage.triage(signals, profile)
        contents = [s.content for s in result]
        assert not any("scam" in c.lower() for c in contents)

    async def test_no_keywords_passes_everything_at_half(self):
        triage = TriageFilter()
        profile = _make_profile(relevance_keywords=[], exclude_keywords=[])
        signals = [
            _make_signal("Anything goes"),
            _make_signal("Whatever content"),
        ]
        result = await triage.triage(signals, profile)
        assert len(result) == 2
        for s in result:
            assert s.relevance_score == pytest.approx(0.5, abs=0.01)

    async def test_empty_signals_returns_empty(self):
        triage = TriageFilter()
        profile = _make_profile(relevance_keywords=["test"])
        result = await triage.triage([], profile)
        assert result == []

    async def test_signals_get_tier_and_status_updates(self):
        triage = TriageFilter()
        profile = _make_profile(relevance_keywords=["bitcoin"])
        signals = [_make_signal("Relevant bitcoin content")]
        result = await triage.triage(signals, profile)
        assert len(result) == 1
        assert result[0].tier == Tier.TRIAGE
        assert result[0].status == SignalStatus.TRIAGED
        assert result[0].relevance_score > 0
