"""Tests for genesis.pipeline.types."""

from genesis.pipeline.types import (
    CollectorResult,
    PipelineRunResult,
    ResearchSignal,
    SignalStatus,
    Tier,
)


class TestTier:
    def test_ordering(self):
        assert Tier.COLLECTION < Tier.TRIAGE < Tier.ANALYSIS < Tier.JUDGMENT

    def test_int_values(self):
        assert int(Tier.COLLECTION) == 0
        assert int(Tier.TRIAGE) == 1
        assert int(Tier.ANALYSIS) == 2
        assert int(Tier.JUDGMENT) == 3

    def test_comparison(self):
        assert Tier.JUDGMENT > Tier.COLLECTION
        assert Tier.TRIAGE >= Tier.TRIAGE


class TestSignalStatus:
    def test_values(self):
        assert SignalStatus.RAW == "raw"
        assert SignalStatus.TRIAGED == "triaged"
        assert SignalStatus.ANALYZED == "analyzed"
        assert SignalStatus.ACTIONABLE == "actionable"
        assert SignalStatus.DISCARDED == "discarded"

    def test_all_statuses_are_strings(self):
        for s in SignalStatus:
            assert isinstance(s, str)


class TestResearchSignal:
    def test_creation_with_defaults(self):
        s = ResearchSignal(
            id="sig-1",
            source="searxng",
            profile_name="crypto",
            content="Bitcoin hits $100k",
        )
        assert s.id == "sig-1"
        assert s.source == "searxng"
        assert s.profile_name == "crypto"
        assert s.content == "Bitcoin hits $100k"
        assert s.url is None
        assert s.tier == Tier.COLLECTION
        assert s.status == SignalStatus.RAW
        assert s.relevance_score == 0.0
        assert s.confidence == 0.0
        assert s.tags == []
        assert s.metadata == {}
        assert s.collected_at == ""
        assert s.promoted_at is None

    def test_creation_with_all_fields(self):
        s = ResearchSignal(
            id="sig-2",
            source="reddit",
            profile_name="ai",
            content="New model released",
            url="https://example.com",
            tier=Tier.ANALYSIS,
            status=SignalStatus.ANALYZED,
            relevance_score=0.85,
            confidence=0.7,
            tags=["ai", "models"],
            metadata={"subreddit": "machinelearning"},
            collected_at="2026-03-12T00:00:00Z",
            promoted_at="2026-03-12T01:00:00Z",
        )
        assert s.tier == Tier.ANALYSIS
        assert s.relevance_score == 0.85
        assert s.tags == ["ai", "models"]

    def test_mutable_defaults_are_independent(self):
        s1 = ResearchSignal(id="a", source="x", profile_name="p", content="c")
        s2 = ResearchSignal(id="b", source="x", profile_name="p", content="c")
        s1.tags.append("test")
        assert s2.tags == []


class TestCollectorResult:
    def test_defaults(self):
        r = CollectorResult(collector_name="web", signals=[])
        assert r.collector_name == "web"
        assert r.signals == []
        assert r.errors == []

    def test_with_signals_and_errors(self):
        sig = ResearchSignal(id="1", source="s", profile_name="p", content="c")
        r = CollectorResult(
            collector_name="web",
            signals=[sig],
            errors=["timeout on query 2"],
        )
        assert len(r.signals) == 1
        assert len(r.errors) == 1


class TestPipelineRunResult:
    def test_defaults(self):
        r = PipelineRunResult(profile_name="crypto")
        assert r.profile_name == "crypto"
        assert r.tier0_collected == 0
        assert r.tier1_survived == 0
        assert r.tier2_survived == 0
        assert r.tier3_surfaced == 0
        assert r.discarded == 0
        assert r.errors == []

    def test_aggregation(self):
        r = PipelineRunResult(
            profile_name="ai",
            tier0_collected=100,
            tier1_survived=30,
            tier2_survived=10,
            tier3_surfaced=3,
            discarded=70,
            errors=["one error"],
        )
        assert r.tier0_collected == 100
        assert r.discarded == 70
        assert len(r.errors) == 1
