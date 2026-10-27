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


#: The plan-time premise section's title (``.claude/docs/premise-check.md``, "At
#: plan and issue time"), matched on a heading's RENDERED text at any level. Real
#: plans use all four spellings (MEASURED 2026-10-27 over one install's 72 plans:
#: "Premise check" 8, "Premise questions" 4, "Premise checks" 1); the issue form
#: says "Premise checks".
_PREMISE_TITLE = re.compile(r"premise\s+(?:checks?|questions?)\b", re.IGNORECASE)
#: The line-rule fallback, used only without a parser.
_PREMISE_HEADING_LINE = re.compile(r"^#{1,6}[ \t]+premise[ \t]+(?:checks?|questions?)\b", re.IGNORECASE)
_ANY_HEADING_LINE = re.compile(r"^#{1,6}[ \t]")
_ANSWER_LINE = re.compile(r"^ {0,3}([1-9])[.)](?:[ \t]+(.*))?$")


def premise_answers(text: str) -> dict[int, str] | None:
    """The numbered answers under a plan's premise heading, or None without one.

    ``text`` is read as given; callers pass the live, quote-stripped section, so a
    heading shown inside an example does not count. Read AS IT RENDERS, like the
    rest of this module: the section runs from the first heading titled "Premise
    check(s)" or "Premise question(s)" to the next heading of the same or a higher
    level, and its answers are the items of its top-level ordered lists. An item
    nested inside an answer is part of that answer, never an answer of its own, and
    text after a list belongs to no answer.

    Answers are keyed by the number written on each item when those numbers are
    all distinct, so "1. 2. 4." reports answer 3 missing; an autonumbered list
    ("1. 1. 1.", which CommonMark renders as 1, 2, 3) is keyed by position. Each
    answer's text is its item's source text, joined.
    """
    parsed = _blocks(text)
    if parsed is None:
        return _premise_answers_by_line(text)
    _, _, tokens = parsed
    start = depth = None
    for i, tok in enumerate(tokens):
        title = _visible_text(tokens[i + 1]).strip() if i + 1 < len(tokens) else ""
        if tok.type == "heading_open" and tok.level == 0 and _PREMISE_TITLE.match(title):
            start, depth = i + 1, int(tok.tag[1:])
            break
    if start is None:
        return None
    items: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for tok in tokens[start + 1 :]:
        if tok.type == "heading_open" and tok.level == 0 and int(tok.tag[1:]) <= depth:
            break
        if tok.type == "list_item_open" and tok.level == 1 and tok.markup in (".", ")"):
            current = []
            items.append((tok.info, current))
        elif tok.type == "list_item_close" and tok.level == 1:
            current = None
        elif tok.type == "inline" and current is not None:
            current.append(tok.content.strip())
    written = [info for info, _ in items]
    by_number = all(n.isdigit() for n in written) and len(set(written)) == len(written)
    return {
        (int(info) if by_number else pos): " ".join(p for p in parts if p)
        for pos, (info, parts) in enumerate(items, start=1)
    }


def _premise_answers_by_line(text: str) -> dict[int, str] | None:
    """``premise_answers`` without a parser: line rules, so the check still runs."""
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if _PREMISE_HEADING_LINE.match(line)), None)
    if start is None:
        return None
    answers: dict[int, str] = {}
    current: int | None = None
    for line in lines[start + 1 :]:
        if _ANY_HEADING_LINE.match(line):
            break
        item = _ANSWER_LINE.match(line)
        if item and int(item.group(1)) not in answers:
            current = int(item.group(1))
            answers[current] = (item.group(2) or "").strip()
        elif current is not None and line.strip():
            answers[current] = f"{answers[current]} {line.strip()}".strip()
    return answers


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
