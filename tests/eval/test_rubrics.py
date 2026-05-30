"""Tests for eval rubric registration and structure."""

from __future__ import annotations

import pytest

from genesis.eval.rubrics import Rubric, get_rubric, list_rubrics


class TestRubricRegistry:
    """Verify rubric auto-registration from submodules."""

    def test_reflection_quality_registered(self) -> None:
        rubric = get_rubric("reflection_quality")
        assert isinstance(rubric, Rubric)
        assert rubric.name == "reflection_quality"
        assert rubric.version == "1.0.0"

    def test_reflection_quality_threshold(self) -> None:
        rubric = get_rubric("reflection_quality")
        assert rubric.pass_threshold == 0.6

    def test_reflection_quality_placeholders(self) -> None:
        rubric = get_rubric("reflection_quality")
        assert "session_context" in rubric.extra_placeholders

    def test_reflection_quality_prompt_has_placeholders(self) -> None:
        rubric = get_rubric("reflection_quality")
        assert "{actual}" in rubric.prompt_template
        assert "{expected}" in rubric.prompt_template
        assert "{session_context}" in rubric.prompt_template

    def test_reflection_quality_prompt_format(self) -> None:
        """Verify the prompt template can be formatted without error."""
        rubric = get_rubric("reflection_quality")
        formatted = rubric.prompt_template.format(
            actual="test observation content",
            expected="deep_reflection_observation",
            session_context="Reflection depth: Deep",
        )
        assert "test observation content" in formatted
        assert "Reflection depth: Deep" in formatted

    def test_memory_recall_grounding_still_registered(self) -> None:
        """Ensure we didn't break the existing rubric."""
        rubric = get_rubric("memory_recall_grounding")
        assert rubric.name == "memory_recall_grounding"

    def test_list_rubrics_includes_both(self) -> None:
        rubrics = list_rubrics()
        names = [r.name for r in rubrics]
        assert "memory_recall_grounding" in names
        assert "reflection_quality" in names

    def test_unknown_rubric_raises(self) -> None:
        with pytest.raises(KeyError, match="unknown rubric"):
            get_rubric("nonexistent_rubric")
