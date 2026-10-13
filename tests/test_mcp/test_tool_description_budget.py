"""Every Genesis MCP tool description must fit the cap Claude Code applies.

Claude Code truncates each MCP tool description at **2,048 characters** before
the model sees it. It is not silent — CC logs the cut ("Tool "X" description
truncated from N to 2048 chars", in its per-server ``mcp-logs-*`` files) and
appends "… [truncated]" — but the text past the cut is gone from the model's
contract all the same. MEASURED on CC 2.1.280 (2026-09-22), with a control arm:
a fresh session read ``follow_up_create`` (then 4,733 chars) back as CUT, and
the 211-char ``module_list`` as FULL.

What sat past the cut was call-time contract — the ``work_state`` enum, the rule
that ``blocked_on_trigger`` REQUIRES ``revisit_condition``, the invalid
``blocked_on_trigger`` + ``surplus_task`` pairing, ``scheduled_at``'s
requirement. A caller cannot choose from an enum it cannot see.

The bar is CC's own 2,048, not a raised one. CC 2.1.280 added
``CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH`` to lift it, but a description that
relies on that lever is still truncated on any older CC and on any install
where the setting is absent — so fitting under 2,048 is the only version that
holds everywhere. If a future change ships the raised cap as a seeded default,
this bar should move with it and be read from whatever constant that change
introduces, never restated here.

Install-agnostic: pure AST over the repo's own source. No live server, no
network, no DB.
"""

from __future__ import annotations

import ast
import unicodedata
from pathlib import Path

from tests.conftest import private_module

_REPO = Path(__file__).resolve().parents[2]
_MCP_ROOT = _REPO / "src" / "genesis" / "mcp"

#: Claude Code's MCP tool-description cap where nothing raises it.
_CC_DESCRIPTION_CAP = 2048

#: The repo's ONE definition of "an MCP tool", owned by scripts/export_agents_md.py.
#: Reused rather than restated: a second, looser predicate here (a substring match
#: on the unparsed decorator) agreed with it on all 147 tools, and would have
#: silently diverged the day a decorator named anything containing "tool" appeared.
_is_mcp_tool_decorator = private_module(
    "export_agents_md", _REPO / "scripts" / "export_agents_md.py"
)._is_mcp_tool_decorator


def _tool_descriptions() -> list[tuple[str, str, int]]:
    """(name, path, description length) for every Genesis MCP tool function.

    The description CC receives is ``inspect.getdoc(fn)`` (fastmcp v2,
    ``FunctionTool``), which ``ast.get_docstring`` — cleandoc'd — reproduces;
    verified against the live server object, where raw ``__doc__`` differs
    materially (``direct_session_run``: 2,131 raw vs 2,006 cleaned).

    Measured in NFKC-normalized characters, because that is what CC's own log
    matches: it reported the pre-change ``follow_up_create`` as 4,735 where
    ``getdoc`` gives 4,733 — its one ``…`` normalizes to ``...`` (+2) — and
    ``memory_recall`` as 3,525 in both (2 of 2 tools). A raw codepoint count
    would pass a 2,046-char description holding a single ``…`` that CC cuts.

    Enumerated by AST rather than regex on purpose: an earlier regex pass over
    these same files found 21 of 147 tools and reported "0 over the cap", which
    is the silent under-read this repo's evidence rules warn about.
    """
    found: list[tuple[str, str, int]] = []
    for path in sorted(_MCP_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            # NOT `continue`: a silently dropped file removes its tools from the
            # check while the population floor (loose by design) still passes.
            # MEASURED: the largest file holds 13 of 147 tools, so no single
            # file's loss could trip a >=100 floor.
            raise AssertionError(
                f"{path.relative_to(_REPO)} does not parse ({exc}); the cap check "
                "would silently skip its tools"
            ) from exc
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(_is_mcp_tool_decorator(d) for d in node.decorator_list):
                continue
            doc = ast.get_docstring(node) or ""
            found.append(
                (node.name, str(path.relative_to(_REPO)), len(unicodedata.normalize("NFKC", doc)))
            )
    return found


def test_mcp_tool_population_is_discoverable():
    """The walk must find a real population — an empty list voids the cap test.

    Guard-the-guard: a cap test over an empty list passes trivially, which is
    indistinguishable from a clean result. 147 tools were enumerated when this
    was written; the floor is deliberately loose (it pins "the walk still
    works", not an inventory every new tool would have to update).
    """
    tools = _tool_descriptions()
    assert len(tools) >= 100, (
        f"only {len(tools)} MCP tool functions found under "
        f"{_MCP_ROOT.relative_to(_REPO)} — expected 100+. The decorator walk has "
        "probably stopped matching (a renamed decorator, a new registration "
        "style). Fix the walk: a cap test over a shrunken population is a test "
        "that silently stops checking."
    )
    assert all(length > 0 for _, _, length in tools), (
        "an MCP tool has an empty docstring — CC would send it no description "
        "at all: " + ", ".join(n for n, _, ln in tools if ln == 0)
    )


def test_no_mcp_tool_description_exceeds_the_cc_cap():
    """No Genesis tool description may exceed CC's 2,048-character cap.

    Past the cap, CC cuts the text (logging the cut and marking it
    "… [truncated]"), so the tail is simply absent from the contract the model
    reads — however the call site was written.
    """
    over = [(n, p, ln) for n, p, ln in _tool_descriptions() if ln > _CC_DESCRIPTION_CAP]
    assert not over, (
        f"MCP tool description(s) exceed Claude Code's {_CC_DESCRIPTION_CAP}-char "
        "cap; everything past it is cut before the model sees it:\n"
        + "\n".join(
            f"  {ln:>6} chars  {n}  ({p})"
            for n, p, ln in sorted(over, reverse=True, key=lambda t: t[2])
        )
        + "\n\nFix by MOVING PROSE OUT, not by leaning on a raised cap: rationale, "
        "routing essays and anything duplicated from CLAUDE.md belong in a "
        "reference doc or a code comment. What must stay is the call-time "
        "contract a caller cannot look up elsewhere — enums, formats, "
        "required-when rules, invalid combinations."
    )
