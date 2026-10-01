"""The `## Acceptance` section and source-pointer PR-body parser (issue #2736).

Sibling to `e2e_declaration.py`: same body bound, same fence handling, same
"never truncate" rule — except this reader REFUSES an oversized body rather than
scanning a prefix, because a section that sits past the bound must not be
reported absent.

Two things are read:

  * The FIRST heading matching ``^#{2,}\\s*Acceptance\\s*$`` (case-insensitive),
    outside fenced code blocks. Its list items are the acceptance criteria.
  * A source pointer naming where the work came from: ``Closes #N`` (or
    Fixes/Resolves/Refs/Part of), a ``Ledger: <32-hex>`` row, a
    ``Follow-up: <32-hex>`` row, or a ``Spec:``/``Plan:`` name.

Wiring this into the merge gate and `gh pr create` is maintainer work, out of
scope here. Pure functions, stdlib only, no I/O.
"""

from __future__ import annotations

import re

#: GitHub's PR-body cap. A body over this is refused, never truncated.
_MAX_BODY = 65_536

_COMMENT_OPEN, _COMMENT_CLOSE = "<!--", "-->"
_FENCE_MARKS = ("```", "~~~")

_HEADING_RE = re.compile(r"^#{2,}\s*Acceptance\s*$", re.IGNORECASE)
_ANY_HEADING_RE = re.compile(r"^#{1,6}(?:\s|$)")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_TASK_MARKER_RE = re.compile(r"^\[[ xX]\]\s*")
_COMMENT_SPAN_RE = re.compile(r"<!--.*?-->", re.DOTALL)

_ISSUE_RE = re.compile(
    r"^[ \t]*(?:Closes|Fixes|Resolves|Refs|Part of)[ \t]+#(\d+)[ \t]*$",
    re.IGNORECASE,
)
_SPEC_RE = re.compile(r"^[ \t]*(Spec|Plan):[ \t]*(\S[^ \t]*)[ \t]*$")
_SPEC_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")

#: Copied verbatim from src/genesis/session_awareness/repo_pulse.py — the test
#: asserts equality by ``.pattern`` so the two readers can never drift.
MARKER_RE = re.compile(r"[Ll]edger:\s*([0-9a-f]{32})(?![0-9a-fA-F])")
FOLLOWUP_MARKER_RE = re.compile(
    r"^[ \t]*(?i:follow-?up):[ \t]*([0-9a-f]{32})[ \t]*$", re.MULTILINE
)


def _visible_lines(body: str) -> list[str]:
    """Body lines with fenced blocks removed and HTML comments stripped.

    Same rules as the sibling's scanner: a fenced line is opaque and never
    interpreted; an unterminated comment opener hides to the end of the line.
    """
    visible: list[str] = []
    in_comment = False
    fence: str | None = None
    for line in body.splitlines():
        if fence is not None:
            if line.strip().startswith(fence):
                fence = None
            continue
        out: list[str] = []
        rest = line
        while rest:
            if in_comment:
                close = rest.find(_COMMENT_CLOSE)
                if close == -1:
                    rest = ""
                    break
                in_comment = False
                rest = rest[close + len(_COMMENT_CLOSE) :]
                continue
            open_at = rest.find(_COMMENT_OPEN)
            if open_at == -1:
                out.append(rest)
                break
            out.append(rest[:open_at])
            in_comment = True
            rest = rest[open_at + len(_COMMENT_OPEN) :]
        kept = "".join(out)
        stripped = kept.strip()
        for mark in _FENCE_MARKS:
            if stripped.startswith(mark):
                fence = mark
                break
        else:
            visible.append(kept)
    return visible


def _acceptance_bullets(lines: list[str]) -> tuple[bool, list[str]]:
    """Locate the Acceptance section and collect its list items."""
    in_section = False
    found = False
    bullets: list[str] = []
    for line in lines:
        if _ANY_HEADING_RE.match(line):
            if in_section:
                break
            if _HEADING_RE.match(line):
                in_section = True
                found = True
            continue
        if not in_section:
            continue
        m = _BULLET_RE.match(line)
        if not m:
            continue
        text = _COMMENT_SPAN_RE.sub("", m.group(1)).strip()
        # An unchecked/checked task marker alone is not a criterion; the
        # marker is stripped for the emptiness test only — a kept bullet
        # keeps its text as written.
        if _TASK_MARKER_RE.sub("", text):
            bullets.append(text)
    return found, bullets


_POINTER_PREFIX_RE = re.compile(r"^\s*(?:>\s*|(?:[-*+]|\d+[.)])\s+)")


def _unwrap(line: str) -> str:
    """Strip the wrappers a pointer line may carry.

    A pointer may sit behind blockquote markers, a list marker, and one pair
    of ``**`` emphasis — but no trailing text, which the anchored patterns
    still reject on the unwrapped line.
    """
    prev = None
    while prev != line:
        prev = line
        line = _POINTER_PREFIX_RE.sub("", line).strip()
    if len(line) > 4 and line.startswith("**") and line.endswith("**"):
        line = line[2:-2].strip()
    return line


def _source_pointer(lines: list[str]) -> tuple[dict | None, str | None]:
    """First source-pointer line wins; returns (source, problem)."""
    for raw in lines:
        line = _unwrap(raw)
        m = _ISSUE_RE.match(line)
        if m:
            return {"kind": "issue", "value": m.group(1)}, None
        m = MARKER_RE.search(line)
        if m and not line[: m.start()].strip() and not line[m.end() :].strip():
            return {"kind": "ledger", "value": m.group(1)}, None
        m = FOLLOWUP_MARKER_RE.match(line)
        if m:
            return {"kind": "follow_up", "value": m.group(1)}, None
        m = _SPEC_RE.match(line)
        if m:
            kind, value = m.group(1).lower(), m.group(2)
            if _SPEC_NAME_RE.fullmatch(value):
                return {"kind": kind, "value": value}, None
            if "/" in value:
                return None, "spec/plan pointer must be a name, not a path"
            return None, "spec/plan pointer is not a valid name"
    return None, None


def parse_acceptance(body: str | None) -> dict:
    """Read a PR body's ``## Acceptance`` section and source pointer.

    Returns ``{"present", "bullets", "source", "problems"}``. ``present`` is
    True only when the section exists AND has at least one bullet. Never
    raises on any ``str`` input; a body over the cap is refused whole.
    """
    result = {"present": False, "bullets": [], "source": None, "problems": []}
    problems: list[str] = result["problems"]

    if not body:
        problems.append("empty PR body")
        return result
    if len(body) > _MAX_BODY:
        problems.append(f"body too large to verify ({len(body)} chars)")
        return result

    lines = _visible_lines(body.replace("\r\n", "\n").replace("\r", "\n"))

    found, bullets = _acceptance_bullets(lines)
    if not found:
        problems.append("no ## Acceptance section")
    elif not bullets:
        problems.append("## Acceptance has no bullets")
    else:
        result["present"] = True
        result["bullets"] = bullets

    source, pointer_problem = _source_pointer(lines)
    result["source"] = source
    if pointer_problem:
        problems.append(pointer_problem)
    elif source is None:
        problems.append(
            "no source pointer (Closes #N, Ledger:, Follow-up:, Spec:, Plan:)"
        )
    return result
