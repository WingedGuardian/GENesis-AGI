"""Tests for the skill_edit_regression Critic rubric."""

from __future__ import annotations

from genesis.eval.rubrics import get_rubric


def test_skill_edit_rubric_registered():
    """Importing the package auto-registers the Critic rubric."""
    rubric = get_rubric("skill_edit_regression")
    assert rubric.name == "skill_edit_regression"
    assert rubric.version == "1.0.0"
    # Base placeholders required by LLMJudgeScorer.
    assert "{actual}" in rubric.prompt_template
    assert "{expected}" in rubric.prompt_template
    # Diff-aware extras must be declared AND present in the template.
    for extra in ("removed_content", "change_size", "edit_rationale"):
        assert extra in rubric.extra_placeholders
        assert "{" + extra + "}" in rubric.prompt_template
    # Inverted score direction => threshold below 1.0 so a clean edit passes.
    assert 0.0 < rubric.pass_threshold < 1.0


def test_skill_edit_rubric_template_formats():
    """The template renders with all placeholders — no stray unescaped braces.

    The judge-output JSON example uses ``{{ }}`` escaping; this proves the
    escaping is correct (a bare ``{score}`` would raise KeyError here).
    """
    rubric = get_rubric("skill_edit_regression")
    rendered = rubric.prompt_template.format(
        actual="proposed skill body",
        expected="current skill body",
        removed_content="- a removed guard line",
        change_size="minor",
        edit_rationale="tighten wording",
    )
    assert "proposed skill body" in rendered
    assert "current skill body" in rendered
    assert "a removed guard line" in rendered
    # The literal JSON schema braces survive as single braces in the output.
    assert '"score"' in rendered
    assert '"pathologies"' in rendered
    # All four pathology names are documented in the prompt.
    for pathology in (
        "reward_hacking",
        "catastrophic_forgetting",
        "under_exploration",
        "constraint_stripping",
    ):
        assert pathology in rendered
