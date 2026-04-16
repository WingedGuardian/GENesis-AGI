"""Tests for skills inventory, wiring, and SKILL.md files."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from genesis.learning.skills.inventory import (
    get_skills_for_consumer,
    get_skills_for_phase,
)
from genesis.learning.skills.wiring import (
    get_skill_path,
    list_available_skills,
    load_skill,
)

# ── inventory tests ──


class TestInventory:
    def test_phase_6_skills(self):
        p6 = get_skills_for_phase(6)
        assert "evaluate" in p6
        assert "retrospective" in p6
        assert "research" in p6
        assert "debugging" in p6
        assert "obstacle-resolution" in p6
        assert "triage-calibration" in p6

    def test_phase_7_skills(self):
        p7 = get_skills_for_phase(7)
        assert "deep-reflection" in p7
        assert "strategic-reflection" in p7

    def test_consumer_filter(self):
        research = get_skills_for_consumer("cc_background_research")
        assert "evaluate" in research
        assert "research" in research
        assert "retrospective" not in research

    def test_nonexistent_phase(self):
        assert get_skills_for_phase(99) == {}

    def test_nonexistent_consumer(self):
        assert get_skills_for_consumer("nonexistent") == {}


# ── wiring tests ──


class TestWiring:
    def test_list_available_skills(self, tmp_path: Path, monkeypatch):
        (tmp_path / "alpha").mkdir()
        (tmp_path / "alpha" / "SKILL.md").write_text("skill alpha")
        (tmp_path / "beta").mkdir()  # no SKILL.md
        (tmp_path / "gamma").mkdir()
        (tmp_path / "gamma" / "SKILL.md").write_text("skill gamma")
        monkeypatch.setattr(
            "genesis.learning.skills.wiring._GENESIS_SKILLS_DIR", tmp_path
        )
        # wiring.py now scans two tiers (core + .claude/); patch both so the
        # real .claude/skills/ dir doesn't leak into the test result.
        monkeypatch.setattr(
            "genesis.learning.skills.wiring._CLAUDE_SKILLS_DIR",
            tmp_path / "nonexistent",
        )
        assert list_available_skills() == ["alpha", "gamma"]

    def test_get_skill_path(self, tmp_path: Path, monkeypatch):
        (tmp_path / "foo").mkdir()
        (tmp_path / "foo" / "SKILL.md").write_text("content")
        monkeypatch.setattr(
            "genesis.learning.skills.wiring._GENESIS_SKILLS_DIR", tmp_path
        )
        monkeypatch.setattr(
            "genesis.learning.skills.wiring._CLAUDE_SKILLS_DIR",
            tmp_path / "nonexistent",
        )
        assert get_skill_path("foo") == tmp_path / "foo" / "SKILL.md"
        assert get_skill_path("missing") is None

    def test_load_skill(self, tmp_path: Path, monkeypatch):
        (tmp_path / "bar").mkdir()
        (tmp_path / "bar" / "SKILL.md").write_text("bar content")
        monkeypatch.setattr(
            "genesis.learning.skills.wiring._GENESIS_SKILLS_DIR", tmp_path
        )
        monkeypatch.setattr(
            "genesis.learning.skills.wiring._CLAUDE_SKILLS_DIR",
            tmp_path / "nonexistent",
        )
        assert load_skill("bar") == "bar content"
        assert load_skill("nope") is None

    def test_list_empty(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            "genesis.learning.skills.wiring._GENESIS_SKILLS_DIR",
            tmp_path / "nonexistent",
        )
        monkeypatch.setattr(
            "genesis.learning.skills.wiring._CLAUDE_SKILLS_DIR",
            tmp_path / "also-nonexistent",
        )
        assert list_available_skills() == []


# ── SKILL.md validation ──

EXPECTED_SKILLS = [
    "evaluate",
    "retrospective",
    "research",
    "debugging",
    "obstacle-resolution",
    "triage-calibration",
]

SKILLS_ROOT = Path(__file__).resolve().parent.parent.parent / "src" / "genesis" / "skills"


@pytest.mark.parametrize("skill_name", EXPECTED_SKILLS)
class TestSkillMdFiles:
    def test_exists(self, skill_name: str):
        path = SKILLS_ROOT / skill_name / "SKILL.md"
        assert path.exists(), f"Missing {path}"

    def test_valid_frontmatter(self, skill_name: str):
        path = SKILLS_ROOT / skill_name / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n"), "Must start with YAML frontmatter"
        end = text.index("---\n", 4)
        fm = yaml.safe_load(text[4:end])
        assert fm["name"] == skill_name
        assert "description" in fm
        assert "consumer" in fm
        assert fm["phase"] == 6
