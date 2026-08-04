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
    skills = [{"name": "taste", "description": "design dials", "tier": 1}]
    tools = [{"name": "browser_click", "description": "Click.", "server": "genesis-health"}]

    block = _exp.render_block(skills, tools)

    assert block.startswith(_exp.START_MARKER)
    assert block.rstrip().endswith(_exp.END_MARKER)
    assert "taste" in block and "browser_click" in block


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

    import pytest

    with pytest.raises(ValueError):
        _exp.update_agents_md(corrupted, block)


def test_update_raises_on_dangling_end_marker(tmp_path):
    corrupted = f"# Prose\n\nleftover\n{_exp.END_MARKER}\n"
    block = f"{_exp.START_MARKER}\nnew\n{_exp.END_MARKER}\n"

    import pytest

    with pytest.raises(ValueError):
        _exp.update_agents_md(corrupted, block)


def test_update_raises_on_reversed_markers(tmp_path):
    corrupted = f"# Prose\n\n{_exp.END_MARKER}\nx\n{_exp.START_MARKER}\n"
    block = f"{_exp.START_MARKER}\nnew\n{_exp.END_MARKER}\n"

    import pytest

    with pytest.raises(ValueError):
        _exp.update_agents_md(corrupted, block)
