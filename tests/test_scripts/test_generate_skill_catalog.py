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


def _fm(name: str, body: str) -> str:
    """A SKILL.md string with the given frontmatter body."""
    return f"---\nname: {name}\n{body}\n---\n# {name}\n"


class TestParseFrontmatter:
    """Descriptions must be parsed COMPLETELY — the regex parser truncated at
    apostrophes (unquoted), escaped quotes (double-quoted), and newlines
    (plain multi-line scalars), losing searchable text (regression: the live
    catalog cut `genesis-voice` at "user's", `gitnexus-*` at `Examples: \\`,
    and `code-intelligence`'s continuation line)."""

    def test_unquoted_apostrophe_not_truncated(self):
        # The genesis-voice failure: an apostrophe in an unquoted scalar.
        content = _fm(
            "genesis-voice",
            "description: Apply when Genesis writes as itself. Not for writing "
            "in the user's voice (that's voice-master).",
        )
        desc = _gen._parse_frontmatter(content)["description"]
        assert "user's voice" in desc
        assert "voice-master" in desc

    def test_double_quoted_escaped_quote_not_truncated(self):
        # The gitnexus-* failure: an escaped quote inside a double-quoted scalar.
        content = _fm(
            "gitnexus-cli",
            'description: "Run CLI commands. Examples: \\"analyze a repo\\", '
            '\\"check status\\"."',
        )
        desc = _gen._parse_frontmatter(content)["description"]
        assert "analyze a repo" in desc
        assert "check status" in desc

    def test_plain_multiline_continuation_kept(self):
        # The code-intelligence failure: a plain scalar wrapped across lines.
        content = _fm(
            "code-intelligence",
            "description: Code understanding tool selection. Use when exploring "
            "architecture,\n  finding definitions, or tracing execution.",
        )
        desc = _gen._parse_frontmatter(content)["description"]
        assert "finding definitions" in desc
        assert "tracing execution" in desc

    def test_folded_scalar_joined_single_line(self):
        content = _fm("x", "description: >\n  Line one\n  line two.")
        assert _gen._parse_frontmatter(content)["description"] == "Line one line two."

    def test_literal_scalar_normalized_single_line(self):
        # Literal `|` keeps newlines in YAML; the catalog wants one scannable line.
        content = _fm("x", "description: |\n  Multi\n  line desc.")
        assert _gen._parse_frontmatter(content)["description"] == "Multi line desc."

    def test_keywords_list_parsed(self):
        content = _fm("x", "description: y\nkeywords: [alpha, beta, gamma]")
        assert _gen._parse_frontmatter(content)["keywords"] == [
            "alpha",
            "beta",
            "gamma",
        ]

    def test_simple_fields_still_work(self):
        content = _fm("plain", "description: does a thing")
        result = _gen._parse_frontmatter(content)
        assert result["name"] == "plain"
        assert result["description"] == "does a thing"

    def test_no_frontmatter_uses_fallback_name(self):
        result = _gen._parse_frontmatter("# just a heading\n", fallback_name="fb")
        assert result["name"] == "fb"
        assert result["description"] == ""
        assert result["keywords"] == []

    def test_falls_back_when_pyyaml_missing(self, monkeypatch):
        # No-PyYAML degraded path: routes through _parse_frontmatter_legacy and
        # still returns the correct shape for a simple (untruncatable) scalar.
        monkeypatch.setattr(_gen, "yaml", None)
        result = _gen._parse_frontmatter(_fm("x", "description: plain desc"))
        assert result == {"name": "x", "description": "plain desc", "keywords": []}

    def test_boolean_like_scalars_kept_as_strings(self):
        # yaml.safe_load coerces YAML 1.1 booleans; the catalog needs the
        # authored spelling (e.g. an "off" keyword) for matching.
        content = _fm(
            "x", "description: y\nkeywords: [off, on, no, yes, true, false]"
        )
        assert _gen._parse_frontmatter(content)["keywords"] == [
            "off",
            "on",
            "no",
            "yes",
            "true",
            "false",
        ]

    def test_boolean_like_description_kept_as_string(self):
        assert _gen._parse_frontmatter(_fm("x", "description: yes"))["description"] == "yes"

    def test_frontmatter_loader_refuses_python_object_tags(self):
        # The custom loader is a SafeLoader subclass — arbitrary object
        # construction (code execution) must still be refused.
        import pytest
        import yaml

        payload = "!!python/object/apply:os.system ['echo pwned']"
        with pytest.raises(yaml.YAMLError):
            yaml.load(payload, Loader=_gen._FrontmatterLoader)  # noqa: S506

    def test_keywords_drops_null_and_empty_elements(self):
        # A null/empty flow element must not become a bogus "None" keyword.
        content = _fm("x", "description: y\nkeywords: [python, , web]")
        assert _gen._parse_frontmatter(content)["keywords"] == ["python", "web"]

    def test_numeric_like_scalars_kept_verbatim(self):
        # Leading-zero / hex / sexagesimal scalars must not be re-typed (YAML 1.1
        # would give "7"/"16"/"750"); the authored spelling must survive.
        content = _fm("x", "description: y\nkeywords: [007, 0x10]")
        assert _gen._parse_frontmatter(content)["keywords"] == ["007", "0x10"]

    def test_non_scalar_description_rejected(self):
        # A description authored as a LIST is not text — reject it (empty),
        # never str()/repr it into the catalog.
        content = "---\nname: x\ndescription:\n  - a\n  - b\n---\n# x\n"
        assert _gen._parse_frontmatter(content)["description"] == ""

    def test_loader_refuses_yaml_aliases(self):
        # Skill frontmatter never uses anchors/aliases; the loader refuses them,
        # closing the alias-multiplication DoS class at the source.
        import pytest
        import yaml

        with pytest.raises(yaml.YAMLError):
            yaml.load("a: &x 1\nb: *x\n", Loader=_gen._FrontmatterLoader)  # noqa: S506

    def test_alias_amplification_falls_back_without_blowup(self):
        # A crafted alias-amplification frontmatter (many *a pointing at a large
        # anchored scalar) must NOT materialize quadratically — aliases are
        # refused, so the parser falls back to the bounded legacy regex.
        import time

        big = "x" * 2000
        content = (
            f"---\nname: y\nanchor: &a {big}\n"
            "keywords: [*a, *a, *a, *a, *a, *a, *a, *a]\n---\n"
        )
        start = time.time()
        result = _gen._parse_frontmatter(content)
        assert time.time() - start < 1.0
        # Legacy keeps only the literal "*a" tokens — never 8x the big scalar.
        assert sum(len(k) for k in result["keywords"]) < len(big)

    def test_base_safeloader_bool_resolver_not_mutated(self):
        # Customizing _FrontmatterLoader must NOT mutate SafeLoader's shared
        # resolver map (a class-attribute footgun).
        import yaml

        assert yaml.safe_load("off") is False
