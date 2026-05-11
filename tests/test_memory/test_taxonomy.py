"""Tests for memory taxonomy classifier."""

import pytest

from genesis.memory.taxonomy import (
    ROOMS,
    WINGS,
    classify,
    detect_wing_from_prompt,
)


class TestUserWorkWings:
    """The 4 user-work wings added 2026-05-11. Verify classification works
    for the cluster categories that previously polluted general/uncategorized.
    """

    @pytest.mark.parametrize(
        ("content", "expected_wing"),
        [
            # dev_workflow
            ("Opened a pull request to upstream", "dev_workflow"),
            ("Cleaning up the worktree after merge", "dev_workflow"),
            ("GitHub Actions failing on macOS runner", "dev_workflow"),
            ("Ran ruff check and fixed lint issues", "dev_workflow"),
            ("Greptile review surfaced two findings", "dev_workflow"),
            # research
            ("Reading the vLLM paper on continuous batching", "research"),
            ("Latent Space newsletter on AI engineering", "research"),
            ("Skimmed the latest agent zero release notes", "research"),
            ("Honcho session memory benchmark", "research"),
            # integrations
            ("Wiring MiniMax video API into the pipeline", "integrations"),
            ("Abacus AI training run failed", "integrations"),
            ("Switched to LiteLLM for fallback routing", "integrations"),
            # career
            ("Tweaked profile.yml for the new role", "career"),
            ("Drafted job application cover letter", "career"),
            ("CareerOps pipeline broken on the conduit", "career"),
        ],
    )
    def test_new_wing_classification(self, content: str, expected_wing: str):
        result = classify(content)
        assert result.wing == expected_wing, (
            f"Expected {expected_wing!r} for {content!r}, got {result.wing!r}/{result.room!r}"
        )

    def test_new_wings_registered(self):
        for wing in ("dev_workflow", "research", "integrations", "career"):
            assert wing in WINGS, f"{wing} missing from WINGS frozenset"
            assert wing in ROOMS, f"{wing} missing from ROOMS dict"
            assert ROOMS[wing], f"{wing} has empty room list"

    def test_provider_tag_still_routes_to_routing(self):
        """Regression guard: adding the integrations wing must not steal the
        'provider' tag from the routing wing — Genesis-internal model
        providers should still route to `routing`.
        """
        result = classify(
            "config tweak", tags=["provider", "deepinfra"], source_pipeline="",
        )
        assert result.wing == "routing"

    def test_detect_wing_dev_workflow_from_prompt(self):
        wing = detect_wing_from_prompt("I'm working on a pull request review")
        assert wing == "dev_workflow"

    def test_detect_wing_career_from_prompt(self):
        wing = detect_wing_from_prompt("Update my CareerOps profile and ATS integration")
        assert wing == "career"


class TestClassify:
    """Test the classify() function."""

    def test_path_based_classification(self):
        """File paths in content produce high-confidence matches."""
        r = classify("Error in src/genesis/memory/retrieval.py line 42")
        assert r.wing == "memory"
        assert r.room == "retrieval"
        assert r.confidence == 0.9

    def test_path_specific_before_catchall(self):
        """Specific path patterns match before catch-all."""
        r = classify("Fix src/genesis/routing/circuit_breaker.py timeout")
        assert r.wing == "routing"
        assert r.room == "circuit_breakers"
        assert r.confidence == 0.9

    def test_path_catchall(self):
        """Catch-all path matches when no specific pattern hits."""
        r = classify("New file at src/genesis/memory/foobar.py")
        assert r.wing == "memory"
        assert r.room == "store"  # catch-all for memory/

    def test_keyword_classification(self):
        """Content keywords produce medium-confidence matches."""
        r = classify("The Qdrant vector search is returning stale results")
        assert r.wing == "memory"
        assert r.confidence == 0.7

    def test_keyword_routing(self):
        r = classify("Circuit breaker tripped for DeepInfra provider")
        assert r.wing == "routing"

    def test_keyword_earliest_match_wins(self):
        """When multiple keywords match, earliest position wins."""
        r = classify("The router handles telegram messages")
        assert r.wing == "routing"  # "router" appears before "telegram"

    def test_tag_classification(self):
        """Tags produce lower-confidence matches."""
        r = classify("Something happened", tags=["health", "monitoring"])
        assert r.wing == "infrastructure"
        assert r.confidence == 0.6

    def test_tag_skips_class_prefix(self):
        """Tags starting with 'class:' are ignored."""
        r = classify("Generic content", tags=["class:fact"])
        assert r.wing == "general"
        assert r.confidence == 0.1

    def test_tag_skips_json_blobs(self):
        """Tags starting with '{' (garbage JSON) are ignored."""
        r = classify("Content", tags=['{"pattern": "something"}'])
        assert r.wing == "general"

    def test_source_pipeline_classification(self):
        """Source pipeline provides fallback classification."""
        # Use content with no keyword matches so pipeline layer activates
        r = classify("The weather is nice today", source_pipeline="reflection")
        assert r.wing == "learning"
        assert r.room == "reflection"
        assert r.confidence == 0.5

    def test_fallback_general(self):
        """Unclassifiable content falls back to general."""
        r = classify("The weather is nice today")
        assert r.wing == "general"
        assert r.room == "uncategorized"
        assert r.confidence == 0.1

    def test_explicit_wing_room_passthrough(self):
        """When wing/room provided to store, classifier is bypassed."""
        # This tests the store.py logic indirectly — classify still runs
        # but the caller can override. Verify classify returns something.
        r = classify("")
        assert r.wing == "general"

    def test_all_wings_have_rooms(self):
        """Every defined wing has at least one room."""
        for wing in WINGS:
            assert wing in ROOMS, f"Wing {wing} has no rooms defined"
            assert len(ROOMS[wing]) >= 1

    def test_classification_is_frozen(self):
        """Classification dataclass is immutable."""
        r = classify("test")
        try:
            r.wing = "hacked"
            raise AssertionError("Should have raised")  # noqa: TRY301
        except AttributeError:
            pass  # Expected — frozen dataclass


class TestDetectWingFromPrompt:
    """Test the detect_wing_from_prompt() function."""

    def test_file_paths_strongest(self):
        """File paths take priority over prompt keywords."""
        w = detect_wing_from_prompt(
            "How does routing work?",
            file_paths=["src/genesis/memory/store.py"],
        )
        assert w == "memory"  # File path wins over "routing" keyword

    def test_prompt_keywords(self):
        """Keywords in prompt detected without file paths."""
        w = detect_wing_from_prompt("The extraction pipeline is broken")
        assert w == "memory"

    def test_multiple_keywords_vote(self):
        """Wing with most keyword hits wins."""
        w = detect_wing_from_prompt(
            "guardian and sentinel health probes are failing"
        )
        assert w == "infrastructure"

    def test_no_match_returns_none(self):
        """Unrecognizable prompt returns None."""
        w = detect_wing_from_prompt("hello how are you")
        assert w is None

    def test_empty_prompt(self):
        w = detect_wing_from_prompt("")
        assert w is None

    def test_channels_detection(self):
        w = detect_wing_from_prompt("Fix the telegram bot response time")
        assert w == "channels"
