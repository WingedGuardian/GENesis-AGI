"""The ONE reader of plan documents for hooks: which plan, and which part is live.

Two hooks read the plan an ExitPlanMode is about, and each used to carry its own
copy of both questions, which disagreed:

* ``adopt_first_gate --plan`` resolved the path from the authoritative
  ``planFilePath`` field, but split the live part with a regex that required the
  decorative ``═`` rule, so it missed a one-``#`` divider and a divider followed by
  text (2 of 66 real plans, MEASURED 2026-10-19), and it split the RAW text, so a
  divider shown inside a fenced example cut the plan short.
* ``plan_bookmark_hook`` read the live part correctly, but never read
  ``planFilePath``: it regex-scanned ``str(tool_input)``, where the full ``plan``
  markdown serializes first, so it returned the first plan path quoted in the
  plan's own prose. With nothing found it took the newest plan on the box, from
  any session. MEASURED 2026-10-19: 243 of 243 ExitPlanMode tool inputs across 60
  recent transcripts carry ``planFilePath``.

So this module holds one copy of each answer, taken from the hook that had it
right. Stdlib, the sibling ``hook_input`` (the one place that knows the payload
contract) and markdown-it-py, the CommonMark parser the merge gate already uses.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import tool_input  # noqa: E402

_BLOB_PLAN_PATH = re.compile(r"(/[^\s\"']+\.claude/plans/[^\s\"']+\.md)")

#: A divider is a heading whose rendered text BEGINS with the phrase, after an
#: optional decorative rule: `## ═══ SUPERSEDED BELOW ═══`, `# ═══ SUPERSEDED BELOW
#: — …`, `## SUPERSEDED BELOW`. A heading that merely MENTIONS the phrase (a plan
#: titled "teach hooks the SUPERSEDED BELOW divider") is not one: matching it would
#: empty the live section, and the adopt-first gate passes an empty plan.
_DIVIDER = re.compile(r"^(?:═+\s*)?SUPERSEDED BELOW")

#: Block tokens whose lines are quoted material, never the author's proposal or
#: verdict: fenced and indented code, and raw HTML blocks (comments included).
_QUOTED = frozenset({"fence", "code_block", "html_block"})


def plan_path(payload: dict) -> str:
    """The plan file this ExitPlanMode is about, or "" when it cannot be known.

    The AUTHORITATIVE field first: ``planFilePath`` is in every real payload
    (counts in the module docstring). The blob scan is a fallback for older
    shapes only, and it is a fallback for a reason: ``plan`` (the full markdown)
    serializes BEFORE ``planFilePath``, so a first-match scan can return a path
    quoted inside the plan's own prose.

    With no path in the payload, never guess another session's plan: the only
    honest correlation is the session id in the filename, and absent that, a
    single unambiguous file is the only safe pick.
    """
    for key in ("planFilePath", "plan_file_path"):
        direct = tool_input(payload).get(key) or payload.get(key)
        if isinstance(direct, str) and direct.endswith(".md"):
            return direct
    # Older shapes: the path the TOOL returned before any path the plan's prose
    # quotes (the bookmark always searched the output first). Claude Code's
    # ExitPlanMode result is {plan, isAgent, filePath} with the plan text FIRST
    # (MEASURED 2026-10-21, 3 of 3 recent results), so read its path field, then
    # scan the result WITHOUT its plan text. Read straight from the payload, not
    # hook_input.tool_response, whose legacy env fallback can hold another
    # tool's result.
    for source in (payload.get("tool_response"), payload.get("tool_output")):
        if isinstance(source, str):
            with contextlib.suppress(ValueError):
                source = json.loads(source)
        if isinstance(source, dict):
            for key in ("filePath", "planFilePath", "plan_file_path"):
                value = source.get(key)
                if isinstance(value, str) and value.endswith(".md"):
                    return value
            source = {k: v for k, v in source.items() if k != "plan"}
        if source:
            text = source if isinstance(source, str) else json.dumps(source)
            match = _BLOB_PLAN_PATH.search(text)
            if match:
                return match.group(1)
    match = _BLOB_PLAN_PATH.search(json.dumps(payload))
    if match:
        return match.group(1)
    plans = Path.home() / ".claude" / "plans"
    if plans.is_dir():
        try:
            candidates = sorted(plans.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            sid = str(payload.get("session_id") or "")
            if sid:
                own = [p for p in candidates if sid in p.name]
                if own:
                    return str(own[0])
            if len(candidates) == 1:
                return str(candidates[0])
        except OSError:
            pass
    return ""


def _parser():
    """A CommonMark parser (markdown-it-py, a declared dependency), or None.

    The plan is read AS IT RENDERS, the same way the merge gate reads PR bodies
    (``git_push_guard._rework_markdown``): hand-written line rules for fences,
    comments and dividers drew a new edge case in each review round.
    """
    try:
        from markdown_it import MarkdownIt  # noqa: PLC0415 — optional at import time
    except ImportError:
        return None
    return MarkdownIt("commonmark")


def _frontmatter_end(lines: list[str]) -> int:
    """Index of the first line after a leading YAML frontmatter block (0 if none).

    CommonMark has no frontmatter: it would read the block as a rule plus a
    setext heading, so it is skipped before parsing. Same definition as
    ``plan_bookmark_hook._title_candidate_lines``: both fences are exactly
    ``---`` at column zero, and the content between them must load as a YAML
    mapping, so a plan that opens with a thematic break and uses another later
    is not mistaken for one.
    """
    if lines and lines[0] == "---":
        for idx in range(1, len(lines)):
            if lines[idx] == "---":
                return idx + 1 if _is_yaml_mapping("\n".join(lines[1:idx])) else 0
    return 0


def _is_yaml_mapping(block: str) -> bool:
    """Whether ``block`` loads as a YAML mapping. True without PyYAML, as in
    ``plan_bookmark_hook._is_yaml_mapping`` (the paired-fence reading)."""
    try:
        import yaml  # noqa: PLC0415 — optional at import time
    except ImportError:
        return True
    try:
        return isinstance(yaml.safe_load(block), dict)
    except yaml.YAMLError:
        return False


def _visible_text(inline) -> str:
    """An inline token's rendered text: emphasis markers and inline HTML dropped,
    as ``git_push_guard._rework_inline_text`` reads a CommonMark heading."""
    return "".join(
        child.content
        for child in (inline.children or [])
        if child.type in ("text", "code_inline")
    )


def _blocks(text: str):
    """``(lines, offset, tokens)`` for ``text``, or None without a parser.

    ``offset`` is where the parsed body starts (after frontmatter); token
    ``map`` ranges are relative to it. Lines are split on newline only, as
    CommonMark does (``splitlines()`` also breaks at U+2028).
    """
    md = _parser()
    if md is None:
        return None
    lines = text.replace("\r\n", "\n").split("\n")
    offset = _frontmatter_end(lines)
    return lines, offset, md.parse("\n".join(lines[offset:]))


def strip_quoted(text: str) -> str:
    """``text`` with every line of quoted material blanked (line count kept):
    fenced or indented code and raw HTML blocks. A path or a verdict shown in an
    example is not a proposal or a verdict. Without a parser nothing is
    stripped, so every path in the plan counts."""
    parsed = _blocks(text)
    if parsed is None:
        return text
    lines, offset, tokens = parsed
    for tok in tokens:
        if tok.type in _QUOTED and tok.map:
            for idx in range(offset + tok.map[0], offset + tok.map[1]):
                lines[idx] = ""
    return "\n".join(lines)


def live_section(content: str) -> str:
    """The part of a plan above its superseded-content divider.

    The convention is documented in the genesis-development skill
    (`references/plan-docs.md`): a heading beginning SUPERSEDED BELOW divides
    live plan content from archaeology kept for provenance. No divider means the
    whole document is live, which is also the correct reading for every plan
    written before the convention existed.

    The divider is a TOP-LEVEL heading (ATX or setext) as CommonMark renders it,
    so the phrase inside a code block, an HTML comment, a blockquote or a list,
    or quoted in a paragraph, never divides. A divider with no block above it
    (a comment renders nothing, so it does not count) is a plan TITLED with
    the phrase, not a divider: taking it would leave an empty live section. The
    heading is matched on its RENDERED text, so emphasis does not hide it. Without a parser the whole plan is live.
    """
    parsed = _blocks(content)
    if parsed is None:
        return content
    lines, offset, tokens = parsed
    seen_block = False
    for i, tok in enumerate(tokens):
        if tok.level != 0 or tok.type.endswith("_close"):
            continue
        if tok.type == "heading_open" and seen_block and tok.map:
            text = _visible_text(tokens[i + 1]).strip() if i + 1 < len(tokens) else ""
            if _DIVIDER.match(text):
                return "\n".join(lines[: offset + tok.map[0]])
        # A comment-only HTML block renders nothing, so it is not a block above
        # a title (as ``git_push_guard._rework_blocks`` omits it).
        if tok.type == "html_block" and not re.sub(r"<!--.*?-->", "", tok.content, flags=re.DOTALL).strip():
            continue
        seen_block = True
    return content
