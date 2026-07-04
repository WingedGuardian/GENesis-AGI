"""Tests for scripts/generate_skill_catalog.py — container recursion + entry shape.

The invariants under test:
  * a directory whose children hold SKILL.md files (gitnexus-style
    ``container/<skill>/SKILL.md``, or plugin-repo style
    ``vendor/<plugin>/skills/<skill>/SKILL.md``) is a CONTAINER — the nested
    real skills are indexed and NO phantom entry is emitted for the container;
  * inside a container, support dirs (hooks/, scripts/) are skipped, never
    indexed as phantom skills;
  * every catalog entry always carries a ``keywords`` key — the no-SKILL.md
    fallback emits ``"keywords": []`` rather than omitting the key.

All fixtures are synthetic tmp_path trees — no dependence on the live repo
or ~/.genesis state.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Load the stdlib script as a module (not a package — use importlib).
_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "generate_skill_catalog.py"
)
_spec = importlib.util.spec_from_file_location("generate_skill_catalog", _SCRIPT_PATH)
_gen = importlib.util.module_from_spec(_spec)
sys.modules["generate_skill_catalog"] = _gen
_spec.loader.exec_module(_gen)


def _mk_skill(skill_dir: Path, name: str, description: str = "does things") -> None:
    """Create a skill dir with a minimal SKILL.md."""
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n"
    )


def _names(results: list[dict]) -> set[str]:
    return {r["name"] for r in results}


def test_scan_tier_indexes_direct_skills(tmp_path):
    _mk_skill(tmp_path / "alpha", "alpha")
    _mk_skill(tmp_path / "beta", "beta")

    results = _gen._scan_tier(tmp_path, 2, None)

    assert _names(results) == {"alpha", "beta"}
    for entry in results:
        assert entry["tier"] == 2
        assert entry["path"].startswith(str(tmp_path))


def test_scan_tier_recurses_into_container(tmp_path):
    """gitnexus-style: container/<skill>/SKILL.md — no phantom container entry."""
    container = tmp_path / "gitnexus"
    _mk_skill(container / "gitnexus-cli", "gitnexus-cli")
    _mk_skill(container / "gitnexus-debugging", "gitnexus-debugging")

    results = _gen._scan_tier(tmp_path, 1, None)

    assert _names(results) == {"gitnexus-cli", "gitnexus-debugging"}
    assert "gitnexus" not in _names(results)
    # Paths point at the real skill dirs, not the container
    paths = {r["path"] for r in results}
    assert str(container / "gitnexus-cli") in paths


def test_scan_tier_recurses_plugin_repo_layout(tmp_path):
    """vendor/<plugin>/skills/<skill>/SKILL.md is indexed; support dirs skipped."""
    plugin = tmp_path / "aws" / "aws-serverless"
    _mk_skill(plugin / "skills" / "api-gateway", "api-gateway")
    _mk_skill(plugin / "skills" / "aws-lambda", "aws-lambda")
    # Support dirs inside the plugin repo must not become phantom entries
    (plugin / "hooks").mkdir(parents=True)
    (plugin / "hooks" / "hook.sh").write_text("#!/bin/sh\n")
    (plugin / "scripts").mkdir()

    results = _gen._scan_tier(tmp_path, 2, None)

    assert _names(results) == {"api-gateway", "aws-lambda"}
    for phantom in ("aws", "aws-serverless", "skills", "hooks", "scripts"):
        assert phantom not in _names(results)


def test_scan_tier_container_readme_does_not_shadow_nested_skills(tmp_path):
    """A container with README.md but no SKILL.md still recurses."""
    container = tmp_path / "toolkit"
    _mk_skill(container / "inner-skill", "inner-skill")
    (container / "README.md").write_text(
        "---\nname: toolkit\ndescription: container readme\n---\n"
    )

    results = _gen._scan_tier(tmp_path, 2, None)

    assert _names(results) == {"inner-skill"}


def test_scan_tier_fallback_entry_has_empty_keywords_list(tmp_path):
    """Top-level dir with no markdown at all: fallback entry, keywords == []."""
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    (lonely / "notes.txt").write_text("not a skill file")

    results = _gen._scan_tier(tmp_path, 2, None)

    assert len(results) == 1
    entry = results[0]
    assert entry["name"] == "lonely"
    assert entry["description"] == ""
    assert entry["keywords"] == []


def test_scan_tier_every_entry_has_keywords_key(tmp_path):
    """Catalog shape: the keywords key is always present, whatever the source."""
    _mk_skill(tmp_path / "real-skill", "real-skill")
    container = tmp_path / "container"
    _mk_skill(container / "nested-skill", "nested-skill")
    (tmp_path / "bare").mkdir()

    results = _gen._scan_tier(tmp_path, 2, None)

    assert _names(results) == {"real-skill", "nested-skill", "bare"}
    for entry in results:
        assert "keywords" in entry, f"missing keywords key: {entry['name']}"
        assert isinstance(entry["keywords"], list)


def test_scan_tier_skips_hidden_dirs(tmp_path):
    """Hidden dirs (.claude-plugin, .git) are skipped at every level."""
    _mk_skill(tmp_path / ".hidden" / "sneaky", "sneaky")
    container = tmp_path / "vendor"
    _mk_skill(container / "plug" / "skills" / "real", "real")
    hidden_in_container = container / ".claude-plugin"
    hidden_in_container.mkdir(parents=True)
    (hidden_in_container / "plugin.json").write_text("{}")

    results = _gen._scan_tier(tmp_path, 2, None)

    assert _names(results) == {"real"}


def test_scan_tier_relative_paths_under_repo_root(tmp_path):
    """Nested skill paths are repo-relative when under repo_root."""
    repo = tmp_path / "repo"
    tier = repo / ".claude" / "skills"
    _mk_skill(tier / "gitnexus" / "gitnexus-cli", "gitnexus-cli")

    results = _gen._scan_tier(tier, 1, repo)

    assert len(results) == 1
    assert results[0]["path"] == str(
        Path(".claude") / "skills" / "gitnexus" / "gitnexus-cli"
    )
