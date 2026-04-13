"""Tests for skill injection hook logic."""
from __future__ import annotations

import sys
from pathlib import Path

# Add scripts dirs to path for import
_scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_scripts_dir / "hooks"))
sys.path.insert(0, str(_scripts_dir))


def test_score_skill_name_match():
    from skill_injection_hook import _score_skill

    skill = {"name": "genesis-development", "description": "For building Genesis"}
    assert _score_skill(skill, ["genesis", "development"]) > 0.5


def test_score_skill_no_match():
    from skill_injection_hook import _score_skill

    skill = {"name": "youtube-fetch", "description": "Fetch YouTube videos"}
    assert _score_skill(skill, ["database", "migration"]) == 0.0


def test_score_skill_description_match():
    from skill_injection_hook import _score_skill

    skill = {
        "name": "evaluate",
        "description": "Evaluate technologies against Genesis architecture",
    }
    assert _score_skill(skill, ["evaluate", "technology"]) > 0.3


def test_score_skill_empty_keywords():
    from skill_injection_hook import _score_skill

    skill = {"name": "anything", "description": "whatever"}
    assert _score_skill(skill, []) == 0.0


def test_extract_keywords():
    from skill_injection_hook import _extract_keywords

    keywords = _extract_keywords("Can you help me debug the memory system?")
    assert "memory" in keywords
    assert "debug" in keywords
    assert "the" not in keywords
    assert "can" not in keywords


def test_extract_keywords_limit():
    from skill_injection_hook import _extract_keywords

    long_prompt = " ".join(f"word{i}" for i in range(50))
    keywords = _extract_keywords(long_prompt)
    assert len(keywords) <= 10


def test_catalog_parse_frontmatter():
    """Generate and load a catalog."""
    from generate_skill_catalog import _parse_frontmatter

    info = _parse_frontmatter(
        '---\nname: test-skill\ndescription: "A test"\n---\n# Content'
    )
    assert info["name"] == "test-skill"
    assert info["description"] == "A test"


def test_catalog_parse_frontmatter_no_yaml():
    from generate_skill_catalog import _parse_frontmatter

    info = _parse_frontmatter("# Just a heading\nSome content", fallback_name="fallback")
    assert info["name"] == "fallback"
    assert info["description"] == ""


def test_catalog_parse_folded_scalar():
    """YAML folded scalar (>) descriptions are joined into one line."""
    from generate_skill_catalog import _parse_frontmatter

    content = (
        "---\n"
        "name: my-skill\n"
        "description: >\n"
        "  This skill is used when developing,\n"
        "  debugging, or refactoring Genesis.\n"
        "---\n"
        "# Content\n"
    )
    info = _parse_frontmatter(content)
    assert info["name"] == "my-skill"
    assert "developing" in info["description"]
    assert "refactoring" in info["description"]
    # Should be joined into a single string, not contain ">"
    assert info["description"] != ">"
