"""The `## Acceptance` section and source-pointer PR-body parser (issue #2736).

Reads a closed grammar over the shared ``readable_body`` view. Pure functions,
stdlib only (no third-party packages), no I/O.
"""

# CONTRACT: issue #2736, "Spec" plus "Amendment: closed grammar", and the owner's
# rulings recorded with the PR. This reader recognises ONLY that grammar and does
# not interpret any other CommonMark. Wiring it into the merge gate and
# `gh pr create` is maintainer work, out of scope here.
#
# VISIBILITY comes from the SHARED scanner, `readable_body` in
# `scripts/check_cc_pin_receipts.py` with `keep_blank=True`: HTML comments and
# fenced blocks are removed there, and blank lines survive as "" so paragraph
# boundaries can end an item. There is deliberately NO local fallback: a second
# scanner is a second contract. If the sibling cannot be loaded, or predates
# `keep_blank`, the parse reports that instead of guessing.
#
# SIZE: a body over GitHub's 65,536-character cap is REFUSED whole, never
# truncated, so a section past the bound is never reported absent.
#
# THE GRAMMAR, in the order the rules apply:
#   * Indent = leading spaces, a leading tab counting 4. Digits are ASCII only.
#     A marker is `-`, `*`, `+`, or 1-9 ASCII digits then `.` or `)`, followed by
#     a space/tab or the end of the line.
#   * Section: the first line, indent 0-3, matching `#{2,6}[ \t]+Acceptance[ \t]*$`
#     (case-insensitive). It ends at the next line, indent 0-3, matching
#     `#{1,6}([ \t]|$)`, at a setext underline (`---`, `===` or a lone `-`,
#     indent 0-3, directly under paragraph text: GitHub renders the text above
#     it as a heading), or at the end of the body.
#   * Items, first matching rule wins: (1) a pointer line, valid or malformed, is
#     never criterion text and closes the open item; so does a thematic break
#     (indent 0-3) or a `>` quote line (indent 0-1), which GitHub renders as
#     ending the list item; (2) a marker at indent 0-3 starts an item; (3) a marker
#     at indent 2+ starts a nested item while one is open; (4) a non-blank line at
#     indent 2+ continues the open item; (5) so does a line at indent 0-1 directly
#     after it; (6) a blank line keeps the item open only if the next non-blank
#     line has indent 2+; (7) anything else is prose or indented code and ignored.
#     An item that is empty, checkbox-only or a pointer is discarded; a checkbox's
#     text is otherwise kept as written (`[x] item`).
#   * Pointer line: indent 0-3; at most one wrapper (`>` and optional whitespace,
#     or one marker and whitespace); an optional matched `**`/`__`/`*`/`_` pair;
#     at most one trailing `.`/`,`/`;`/`!`, inside or outside the emphasis. Forms:
#     `Closes|Fixes|Resolves|Refs|Part of #N` (keyword case-insensitive, ASCII
#     folding only, so `Cloſes` is not `Closes`), `Ledger: <32 hex>`,
#     `Follow-up: <32 hex>`, and `Spec:`/`Plan:` judged by the value: one name
#     token is a pointer; a token with `/` is "must be a name, not a path"; nothing
#     is "has no name"; another single token is "not a valid name"; more than one
#     word is prose. A malformed `Closes`/`Ledger:`/`Follow-up:` line is prose.
#     The first pointer line in body order wins, valid or malformed.
#   * Only a fence at column 0 is trusted. A body with any other fence boundary
#     (an opener behind `>` or list-marker wrappers, or an indented opener or
#     closer) is REFUSED whole, like an oversized one; fence-like text inside a
#     fence the scanner reads correctly, or inside a comment, is content and does
#     not count. The scanner does not model containers (#2791): it
#     misses a wrapped opener, takes an indented-code line for a fence, and never
#     closes a list item's fence when the item ends. Each flips its fence state,
#     and the NEXT real fence then reads backwards, so text GitHub renders as
#     code becomes visible here and could be taken as the pointer (measured: a
#     list-item fence followed by a real fenced `Closes #9` reported issue 9). No
#     reader downstream of a flipped scanner can repair what it never sees.
#     Measured 2026-10-03: 0 of 1,902 merged PR bodies, and the PR template, have
#     such a line.
#
# THREAT MODEL: none adversarial. The body's author controls it and can always
# write a real pointer, so a misreading can mislead an honest author but cannot
# bypass anything. The fence refusal covers every scanner misreading measured
# (containers, indentation, a non-breaking space, a backtick info string, a fence
# exposed by comment removal); the shared scanner stays the authority (#2791).
#
# KNOWN LIMITS, outside the grammar (counts over 1,902 merged PR bodies,
# 2026-10-03). Occurring 0 times: a setext `Acceptance` heading as the section
# start, `## Acceptance ##`, containers nested two wrappers deep. Not measured: a
# code span running across lines (``see `x` + newline + `Closes #9` + newline +
# `` ` ``), whose middle line still reads as a pointer. Text-only effects: the
# scanner drops a comment-only line
# (12 bodies right after a list item) and a fenced block (6 bodies directly
# after one), though GitHub ends the list at both, so the next line joins the
# item; after an empty or checkbox-only item it can create one (0 bodies). The fix is a
# boundary from the shared scanner (#2791). HTML blocks (`<div>`, `<details>`;
# 7 bodies) are not interpreted. An ordered marker other than `1.` directly
# under paragraph text counts as an item although GitHub renders it as text
# (18 bodies; rule 2 as written).

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
from pathlib import Path

_MAX_BODY = 65_536

#: Copied verbatim from src/genesis/session_awareness/repo_pulse.py — the test
#: asserts equality by ``.pattern`` so the two readers can never drift.
MARKER_RE = re.compile(r"[Ll]edger:\s*([0-9a-f]{32})(?![0-9a-fA-F])")
FOLLOWUP_MARKER_RE = re.compile(r"^[ \t]*(?i:follow-?up):[ \t]*([0-9a-f]{32})[ \t]*$", re.MULTILINE)

_MARK = r"(?:[-*+]|[0-9]{1,9}[.)])"
_START_RE = re.compile(r"#{2,6}[ \t]+Acceptance[ \t]*", re.IGNORECASE | re.ASCII)
_END_RE = re.compile(r"#{1,6}(?:[ \t]|$)")
_ITEM_RE = re.compile(_MARK + r"(?:[ \t]+(.*)|)")
_WRAP_RE = re.compile(r"(?:>[ \t]*|" + _MARK + r"[ \t]+)?(.*)")
_WRAPPED_FENCE_RE = re.compile(r"[ \t]*(?:(?:>|" + _MARK + r"[ \t])[ \t]*)+(?:`{3}|~{3})")
_FENCE_RUN = re.compile(r"`{3,}|~{3,}")
_FENCE_MARKS = ("```", "~~~")
_LINE_ENDS = re.compile(r"\r\n|\r|\n")
_ISSUE_RE = re.compile(
    r"(?:closes|fixes|resolves|refs|part of)[ \t]+#([0-9]+)", re.IGNORECASE | re.ASCII
)
_SPEC_RE = re.compile(r"(Spec|Plan):[ \t]*(.*)")
_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
_BREAK_RE = re.compile(r"([-*_])[ \t]*(?:\1[ \t]*){2,}")
_SETEXT_RE = re.compile(r"=+|-+")
_CHECKBOX_RE = re.compile(r"\[[ xX]\]")
_PUNCT = ".,;!"
_NO_POINTER = "no source pointer (Closes #N, Ledger:, Follow-up:, Spec:, Plan:)"

_READABLE_BODY_UNSET = object()
_READABLE_BODY_FN = _READABLE_BODY_UNSET


def _load_sibling_readable_body():
    # `readable_body` from `check_cc_pin_receipts.py`, or None.
    name = "_cc_pin_receipts_for_acceptance"
    try:
        path = Path(__file__).resolve().parent / "check_cc_pin_receipts.py"
        spec = importlib.util.spec_from_file_location(name, path)
        if not (spec and spec.loader):
            return None
        mod = importlib.util.module_from_spec(spec)
        # Registered BEFORE exec: that module's dataclasses resolve their own
        # module out of sys.modules, so exec-ing an unregistered module raises.
        # On failure the half-initialised entry is removed.
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            sys.modules.pop(name, None)
            raise
        fn = getattr(mod, "readable_body", None)
        # A sibling without keep_blank cannot mark paragraph ends, and calling
        # it with the keyword would raise inside parse_acceptance. The fence
        # check below reuses its comment rule, so that must exist too.
        if not callable(fn) or "keep_blank" not in inspect.signature(fn).parameters:
            return None
        return fn if callable(getattr(mod, "_outside_comments", None)) else None
    except Exception:
        return None


def _readable_body():
    global _READABLE_BODY_FN
    if _READABLE_BODY_FN is _READABLE_BODY_UNSET:
        _READABLE_BODY_FN = _load_sibling_readable_body()
    return _READABLE_BODY_FN


def _misread_fence(body: str, outside_comments) -> bool:
    # True if the shared scanner takes a fence boundary GitHub would not.
    # Walks the lines with the scanner's own fence and comment decisions, and
    # flags the first one that is not a column-0 fence. Lines inside a fence the
    # scanner reads correctly are content and never count.
    fence, in_comment = None, False
    for line in _LINE_ENDS.split(body):
        if fence is not None:
            bare = line.strip()
            run = _FENCE_RUN.match(bare)
            closes = run and run[0][0] == fence[0] and len(run[0]) >= len(fence)
            if closes and not bare[run.end() :].strip():
                if not line.startswith(run[0]):
                    return True  # an indented closer
                fence = None
            continue
        text, in_comment = outside_comments(line, in_comment)
        if _WRAPPED_FENCE_RE.match(text):
            return True  # an opener behind `>` or a list marker
        if text.strip().startswith(_FENCE_MARKS):
            run = _FENCE_RUN.match(text)
            if not run or not line.startswith(run[0]):
                return True  # indented, or exposed by removing a leading comment
            if run[0][0] == "`" and "`" in text[run.end() :]:
                return True  # a backtick info string cannot hold a backtick: inline code
            fence = run[0]
    return False


def _indent(line: str) -> int:
    # Leading spaces, a leading tab counting 4.
    lead = line[: len(line) - len(line.lstrip(" \t"))]
    return len(lead) + 3 * lead.count("\t")


def _peel(text: str) -> str:
    # Strip at most one trailing punctuation mark and one emphasis pair.
    text = text.rstrip(" \t")
    punct = len(text) > 1 and text[-1] in _PUNCT
    if punct:
        text = text[:-1]
    for tok in ("**", "__", "*", "_"):
        if len(text) > 2 * len(tok) and text.startswith(tok) and text.endswith(tok):
            text = text[len(tok) : -len(tok)]
            break
    if not punct and len(text) > 1 and text[-1] in _PUNCT:
        text = text[:-1]
    return text


def _pointer(line: str) -> tuple[dict | None, str | None] | None:
    # None, `(source, None)` for a pointer, or `(None, problem)` if malformed.
    if _indent(line) > 3:
        return None
    core = _peel(_WRAP_RE.fullmatch(line.lstrip(" \t")).group(1))
    if m := _ISSUE_RE.fullmatch(core):
        return {"kind": "issue", "value": m.group(1)}, None
    if m := MARKER_RE.fullmatch(core):
        return {"kind": "ledger", "value": m.group(1)}, None
    if m := FOLLOWUP_MARKER_RE.fullmatch(core):
        return {"kind": "follow_up", "value": m.group(1)}, None
    m = _SPEC_RE.fullmatch(core)
    if not m or len(m.group(2).split()) > 1:
        return None
    kind, value = m.group(1).lower(), m.group(2)
    if _NAME_RE.fullmatch(value):
        return {"kind": kind, "value": value}, None
    if not value:
        return None, "spec/plan pointer has no name"
    if "/" in value:
        return None, "spec/plan pointer must be a name, not a path"
    return None, "spec/plan pointer is not a valid name"


def _items(lines: list[str]) -> tuple[bool, list[str]]:
    # `(section found, criteria)` for the first Acceptance section.
    found, items, cur, gap, after_text = False, [], None, False, False
    for line in lines:
        ind, bare = _indent(line), line.strip(" \t")
        if not found:
            found = ind <= 3 and bool(_START_RE.fullmatch(bare))
            continue
        if ind <= 3 and _END_RE.match(bare):
            break
        if not bare:
            gap, after_text = True, False
            continue
        if gap and (ind < 2 or (cur is not None and not items[cur])):
            cur = None  # rule 6; an EMPTY item never survives a blank line
        gap = False
        if after_text and ind <= 3 and _SETEXT_RE.fullmatch(bare):
            break  # paragraph text over `---`/`===`/`-` is a setext heading
        item = _ITEM_RE.fullmatch(bare)
        pointer = _pointer(line) is not None
        enders = (ind <= 3 and _BREAK_RE.fullmatch(bare)) or (ind < 2 and bare[0] == ">")
        text = False
        if pointer or enders:
            cur, text = None, pointer
        elif item and (ind <= 3 or cur is not None):
            items.append((item.group(1) or "").strip())
            cur = len(items) - 1
        elif cur is not None and (ind >= 2 or items[cur]):
            # Rule 5's lazy line needs a paragraph to continue; an empty item has none.
            items[cur] = (items[cur] + " " + bare).strip()
        else:
            # Indented code cannot interrupt a paragraph, so an indented line
            # under paragraph text continues that paragraph.
            cur, text = None, ind <= 3 or after_text
        after_text = text
    keep = [t for t in items if t and not _CHECKBOX_RE.fullmatch(t) and _pointer(t) is None]
    return found, keep


def parse_acceptance(body: str | None) -> dict:
    """``{"present", "bullets", "source", "problems"}`` for a PR body; never raises."""
    result = {"present": False, "bullets": [], "source": None, "problems": []}
    problems: list[str] = result["problems"]
    if not body:
        problems.append("empty PR body")
        return result
    if len(body) > _MAX_BODY:
        problems.append(f"body too large to verify ({len(body)} chars)")
        return result
    readable_body = _readable_body()
    if readable_body is None:
        problems.append("cannot load readable_body from check_cc_pin_receipts.py")
        return result
    if _misread_fence(body, readable_body.__globals__["_outside_comments"]):
        problems.append(
            "cannot read this body reliably: a code fence is indented or inside"
            " a list item or quote (start fences at column 0)"
        )
        return result

    lines = readable_body(body, keep_blank=True).split("\n")
    found, bullets = _items(lines)
    if not found:
        problems.append("no ## Acceptance section")
    elif not bullets:
        problems.append("## Acceptance has no bullets")
    else:
        result["present"], result["bullets"] = True, bullets

    source, problem = next((r for r in map(_pointer, lines) if r is not None), (None, None))
    result["source"] = source
    if problem:
        problems.append(problem)
    elif source is None:
        problems.append(_NO_POINTER)
    return result
