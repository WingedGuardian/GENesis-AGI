#!/usr/bin/env python3
"""Export a portable skills + MCP-tools inventory into AGENTS.md.

One-way, on-demand generator (vendor-lock insurance — Cap #9). Reads the
repo-tracked skill tiers (``.claude/skills`` + ``src/genesis/skills``) and
STATIC-PARSES the ``@mcp.tool()`` decorators across ``src/genesis/mcp/``
(no server import → no import side-effects), then writes a BODY-SCOPE
inventory into a managed block in ``AGENTS.md``, between
``<!-- genesis:skills:start/end -->`` markers.

Body scope (never the brain): the whole ``genesis-memory`` server and the
cognitive ``genesis-health`` tools (ego / deliberate / evo / experiment /
cognitive-modification / reflex / calibration / immunity / loop-closure /
j9-eval / skill-replay / bench) are EXCLUDED. The install-specific
``~/.genesis/skill-library`` tier is intentionally NOT scanned
(generalizability + privacy).

Idempotent: only the content between the markers is rewritten — hand-curated
prose AND the GitNexus block are preserved. Export-only, no round-trip import.

NOTE: the output must be COMMITTED. ``update.sh`` treats AGENTS.md as
tracked-ephemeral and restores it to HEAD on deploy, so an uncommitted
regeneration would be discarded.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_ROOT = REPO_ROOT / "src" / "genesis" / "mcp"
AGENTS_MD = REPO_ROOT / "AGENTS.md"

START_MARKER = "<!-- genesis:skills:start -->"
END_MARKER = "<!-- genesis:skills:end -->"

# Reuse the sibling skill-catalog scanner (scripts/ is not a package).
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import generate_skill_catalog as _gsc  # noqa: E402

try:  # robust frontmatter parsing; degrade to the scanner's value if absent
    import yaml as _yaml
except ImportError:  # pragma: no cover - yaml ships with the venv
    _yaml = None

# --------------------------------------------------------------------------- #
# body scope — never export the brain
# --------------------------------------------------------------------------- #
# Whole servers that ARE the brain: excluded wholesale.
_BRAIN_SERVERS = {"genesis-memory"}
# Cognitive genesis-health tool families: excluded by name prefix. A denylist
# by prefix is fail-safe here — a NEW tool in one of these families is
# excluded by default, so the brain surface can't leak silently.
_BRAIN_TOOL_PREFIXES = (
    "ego_",
    "cognitive_",
    "experiment_",
    "evo_",
    "reflex_",
    "immunity_",
    "loop_closure_",
    "j9_eval_",
    "skill_replay_",
    "calibration_",
    "deliberat",  # deliberate, deliberation_*
    "bench_",
)


def _server_for(rel_path: Path) -> str:
    """Map an mcp/ source path to its FastMCP server name."""
    parts = rel_path.parts
    if parts and parts[0] == "memory":
        return "genesis-memory"
    if parts and parts[0] == "health":
        return "genesis-health"
    return {
        "recon_mcp": "genesis-recon",
        "outreach_mcp": "genesis-outreach",
        "discord_bot_mcp": "discord-bot",
        "health_mcp": "genesis-health",
        "memory_mcp": "genesis-memory",
    }.get(rel_path.stem, rel_path.stem)


def _is_brain_tool(name: str, server: str) -> bool:
    if server in _BRAIN_SERVERS:
        return True
    return any(name.startswith(p) for p in _BRAIN_TOOL_PREFIXES)


def _is_mcp_tool_decorator(dec: ast.expr) -> bool:
    """True for ``@mcp.tool()`` (or bare ``@mcp.tool``)."""
    target = dec.func if isinstance(dec, ast.Call) else dec
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "tool"
        and isinstance(target.value, ast.Name)
        and target.value.id == "mcp"
    )


def parse_mcp_tools(mcp_root: Path) -> list[dict]:
    """Static-parse ``@mcp.tool()`` functions under ``mcp_root``.

    Returns body-scope tools only, sorted by (server, name). Each entry:
    ``{"name", "description", "server"}`` where description is the first
    docstring line. Never imports the server modules.
    """
    tools: list[dict] = []
    for py in sorted(mcp_root.rglob("*.py")):
        rel = py.relative_to(mcp_root)
        server = _server_for(rel)
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(_is_mcp_tool_decorator(d) for d in node.decorator_list):
                continue
            if _is_brain_tool(node.name, server):
                continue
            doc = (ast.get_docstring(node) or "").strip()
            # First paragraph (to the first blank line), whitespace-normalized —
            # docstrings whose summary wraps across physical lines must not be
            # truncated mid-sentence by a naive first-line split.
            para = doc.split("\n\n", 1)[0]
            desc = " ".join(para.split())
            tools.append({"name": node.name, "description": desc, "server": server})
    tools.sort(key=lambda t: (t["server"], t["name"]))
    return tools


def _frontmatter_description(skill_dir: Path) -> str | None:
    """Full, whitespace-normalized description from a skill's frontmatter.

    The shared catalog scanner now returns full descriptions via its own
    string parser, but a YAML load here stays authoritative for AGENTS.md: it
    resolves any scalar form (exotic escapes, multi-line quoted) exactly, so a
    shipped artifact never depends on the scanner's documented edge-case limits.
    Runs at build time over repo-tracked tiers only. Returns None (so the caller
    keeps the scanner's value) if yaml is unavailable or the block doesn't parse
    to a usable string.
    """
    if _yaml is None:
        return None
    for md in ("SKILL.md", "skill.md", "README.md"):
        p = skill_dir / md
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if not text.startswith("---"):
            return None
        end = text.find("\n---", 3)
        if end == -1:
            return None
        try:
            data = _yaml.safe_load(text[3:end]) or {}
        except _yaml.YAMLError:
            return None
        desc = data.get("description") if isinstance(data, dict) else None
        if isinstance(desc, str) and desc.strip():
            return " ".join(desc.split())
        return None
    return None


def collect_skills(repo_root: Path) -> list[dict]:
    """Skills from the repo-tracked tiers only.

    Tier 1 ``.claude/skills`` + Tier 2 ``src/genesis/skills`` — the
    install-specific ``~/.genesis/skill-library`` is intentionally NOT
    scanned (a shipped AGENTS.md must stay generalizable and must not leak
    a user's private skill names). Tier-1 wins on a name collision.
    Descriptions are re-parsed with YAML for authoritative full-fidelity values.
    """
    tier1 = _gsc._scan_tier(repo_root / ".claude" / "skills", 1, repo_root)
    seen = {s["name"].lower() for s in tier1}
    skills = list(tier1)
    for s in _gsc._scan_tier(repo_root / "src" / "genesis" / "skills", 2, repo_root):
        if s["name"].lower() not in seen:
            skills.append(s)
            seen.add(s["name"].lower())
    for s in skills:
        full = _frontmatter_description(repo_root / s["path"])
        if full:
            s["description"] = full
    return skills


def render_block(skills: list[dict], tools: list[dict]) -> str:
    """Render the managed AGENTS.md block (inclusive of both markers)."""
    lines = [
        START_MARKER,
        "",
        "<!-- Auto-generated by scripts/export_agents_md.py — do not edit by "
        "hand; run the script to refresh. -->",
        "",
        "## Genesis Capability Surface",
        "",
        "Body-scope inventory for cross-tool agents — Genesis's skills and "
        "action tools. Memory and cognition (the brain) are intentionally "
        "excluded.",
        "",
        "### Skills",
        "",
    ]
    if skills:
        for s in sorted(skills, key=lambda x: x["name"]):
            desc = (s.get("description") or "").strip()
            lines.append(f"- **{s['name']}** — {desc}" if desc else f"- **{s['name']}**")
    else:
        lines.append("_none_")
    lines += ["", "### MCP Tools", ""]
    if tools:
        current = None
        for t in tools:  # already sorted by (server, name)
            if t["server"] != current:
                if current is not None:
                    lines.append("")  # blank line separates server sections
                current = t["server"]
                lines += [f"**{current}**", ""]
            desc = (t.get("description") or "").strip()
            lines.append(f"- `{t['name']}` — {desc}" if desc else f"- `{t['name']}`")
        lines.append("")
    else:
        lines += ["_none_", ""]
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def update_agents_md(text: str, block: str) -> str:
    """Insert/replace the managed block in ``text``, preserving all else.

    Idempotent: rewrites only the region between the markers. If the markers
    are absent, appends the block. Never touches surrounding content (the
    hand-curated prose or the GitNexus marker block).
    """
    block = block.rstrip("\n")
    start = text.find(START_MARKER)
    end = text.find(END_MARKER)
    has_start, has_end = start != -1, end != -1
    # Malformed marker state (interrupted write, bad merge): refuse to guess —
    # guessing risks deleting hand-curated prose or the GitNexus block.
    if has_start != has_end:
        raise ValueError(
            "AGENTS.md has an unpaired genesis:skills marker "
            f"({'START' if has_start else 'END'} present, its pair missing); "
            "refusing to edit — fix the markers by hand and re-run."
        )
    if has_start and has_end:
        if end <= start:
            raise ValueError(
                "AGENTS.md genesis:skills markers are out of order "
                "(END before START); refusing to edit — fix by hand and re-run."
            )
        end_full = end + len(END_MARKER)
        new = text[:start] + block + text[end_full:]
        return new if new.endswith("\n") else new + "\n"
    base = text.rstrip("\n")
    return f"{base}\n\n{block}\n"


def main() -> None:
    skills = collect_skills(REPO_ROOT)
    tools = parse_mcp_tools(MCP_ROOT)
    block = render_block(skills, tools)
    text = AGENTS_MD.read_text(encoding="utf-8") if AGENTS_MD.exists() else "# Agent Instructions\n"
    AGENTS_MD.write_text(update_agents_md(text, block), encoding="utf-8")
    print(f"AGENTS.md updated: {len(skills)} skills, {len(tools)} body tools ({AGENTS_MD})")


if __name__ == "__main__":
    main()
