"""Tests for genesis.research.types."""

from genesis.research.types import ResearchResult, SearchResult


class TestSearchResult:
    def test_frozen(self):
        r = SearchResult(title="T", url="http://x", snippet="S", source="searxng")
        assert r.title == "T"
        assert r.score == 0.0

    def test_with_score(self):
        r = SearchResult(title="T", url="http://x", snippet="S", source="brave", score=0.9)
        assert r.score == 0.9


class TestResearchResult:
    def test_defaults(self):
        r = ResearchResult(query="test")
        assert r.results == []
        assert r.sources_queried == []
        assert r.deduplicated_count == 0
        assert r.synthesis is None

    def test_with_results(self):
        results = [SearchResult(title="A", url="http://a", snippet="", source="x")]
        r = ResearchResult(query="q", results=results, sources_queried=["x"])
        assert len(r.results) == 1
