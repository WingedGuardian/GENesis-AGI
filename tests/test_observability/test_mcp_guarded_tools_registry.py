"""Completeness guard for GUARDED_MCP_TOOLS.

The stale-code guard only blocks tools that overwrite an existing row via
SIMILARITY matching (stale-able). This test scans every ``@mcp.tool()`` function
body for a similarity-refine marker and asserts the set of such tools EQUALS
GUARDED_MCP_TOOLS — so a NEW fuzzy-matched refine writer cannot ship without
being consciously classified (and the guard can't silently under-cover), and a
stale entry can't linger after its tool stops refining.

Limitation (documented, accepted): body-level marker detection only — a tool
that reaches a similarity refine through an un-marked cross-module helper would
be missed. That matches how the guarded set was originally derived (a repo-wide
marker grep) and is the pragmatic invariant; revisit if indirection appears.
"""

from __future__ import annotations

import ast
import pathlib

import genesis
from genesis.observability.mcp_guarded_tools import GUARDED_MCP_TOOLS

_MCP_DIR = pathlib.Path(genesis.__file__).parent / "mcp"

# Signatures of "select the row to overwrite by fuzzy/similarity match".
_REFINE_MARKERS = ("_checked", "find_similar", "SequenceMatcher", ".ratio(")


def _is_mcp_tool(node: ast.AST) -> bool:
    """True if a def is decorated with @mcp.tool()/@mcp.tool."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr == "tool":
            return True
    return False


def _tools_with_refine_marker() -> set[str]:
    found: set[str] = set()
    for path in _MCP_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not _is_mcp_tool(node):
                continue
            body_src = ast.get_source_segment(text, node) or ""
            if any(marker in body_src for marker in _REFINE_MARKERS):
                found.add(node.name)
    return found


def test_guarded_set_matches_similarity_refine_writers():
    detected = _tools_with_refine_marker()
    assert detected == set(GUARDED_MCP_TOOLS), (
        "GUARDED_MCP_TOOLS is out of sync with the MCP tools that overwrite an "
        "existing row by similarity match.\n"
        f"  detected refine-writers: {sorted(detected)}\n"
        f"  GUARDED_MCP_TOOLS:       {sorted(GUARDED_MCP_TOOLS)}\n"
        "If you added a fuzzy-matched refine tool, add it to GUARDED_MCP_TOOLS "
        "(observability/mcp_guarded_tools.py). If a tool stopped refining, remove "
        "it. If this is a false marker match, refactor or widen the allowlist."
    )


def test_procedure_store_is_detected():
    # Sanity: the known refine writer is actually found by the scanner, so a
    # green suite means the scan works (not that it silently found nothing).
    assert "procedure_store" in _tools_with_refine_marker()
