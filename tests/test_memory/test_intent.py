"""Tests for query intent classification and expansion."""

from __future__ import annotations

from genesis.memory.intent import (
    QueryIntent,
    TagCooccurrenceIndex,
    _tokenize_query,
    classify_intent,
    compute_intent_affinity,
    rank_by_intent,
)

# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------


class TestClassifyIntent:
    """Tests for classify_intent()."""

    def test_what_queries(self):
        for q in [
            "what is the cc_relay?",
            "What are observations?",
            "define episodic memory",
            "describe the extraction pipeline",
            "explain what RRF does",
            "tell me about the reflection engine",
        ]:
            intent = classify_intent(q)
            assert intent.category == "WHAT", f"Expected WHAT for: {q!r}, got {intent.category}"
            assert intent.confidence > 0

    def test_why_queries(self):
        for q in [
            "why did we choose subprocess?",
            "Why do we use RRF fusion?",
            "rationale for the confidence gate",
            "what was the reason for skipping Qdrant?",
            "motivation behind the drive system",
        ]:
            intent = classify_intent(q)
            assert intent.category == "WHY", f"Expected WHY for: {q!r}, got {intent.category}"

    def test_how_queries(self):
        for q in [
            "how to run the extraction job?",
            "How does memory linking work?",
            "steps to deploy the bridge",
            "procedure for memory consolidation",
            "process for adding a new module",
        ]:
            intent = classify_intent(q)
            assert intent.category == "HOW", f"Expected HOW for: {q!r}, got {intent.category}"

    def test_when_queries(self):
        for q in [
            "when did we add the linker?",
            "When was the Qdrant incident?",
            "timeline of reflection changes",
            "history of the bootstrap system",
            "last time we updated the schema",
        ]:
            intent = classify_intent(q)
            assert intent.category == "WHEN", f"Expected WHEN for: {q!r}, got {intent.category}"

    def test_where_queries(self):
        for q in [
            "where is the config file?",
            "Where does the bridge run?",
            "which file has the RRF function?",
            "find the memory store implementation",
        ]:
            intent = classify_intent(q)
            assert intent.category == "WHERE", f"Expected WHERE for: {q!r}, got {intent.category}"

    def test_status_queries(self):
        for q in [
            "status of the bookmark feature",
            "progress on V4 work",
            "current state of the ego system",
            "update on the pipeline revival",
            "is it done yet?",
        ]:
            intent = classify_intent(q)
            assert intent.category == "STATUS", f"Expected STATUS for: {q!r}, got {intent.category}"

    def test_general_queries(self):
        """Queries with no intent prefix return GENERAL."""
        for q in [
            "subprocess popen error",
            "cc_relay timeout issues",
            "Qdrant memory",
            "reflection engine",
        ]:
            intent = classify_intent(q)
            assert intent.category == "GENERAL", f"Expected GENERAL for: {q!r}, got {intent.category}"
            assert intent.confidence == 0.0
            assert intent.matched_pattern == ""

    def test_empty_query(self):
        intent = classify_intent("")
        assert intent.category == "GENERAL"
        assert intent.confidence == 0.0

    def test_case_insensitive(self):
        lower = classify_intent("why did we choose subprocess?")
        upper = classify_intent("WHY did we choose subprocess?")
        mixed = classify_intent("Why Did We Choose Subprocess?")
        assert lower.category == upper.category == mixed.category == "WHY"

    def test_why_before_what_priority(self):
        """'what was the reason' should match WHY, not WHAT."""
        intent = classify_intent("what was the reason for the rewrite?")
        assert intent.category == "WHY"


# ---------------------------------------------------------------------------
# Intent affinity scoring
# ---------------------------------------------------------------------------


class TestIntentAffinity:
    """Tests for compute_intent_affinity()."""

    def test_general_intent_returns_zero(self):
        intent = QueryIntent(category="GENERAL", confidence=0.0, matched_pattern="")
        score = compute_intent_affinity(intent, "deep_reflection", ["decision"], "because we decided")
        assert score == 0.0

    def test_source_boost(self):
        intent = QueryIntent(category="WHY", confidence=0.85, matched_pattern="why")
        # deep_reflection is boosted for WHY
        score = compute_intent_affinity(intent, "deep_reflection", [], "")
        assert score >= 2.0

    def test_tag_boost(self):
        intent = QueryIntent(category="WHY", confidence=0.85, matched_pattern="why")
        score = compute_intent_affinity(intent, "other_source", ["decision"], "")
        assert score >= 1.5

    def test_content_signal_boost(self):
        intent = QueryIntent(category="WHY", confidence=0.85, matched_pattern="why")
        score = compute_intent_affinity(intent, "other_source", [], "we decided because of X")
        assert score >= 1.0

    def test_all_boosts_stack(self):
        intent = QueryIntent(category="WHY", confidence=0.85, matched_pattern="why")
        score = compute_intent_affinity(
            intent, "deep_reflection", ["decision"], "we decided because"
        )
        assert score >= 4.5  # 2.0 + 1.5 + 1.0

    def test_no_match_returns_zero(self):
        intent = QueryIntent(category="WHY", confidence=0.85, matched_pattern="why")
        score = compute_intent_affinity(intent, "other_source", ["entity"], "some content")
        assert score == 0.0


# ---------------------------------------------------------------------------
# Intent ranking
# ---------------------------------------------------------------------------


class TestRankByIntent:
    """Tests for rank_by_intent()."""

    def test_general_returns_empty(self):
        intent = QueryIntent(category="GENERAL", confidence=0.0, matched_pattern="")
        result = rank_by_intent(intent, {"m1": {"source": "x", "tags": [], "content": ""}})
        assert result == []

    def test_matching_memories_rank_higher(self):
        intent = QueryIntent(category="WHY", confidence=0.85, matched_pattern="why")
        candidates = {
            "m1": {"source": "deep_reflection", "tags": ["decision"], "content": "we decided"},
            "m2": {"source": "session_extraction", "tags": ["entity"], "content": "the widget"},
            "m3": {"source": "other", "tags": [], "content": "random stuff"},
        }
        ranked = rank_by_intent(intent, candidates)
        assert ranked[0] == "m1"  # Highest affinity
        assert len(ranked) == 3  # All candidates included

    def test_stable_sort_for_ties(self):
        """Memories with equal affinity should sort by ID for stability."""
        intent = QueryIntent(category="WHAT", confidence=0.80, matched_pattern="what")
        candidates = {
            "b": {"source": "other", "tags": [], "content": ""},
            "a": {"source": "other", "tags": [], "content": ""},
        }
        ranked = rank_by_intent(intent, candidates)
        assert ranked == ["a", "b"]  # Alphabetical for ties


# ---------------------------------------------------------------------------
# Query tokenization
# ---------------------------------------------------------------------------


class TestTokenizeQuery:
    """Tests for _tokenize_query()."""

    def test_removes_stop_words(self):
        tokens = _tokenize_query("what is the cc_relay?")
        assert "what" not in tokens
        assert "the" not in tokens

    def test_keeps_meaningful_words(self):
        tokens = _tokenize_query("Qdrant memory incident production")
        assert "qdrant" in tokens
        assert "memory" in tokens
        assert "incident" in tokens
        assert "production" in tokens

    def test_removes_single_char_tokens(self):
        tokens = _tokenize_query("a I x cc_relay")
        assert "a" not in tokens
        assert "x" not in tokens
        # "cc" would be part of "cc_relay" as one token (underscores are word chars)

    def test_empty_query(self):
        assert _tokenize_query("") == []


# ---------------------------------------------------------------------------
# Tag co-occurrence index
# ---------------------------------------------------------------------------


class TestTagCooccurrenceIndex:
    """Tests for TagCooccurrenceIndex."""

    def test_build_and_expand(self):
        index = TagCooccurrenceIndex()
        tag_lists = [
            ["qdrant", "production_data", "incident"],
            ["qdrant", "test_isolation", "incident"],
            ["qdrant", "delete_guard", "production_data"],
            ["memory", "extraction", "pipeline"],
        ]
        index.build(tag_lists, memory_count=100)

        # "qdrant" co-occurs with production_data, incident, test_isolation, delete_guard
        expansions = index.expand(["qdrant"])
        assert len(expansions) > 0
        assert "incident" in expansions or "production_data" in expansions

    def test_expand_empty_index(self):
        index = TagCooccurrenceIndex()
        assert index.expand(["anything"]) == []

    def test_expand_no_keywords(self):
        index = TagCooccurrenceIndex()
        index.build([["a", "b"]], memory_count=10)
        assert index.expand([]) == []

    def test_staleness_detection(self):
        index = TagCooccurrenceIndex()
        assert index.is_stale(100)  # Never built

        index.build([["a", "b"]], memory_count=100)
        assert not index.is_stale(105)  # 5% change, below threshold
        assert index.is_stale(115)  # 15% change, above 10% threshold

    def test_skips_obs_tags(self):
        """Tags starting with 'obs:' should be excluded from co-occurrence."""
        index = TagCooccurrenceIndex()
        tag_lists = [
            ["qdrant", "obs:abc123", "incident"],
        ]
        index.build(tag_lists, memory_count=10)
        # obs:abc123 should not appear in expansions
        expansions = index.expand(["qdrant"], max_expansions=10)
        assert not any(e.startswith("obs:") for e in expansions)

    def test_max_expansions_limit(self):
        index = TagCooccurrenceIndex()
        # Create many co-occurring tags
        tag_lists = [
            ["root", f"tag_{i}", f"tag_{i+1}"]
            for i in range(20)
        ]
        index.build(tag_lists, memory_count=100)
        expansions = index.expand(["root"], max_expansions=3)
        assert len(expansions) <= 3

    def test_expansion_excludes_query_keywords(self):
        """Expansion should not include terms already in the query."""
        index = TagCooccurrenceIndex()
        tag_lists = [
            ["qdrant", "incident", "production"],
            ["qdrant", "incident", "recovery"],
        ]
        index.build(tag_lists, memory_count=10)
        expansions = index.expand(["qdrant", "incident"])
        assert "qdrant" not in expansions
        assert "incident" not in expansions
