"""Tests for memory classification (rule/fact/reference)."""

from genesis.memory.classification import CLASS_WEIGHTS, classify_memory


class TestClassifyMemory:
    """classify_memory returns the correct class for various inputs."""

    def test_cc_feedback_is_rule(self):
        assert classify_memory("some content", cc_memory_type="feedback") == "rule"

    def test_cc_user_is_rule(self):
        assert classify_memory("some content", cc_memory_type="user") == "rule"

    def test_cc_project_is_fact(self):
        assert classify_memory("some content", cc_memory_type="project") == "fact"

    def test_cc_reference_is_reference(self):
        assert classify_memory("some content", cc_memory_type="reference") == "reference"

    def test_cc_type_overrides_content_heuristics(self):
        # Even though content has NEVER, cc_memory_type wins
        assert classify_memory("NEVER do this", cc_memory_type="project") == "fact"

    def test_imperative_always_is_rule(self):
        assert classify_memory("ALWAYS run tests before committing") == "rule"

    def test_imperative_never_is_rule(self):
        assert classify_memory("NEVER use git add .") == "rule"

    def test_imperative_must_is_rule(self):
        assert classify_memory("You MUST verify before claiming done") == "rule"

    def test_imperative_do_not_is_rule(self):
        assert classify_memory("DO NOT push to main without review") == "rule"

    def test_imperative_critical_is_rule(self):
        assert classify_memory("CRITICAL: always check the schema first") == "rule"

    def test_url_heavy_is_reference(self):
        assert classify_memory(
            "Check https://grafana.internal/d/api-latency for details"
        ) == "reference"

    def test_tracked_in_is_reference(self):
        assert classify_memory("Pipeline bugs are tracked in Linear project INGEST") == "reference"

    def test_see_also_is_reference(self):
        assert classify_memory("For more details, see also the architecture docs") == "reference"

    def test_plain_fact_default(self):
        assert classify_memory("Session discussed deployment strategy for Q2") == "fact"

    def test_session_extraction_default(self):
        assert classify_memory(
            "The team evaluated three options for caching",
            source="session_extraction",
        ) == "fact"

    def test_empty_content_is_fact(self):
        assert classify_memory("") == "fact"

    def test_unknown_cc_type_falls_through(self):
        # Unknown cc_memory_type is ignored, falls to content heuristics
        assert classify_memory("NEVER ignore this", cc_memory_type="unknown") == "rule"


class TestClassWeights:
    """CLASS_WEIGHTS has correct values."""

    def test_rule_weight(self):
        assert CLASS_WEIGHTS["rule"] == 1.3

    def test_fact_weight(self):
        assert CLASS_WEIGHTS["fact"] == 1.0

    def test_reference_weight(self):
        assert CLASS_WEIGHTS["reference"] == 0.7
