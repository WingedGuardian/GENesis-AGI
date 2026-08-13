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


# ---------------------------------------------------------------------------
# _parse_frontmatter — description extraction across YAML scalar forms.
#
# The legacy regex (`description:\s*["\']?([^"\'\n]+)`) truncated the value at
# the first apostrophe/quote/newline, dropping searchable text from the
# catalog. These tests pin the full-value behavior for every scalar form the
# real skill corpus uses. Name + keywords extraction is unchanged and its
# happy path is guarded too. All fixtures synthetic — no live-repo dependence.
# ---------------------------------------------------------------------------


def test_parse_frontmatter_plain_apostrophe_not_truncated():
    """A plain scalar with apostrophes survives in full (was cut at the ')."""
    content = (
        "---\n"
        "name: voice\n"
        "description: Apply when Genesis writes as itself. "
        "Not for the user's voice (that's other). Activate here.\n"
        "keywords: [voice]\n"
        "---\n# body\n"
    )
    r = _gen._parse_frontmatter(content, fallback_name="voice")
    assert r["name"] == "voice"
    assert r["description"] == (
        "Apply when Genesis writes as itself. Not for the user's voice "
        "(that's other). Activate here."
    )
    assert r["keywords"] == ["voice"]


def test_parse_frontmatter_double_quoted_unescapes_and_keeps_full():
    """A double-quoted scalar with escaped quotes is captured whole + unescaped."""
    content = (
        "---\n"
        "name: gx\n"
        'description: "Use when X. Examples: \\"Index this repo\\", \\"Reanalyze\\""\n'
        "keywords: [a]\n"
        "---\n"
    )
    r = _gen._parse_frontmatter(content, "gx")
    assert r["description"] == 'Use when X. Examples: "Index this repo", "Reanalyze"'


def test_parse_frontmatter_single_quoted_doubled_apostrophe():
    """A single-quoted scalar with a doubled '' escape survives (was cut at ')."""
    content = "---\nname: s\ndescription: 'It''s a thing'\nkeywords: [a]\n---\n"
    r = _gen._parse_frontmatter(content, "s")
    assert r["description"] == "It's a thing"


def test_parse_frontmatter_plain_multiline_folds_indented_continuations():
    """A plain scalar wrapped across indented lines folds into one line."""
    content = (
        "---\n"
        "name: ci\n"
        "description: Line one,\n"
        "  line two,\n"
        "  line three\n"
        "keywords: [a]\n"
        "---\n"
    )
    r = _gen._parse_frontmatter(content, "ci")
    assert r["description"] == "Line one, line two, line three"


def test_parse_frontmatter_plain_multiline_stops_at_next_key():
    """Plain-scalar folding must not swallow the following (non-indented) key."""
    content = (
        "---\n"
        "name: ci\n"
        "description: Only this line.\n"
        "keywords: [alpha, beta]\n"
        "---\n"
    )
    r = _gen._parse_frontmatter(content, "ci")
    assert r["description"] == "Only this line."
    assert r["keywords"] == ["alpha", "beta"]  # not eaten by the description


def test_parse_frontmatter_folded_block_scalar_joined():
    """`>` folded block: indented continuation lines are collected (guard)."""
    content = (
        "---\nname: f\ndescription: >\n  Folded one\n  folded two\n"
        "keywords: [a]\n---\n"
    )
    r = _gen._parse_frontmatter(content, "f")
    assert r["description"] == "Folded one folded two"


def test_parse_frontmatter_literal_block_scalar_joined():
    """`|` literal block: collected the same way as folded (guard)."""
    content = (
        "---\nname: l\ndescription: |\n  Literal one\n  literal two\n"
        "keywords: [a]\n---\n"
    )
    r = _gen._parse_frontmatter(content, "l")
    assert r["description"] == "Literal one literal two"


def test_parse_frontmatter_chomped_folded_indicator():
    """`>-` chomping indicator is treated as a block scalar, not literal text."""
    content = (
        "---\nname: c\ndescription: >-\n  Chomped one\n  chomped two\n"
        "keywords: [a]\n---\n"
    )
    r = _gen._parse_frontmatter(content, "c")
    assert r["description"] == "Chomped one chomped two"


def test_parse_frontmatter_folded_block_multi_paragraph():
    """A blank line inside a block scalar is a paragraph break, not the end —
    content after it must not be dropped (matches yaml.safe_load)."""
    content = (
        "---\nname: mp\ndescription: >\n"
        "  Paragraph one line.\n"
        "\n"
        "  Paragraph two line.\n"
        "keywords: [a]\n---\n"
    )
    r = _gen._parse_frontmatter(content, "mp")
    assert r["description"] == "Paragraph one line. Paragraph two line."


def test_parse_frontmatter_block_scalar_single_space_indent():
    """Block content only needs to be MORE indented than the col-0 key; a
    single leading space must not yield an empty description."""
    content = (
        "---\nname: sp\ndescription: |\n"
        " One line.\n"
        " Two line.\n"
        "keywords: [a]\n---\n"
    )
    r = _gen._parse_frontmatter(content, "sp")
    assert r["description"] == "One line. Two line."


def test_parse_frontmatter_block_scalar_stops_at_col0_key():
    """A block scalar still terminates at the next column-0 key (no absorb)."""
    content = (
        "---\nname: b\ndescription: >\n"
        "  Folded body.\n"
        "keywords: [x, y]\n---\n"
    )
    r = _gen._parse_frontmatter(content, "b")
    assert r["description"] == "Folded body."
    assert r["keywords"] == ["x", "y"]


def test_parse_frontmatter_indented_mapping_stops_at_sibling_key():
    """When the whole frontmatter mapping is indented, a plain description must
    stop at the SIBLING key (same indent as the description key), not fold it.
    Matches yaml.safe_load -> 'useful'."""
    content = "---\n  name: im\n  description: useful\n  keywords: [x]\n---\n"
    r = _gen._parse_frontmatter(content, "im")
    assert r["description"] == "useful"
    assert r["keywords"] == ["x"]


def test_parse_frontmatter_plain_scalar_skips_comment_line():
    """An indented comment line ends a plain scalar (yaml ignores it) -> 'foo'."""
    content = "---\nname: c\ndescription: foo\n  # a trailing comment\nkeywords: [a]\n---\n"
    r = _gen._parse_frontmatter(content, "c")
    assert r["description"] == "foo"


def test_parse_frontmatter_block_scalar_keeps_hash_as_content():
    """Inside a block scalar, '#' is literal content, not a comment
    (yaml.safe_load folds it in)."""
    content = (
        "---\nname: h\ndescription: >\n  line one\n  # still content here\n"
        "keywords: [a]\n---\n"
    )
    r = _gen._parse_frontmatter(content, "h")
    assert r["description"] == "line one # still content here"


def test_parse_frontmatter_deeper_indent_child_then_col0_key():
    """A more-indented continuation folds; a col-0 sibling key stops it."""
    content = "---\nname: d\ndescription: foo\n    deeper\nkeywords: [a]\n---\n"
    r = _gen._parse_frontmatter(content, "d")
    assert r["description"] == "foo deeper"
    assert r["keywords"] == ["a"]


def test_parse_frontmatter_missing_description_is_empty():
    content = "---\nname: n\nkeywords: [a]\n---\n"
    r = _gen._parse_frontmatter(content, "n")
    assert r["name"] == "n"
    assert r["description"] == ""


def test_parse_frontmatter_no_frontmatter_uses_fallback_name():
    r = _gen._parse_frontmatter("# just a heading\nsome text\n", "fallback")
    assert r["name"] == "fallback"
    assert r["description"] == ""
    assert r["keywords"] == []


def test_parse_frontmatter_keywords_flow_list_unchanged():
    content = "---\nname: k\ndescription: d\nkeywords: [alpha, beta, gamma]\n---\n"
    r = _gen._parse_frontmatter(content, "k")
    assert r["keywords"] == ["alpha", "beta", "gamma"]


def test_parse_frontmatter_plain_scalar_folds_across_blank_line():
    """A plain scalar spans a blank line (yaml folds the paragraphs) -> both."""
    content = "---\nname: a\ndescription: first\n\n  second\nkeywords: [x]\n---\n"
    r = _gen._parse_frontmatter(content, "a")
    assert r["description"] == "first second"


def test_parse_frontmatter_inline_comment_stripped():
    """An inline ' # comment' suffix on a plain scalar is removed (yaml)."""
    content = "---\nname: a\ndescription: real text # author note\nkeywords: [x]\n---\n"
    r = _gen._parse_frontmatter(content, "a")
    assert r["description"] == "real text"


def test_parse_frontmatter_double_quoted_unicode_escape_decoded():
    r'''A double-quoted \u / \x escape decodes to the real character (yaml).'''
    content = '---\nname: a\ndescription: "em\\u2014dash \\x20 gap"\nkeywords: [x]\n---\n'
    r = _gen._parse_frontmatter(content, "a")
    # — -> em dash, \x20 -> space; whitespace collapsed to single spaces.
    assert r["description"] == "em—dash gap"
    assert "—" in r["description"] and "u2014" not in r["description"]


def test_parse_frontmatter_alias_bomb_is_refused_and_bounded():
    """A YAML alias-multiplication payload is refused at the composer and the
    parse falls back to the linear legacy regex — no object-graph expansion.
    (Fails without _FrontmatterLoader's alias refusal.)"""
    payload = ",".join(['"x"'] * 50)
    refs = ",".join(["*a"] * 50)
    content = f"---\nname: b\ndescription: &a [{payload}]\nkeywords: [{refs}]\n---\n"
    r = _gen._parse_frontmatter(content, "b")
    assert isinstance(r, dict)
    # No alias expansion: keywords are the literal unexpanded tokens, not a
    # multiplied structure, and the description stayed a short scalar.
    assert len(r["keywords"]) <= 60
    assert len(r["description"]) < 200


def test_parse_frontmatter_merge_key_alias_is_refused():
    """A YAML merge-key alias (`<<: *a`) is refused at the composer and routed to
    the bounded legacy path — not expanded. (Pins compose_node coverage.)"""
    content = (
        "---\nname: m\ndescription: base &a real text\n"
        "extra:\n  <<: *a\nkeywords: [x]\n---\n"
    )
    r = _gen._parse_frontmatter(content, "m")
    assert isinstance(r, dict)
    assert len(r["description"]) < 200  # no alias-driven growth


def test_parse_frontmatter_alias_as_mapping_key_is_refused():
    """An alias used as a mapping key (`*a : v`) is refused → bounded legacy."""
    content = "---\nname: k\ndescription: &a hello\n*a : world\nkeywords: [x]\n---\n"
    r = _gen._parse_frontmatter(content, "k")
    assert isinstance(r, dict)
    assert len(r["description"]) < 200


def test_parse_frontmatter_oversized_block_falls_back_bounded():
    """A frontmatter block over the size cap skips YAML for the linear legacy
    regex (no object-graph amplification). (Fails without the size cap.)"""
    huge = "y" * (_gen._MAX_FRONTMATTER_CHARS + 5000)
    content = f"---\nname: o\ndescription: {huge}\nkeywords: [a]\n---\n"
    r = _gen._parse_frontmatter(content, "o")
    assert r["name"] == "o"
    assert len(r["description"]) <= len(content)


def test_parse_frontmatter_pathological_input_is_bounded():
    """Unterminated double-quote + many escapes must complete (no ReDoS/hang)
    and never amplify beyond the input (no object graph)."""
    big = '"' + ("\\a" * 200_000)  # unterminated quote, no closing "
    content = f"---\nname: p\ndescription: {big}\nkeywords: [a]\n---\n"
    r = _gen._parse_frontmatter(content, "p")
    assert isinstance(r["description"], str)
    assert len(r["description"]) <= len(content)
