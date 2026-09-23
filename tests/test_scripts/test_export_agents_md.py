"""Tests for scripts/export_agents_md.py — the AGENTS.md skills+MCP exporter.

Invariants under test:
  * MCP tools are discovered by STATIC-PARSE of the ``@mcp.tool()`` decorator
    (no import of the server modules → no import side-effects);
  * BODY-scope only: the whole ``genesis-memory`` server and cognitive
    ``genesis-health`` tools (ego/deliberate/evo/experiment/cognitive-mod/
    reflex/…) are EXCLUDED — never the brain;
  * skills come from the repo-tracked tiers only (``.claude/skills`` +
    ``src/genesis/skills``) — the install-specific ``~/.genesis/skill-library``
    is NOT scanned (generalizability + privacy);
  * the generated block lives between ``<!-- genesis:skills:start/end -->``
    markers and update is IDEMPOTENT and PRESERVES all surrounding content
    (hand-curated prose AND the GitNexus block are never touched).

All fixtures are synthetic tmp_path trees — no dependence on the live repo.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Load the stdlib script as a module (not a package — use importlib).
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "export_agents_md.py"
_spec = importlib.util.spec_from_file_location("export_agents_md", _SCRIPT_PATH)
_exp = importlib.util.module_from_spec(_spec)
sys.modules["export_agents_md"] = _exp
_spec.loader.exec_module(_exp)


# --------------------------------------------------------------------------- #
# fixture builders
# --------------------------------------------------------------------------- #
def _mk_mcp_file(path: Path, server: str, tools: list[tuple[str, str]]) -> None:
    """Write a fake MCP module: a FastMCP server + @mcp.tool() functions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "from fastmcp import FastMCP",
        f'mcp = FastMCP("{server}")',
        "",
    ]
    for name, doc in tools:
        lines += [
            "@mcp.tool()",
            f"async def {name}(x: str) -> dict:",
            f'    """{doc}"""',
            "    return {}",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _mk_skill(skill_dir: Path, name: str, description: str = "does things") -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n"
    )


def _names(rows: list[dict]) -> set[str]:
    return {r["name"] for r in rows}


# --------------------------------------------------------------------------- #
# parse_mcp_tools — static parse + body scope
# --------------------------------------------------------------------------- #
def test_parse_mcp_tools_extracts_name_and_description(tmp_path):
    _mk_mcp_file(
        tmp_path / "recon_mcp.py",
        "genesis-recon",
        [
            ("recon_config", "View or modify recon configuration."),
            ("recon_findings", "List recon findings."),
        ],
    )

    tools = _exp.parse_mcp_tools(tmp_path)

    by_name = {t["name"]: t for t in tools}
    assert set(by_name) == {"recon_config", "recon_findings"}
    assert by_name["recon_config"]["description"] == "View or modify recon configuration."
    assert by_name["recon_config"]["server"] == "genesis-recon"


def test_parse_mcp_tools_excludes_memory_server(tmp_path):
    """The whole genesis-memory server is brain — never exported."""
    _mk_mcp_file(
        tmp_path / "memory" / "core.py",
        "genesis-memory",
        [("memory_store", "Store a memory."), ("memory_recall", "Recall memories.")],
    )
    _mk_mcp_file(
        tmp_path / "recon_mcp.py",
        "genesis-recon",
        [("recon_config", "Recon config.")],
    )

    tools = _exp.parse_mcp_tools(tmp_path)

    assert _names(tools) == {"recon_config"}
    assert "memory_store" not in _names(tools)


def test_parse_mcp_tools_excludes_cognitive_health_tools(tmp_path):
    """Cognitive health tools (ego/deliberate/evo/…) are brain — excluded;
    body health tools (browser/web/…) are kept."""
    _mk_mcp_file(
        tmp_path / "health" / "ego_tools.py",
        "genesis-health",
        [("ego_decision", "Ego decision."), ("ego_goal_create", "Create a goal.")],
    )
    _mk_mcp_file(
        tmp_path / "health" / "deliberation_tools.py",
        "genesis-health",
        [("deliberate", "Deliberate on a question.")],
    )
    _mk_mcp_file(
        tmp_path / "health" / "browser.py",
        "genesis-health",
        [("browser_click", "Click an element.")],
    )

    tools = _exp.parse_mcp_tools(tmp_path)

    assert _names(tools) == {"browser_click"}


def test_parse_mcp_tools_ignores_undecorated_functions(tmp_path):
    """Only @mcp.tool()-decorated functions are tools; helpers are ignored."""
    path = tmp_path / "recon_mcp.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "from fastmcp import FastMCP\n"
        'mcp = FastMCP("genesis-recon")\n\n'
        "def _helper(x):\n    return x\n\n"
        "@mcp.tool()\n"
        "async def recon_config(x: str) -> dict:\n"
        '    """Recon config."""\n    return {}\n',
        encoding="utf-8",
    )

    tools = _exp.parse_mcp_tools(tmp_path)

    assert _names(tools) == {"recon_config"}


# --------------------------------------------------------------------------- #
# collect_skills — repo tiers only
# --------------------------------------------------------------------------- #
def test_collect_skills_repo_tracked_tiers_only(tmp_path):
    repo = tmp_path / "repo"
    _mk_skill(repo / ".claude" / "skills" / "genesis-development", "genesis-development")
    _mk_skill(repo / "src" / "genesis" / "skills" / "browser-automation", "browser-automation")

    skills = _exp.collect_skills(repo)

    assert _names(skills) == {"genesis-development", "browser-automation"}


def test_collect_skills_tier1_wins_on_duplicate(tmp_path):
    repo = tmp_path / "repo"
    _mk_skill(repo / ".claude" / "skills" / "dup", "dup", "tier1 version")
    _mk_skill(repo / "src" / "genesis" / "skills" / "dup", "dup", "tier2 version")

    skills = _exp.collect_skills(repo)

    rows = [s for s in skills if s["name"] == "dup"]
    assert len(rows) == 1
    assert rows[0]["description"] == "tier1 version"


# --------------------------------------------------------------------------- #
# render_block + update_agents_md — markers, idempotency, preservation
# --------------------------------------------------------------------------- #
def test_render_block_wrapped_in_markers_with_content(tmp_path):
    skills = [
        {
            "name": "taste",
            "description": "design dials",
            "tier": 1,
            "path": ".claude/skills/taste",
            "marker": "SKILL.md",
        }
    ]
    tools = [{"name": "browser_click", "description": "Click.", "server": "genesis-health"}]

    block = _exp.render_block(skills, tools)

    assert block.startswith(_exp.START_MARKER)
    assert block.rstrip().endswith(_exp.END_MARKER)
    assert "taste" in block and "browser_click" in block


def test_render_block_emits_the_real_path_not_a_name_guess(tmp_path):
    """A nested skill's path must survive into the bullet verbatim.

    `gitnexus-cli` lives at `.claude/skills/gitnexus/gitnexus-cli/`, NOT at
    `.claude/skills/gitnexus-cli/`. An external client that derived the path
    from the name would miss 6 of this repo's Tier 1 skills, which is the
    defect this rendering exists to remove.
    """
    skills = [
        {
            "name": "gitnexus-cli",
            "description": "CLI wrapper",
            "tier": 1,
            "path": ".claude/skills/gitnexus/gitnexus-cli",
            "marker": "SKILL.md",
        }
    ]

    block = _exp.render_block(skills, [])
    bullets = [ln for ln in block.splitlines() if ln.startswith("- **")]

    assert "`.claude/skills/gitnexus/gitnexus-cli/SKILL.md`" in "\n".join(bullets)
    # Scoped to the BULLETS on purpose: asserting only that the real path is
    # present would pass even if a name-derived one were emitted alongside it.
    # The header prose quotes the wrong path deliberately, as the counter-
    # example, so a whole-block assertion here fails against correct output.
    assert not any(".claude/skills/gitnexus-cli/" in ln for ln in bullets)


def test_render_block_emits_tier2_path_under_src(tmp_path):
    """Tier 2 skills live under src/genesis/skills and must say so.

    The bullet list is flat and unannotated, so without the path a reader
    cannot tell a Tier 2 entry from a Tier 1 one.
    """
    skills = [
        {
            "name": "browser-automation",
            "description": "drive a browser",
            "tier": 2,
            "path": "src/genesis/skills/browser-automation",
            "marker": "SKILL.md",
        }
    ]

    block = _exp.render_block(skills, [])

    assert "`src/genesis/skills/browser-automation/SKILL.md`" in block


def test_every_emitted_skill_path_resolves_in_this_repo():
    """The bullets must be TRUE of this repo, not merely well-formed.

    Every other test here feeds render_block a fixture dict and checks the
    dict comes back out — which proves the renderer is faithful and proves
    nothing about whether the value is real. This one walks the actual tiers
    and opens every file the block would publish, so a marker that is not
    SKILL.md, a container laid out differently, or a scanner change all fail
    HERE rather than shipping a path that 404s for an external client.
    """
    repo_root = Path(__file__).resolve().parents[2]
    skills = _exp.collect_skills(repo_root)

    assert skills, "no skills collected — the scan itself is broken"
    missing = [
        f"{s['name']} -> {s['path']}/{s.get('marker')}"
        for s in skills
        if not (repo_root / s["path"] / (s.get("marker") or "SKILL.md")).is_file()
    ]
    assert not missing, f"emitted skill paths that do not resolve: {missing}"


def test_nested_container_skill_is_reachable_by_its_emitted_path():
    """Pins the header prose's own worked example against the filesystem.

    The header tells readers `gitnexus-cli` lives under a container. If that
    layout ever changes, the prose becomes a lie that nothing else detects —
    the class-level test above would still pass, because it checks whatever
    the scanner currently reports rather than this specific claim.
    """
    repo_root = Path(__file__).resolve().parents[2]
    skills = {s["name"]: s for s in _exp.collect_skills(repo_root)}

    cli = skills.get("gitnexus-cli")
    assert cli is not None, "gitnexus-cli missing — the header example is stale"
    assert cli["path"] == ".claude/skills/gitnexus/gitnexus-cli"
    assert (repo_root / cli["path"] / cli["marker"]).is_file()


def test_render_block_refuses_a_non_repo_relative_path(tmp_path):
    """An absolute path would publish a home directory into a public file.

    Reachable, not hypothetical: _scan_tier falls back to str(entry) for any
    skill outside repo_root, which is how the ~/.genesis/skill-library tier
    resolves. Nothing but an omitted call keeps that tier out of this render.
    """
    with pytest.raises(ValueError, match="non-repo-relative"):
        _exp.render_block(
            [
                {
                    "name": "leaky",
                    "description": "d",
                    "tier": 2,
                    # Absolute but deliberately NOT home-shaped. A realistic
                    # /home/<user>/... fixture matches the leak detector's
                    # generic pattern class and trips the pre-push privacy
                    # scan on every push, training readers to wave it through.
                    # What this test needs is only that the path is absolute.
                    "path": "/absolute/outside-any-repo/leaky",
                    "marker": "SKILL.md",
                }
            ],
            [],
        )


def test_render_block_emits_a_non_default_marker(tmp_path):
    """A skill defined by README.md must not be advertised as SKILL.md.

    The scanner accepts three markers; hardcoding one at render time would
    re-introduce a name-based guess and emit a path that does not resolve.
    """
    block = _exp.render_block(
        [
            {
                "name": "readme-defined",
                "description": "d",
                "tier": 2,
                "path": "src/genesis/skills/readme-defined",
                "marker": "README.md",
            }
        ],
        [],
    )

    assert "`src/genesis/skills/readme-defined/README.md`" in block
    assert "readme-defined/SKILL.md" not in block


def test_marker_less_directory_is_refused_not_guessed(tmp_path):
    """A directory with no instruction file must stop the export.

    REPRODUCED before the fix: `_scan_tier` emits a name-only entry for a
    top-level directory carrying no marker, `_resolve_marker` substituted
    "SKILL.md", and the bullet shipped `<dir>/SKILL.md` pointing at nothing —
    round 1's defect relocated one layer down. Drives the REAL scan → collect →
    render chain, because the bug lived in how those three compose.
    """
    (tmp_path / ".claude" / "skills" / "orphan-dir").mkdir(parents=True)

    skills = _exp.collect_skills(tmp_path)

    assert skills, "fixture did not produce an entry — the scan changed shape"
    assert skills[0]["marker"] == "", "a missing marker must resolve EMPTY, not a guess"
    with pytest.raises(ValueError, match="no instruction file"):
        _exp.render_block(skills, [])


def test_marker_set_is_the_scanner_s_own(tmp_path):
    """Import, never restate.

    A second hand-written copy is how a future fourth spelling gets handled in
    the scanner and silently missed here — which would publish a path no
    external client can open.
    """
    import generate_skill_catalog as _gsc_direct

    assert _exp._gsc.SKILL_MARKERS is _gsc_direct.SKILL_MARKERS


def test_render_block_refuses_a_skill_with_no_path(tmp_path):
    """Fail loud, never emit a pathless bullet.

    A bullet without a path silently returns the reader to guessing from the
    name — the exact inference this block removes — so a missing path is a
    generator bug to surface, not a field to degrade around.
    """
    with pytest.raises(ValueError, match="has no path"):
        _exp.render_block([{"name": "orphan", "description": "d", "tier": 1}], [])


def test_update_appends_block_when_markers_absent(tmp_path):
    original = "# Agent Instructions\n\nHand-curated prose.\n"
    block = f"{_exp.START_MARKER}\ngenerated\n{_exp.END_MARKER}\n"

    updated = _exp.update_agents_md(original, block)

    assert updated.startswith(original.rstrip())
    assert _exp.START_MARKER in updated and _exp.END_MARKER in updated


def test_update_replaces_existing_block_idempotently(tmp_path):
    block1 = f"{_exp.START_MARKER}\nold content\n{_exp.END_MARKER}\n"
    original = f"# Prose\n\n{block1}\n"
    block2 = f"{_exp.START_MARKER}\nnew content\n{_exp.END_MARKER}\n"

    once = _exp.update_agents_md(original, block2)
    twice = _exp.update_agents_md(once, block2)

    assert "new content" in once
    assert "old content" not in once
    assert once == twice  # idempotent
    assert "# Prose" in once  # prose preserved


def test_update_preserves_gitnexus_block_and_prose(tmp_path):
    gitnexus = "<!-- gitnexus:start -->\nauto stats\n<!-- gitnexus:end -->\n"
    original = f"# Agent Instructions\n\nProse here.\n\n{gitnexus}"
    block = f"{_exp.START_MARKER}\ninventory\n{_exp.END_MARKER}\n"

    updated = _exp.update_agents_md(original, block)

    assert "Prose here." in updated
    assert "<!-- gitnexus:start -->" in updated
    assert "auto stats" in updated
    assert "inventory" in updated


def test_render_block_blank_line_before_each_server_header(tmp_path):
    """Each **server** header must be preceded by a blank line, else markdown
    folds it into the previous server's bullet list."""
    tools = [
        {"name": "browser_click", "description": "Click.", "server": "genesis-health"},
        {"name": "recon_config", "description": "Cfg.", "server": "genesis-recon"},
    ]
    block = _exp.render_block([], tools)

    lines = block.split("\n")
    headers = [i for i, ln in enumerate(lines) if ln.startswith("**genesis-")]
    assert len(headers) == 2
    for i in headers:
        assert lines[i - 1] == "", f"header {lines[i]!r} not preceded by blank line"


def test_update_raises_on_dangling_start_marker(tmp_path):
    """Unpaired START (no END) must NOT silently append — refuse to guess."""
    corrupted = f"# Prose\n\n{_exp.START_MARKER}\nhalf-written\n"
    block = f"{_exp.START_MARKER}\nnew\n{_exp.END_MARKER}\n"

    with pytest.raises(ValueError):
        _exp.update_agents_md(corrupted, block)


def test_update_raises_on_dangling_end_marker(tmp_path):
    corrupted = f"# Prose\n\nleftover\n{_exp.END_MARKER}\n"
    block = f"{_exp.START_MARKER}\nnew\n{_exp.END_MARKER}\n"

    with pytest.raises(ValueError):
        _exp.update_agents_md(corrupted, block)


def test_update_raises_on_reversed_markers(tmp_path):
    corrupted = f"# Prose\n\n{_exp.END_MARKER}\nx\n{_exp.START_MARKER}\n"
    block = f"{_exp.START_MARKER}\nnew\n{_exp.END_MARKER}\n"

    with pytest.raises(ValueError):
        _exp.update_agents_md(corrupted, block)


# --------------------------------------------------------------------------- #
# description completeness (Codex P2 regressions)
# --------------------------------------------------------------------------- #
def test_parse_mcp_tools_joins_wrapped_first_paragraph(tmp_path):
    """A summary that wraps across physical lines must not be truncated to the
    first line — the whole first paragraph is kept, whitespace-normalized."""
    path = tmp_path / "recon_mcp.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "from fastmcp import FastMCP\n"
        'mcp = FastMCP("genesis-recon")\n\n'
        "@mcp.tool()\n"
        "async def recon_config(x: str) -> dict:\n"
        '    """View or modify the recon configuration\n'
        "    across watchlist, schedule, and sources.\n\n"
        "    Longer detail is dropped.\n"
        '    """\n'
        "    return {}\n",
        encoding="utf-8",
    )

    tools = _exp.parse_mcp_tools(tmp_path)

    assert tools[0]["description"] == (
        "View or modify the recon configuration across watchlist, schedule, and sources."
    )
    assert "Longer detail" not in tools[0]["description"]


def test_collect_skills_recovers_description_past_apostrophe(tmp_path):
    """The line-based catalog regex truncates a description at the first
    apostrophe; collect_skills re-parses with YAML to keep it whole."""
    repo = tmp_path / "repo"
    skill = repo / ".claude" / "skills" / "taste"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: taste\n"
        "description: Use for Genesis's own dashboard and other UIs.\n"
        "---\n# taste\n",
        encoding="utf-8",
    )

    skills = _exp.collect_skills(repo)

    row = next(s for s in skills if s["name"] == "taste")
    assert row["description"] == "Use for Genesis's own dashboard and other UIs."
