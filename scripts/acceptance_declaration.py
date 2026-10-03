"""The `## Acceptance` section and source-pointer PR-body parser (issue #2736).

Sibling to `e2e_declaration.py`: same body bound — except this reader REFUSES
an oversized body rather than scanning a prefix, because a section that sits
past the bound must not be reported absent.

Visibility — which lines a human actually reads — comes from the SHARED
scanner: ``readable_body`` in ``scripts/check_cc_pin_receipts.py``, loaded
repo-locally the way ``e2e_declaration.py`` does (registered in ``sys.modules``
before ``exec_module``, popped on failure, cached). There is deliberately NO
local fallback: a second scanner is a second contract, and "same scanner"
must not be a claim that is false whenever the sibling fails to import. If
the load fails the parse reports it instead of guessing.

Two things are read:

  * The FIRST heading matching ``^#{2,6}[ \\t]+Acceptance[ \\t]*$``
    (case-insensitive). Its list items are the acceptance criteria, with
    continuation lines joined in.
  * A source pointer naming where the work came from: ``Closes #N`` (or
    Fixes/Resolves/Refs/Part of), a ``Ledger: <32-hex>`` row, a
    ``Follow-up: <32-hex>`` row, or a ``Spec:``/``Plan:`` name. Pointers may
    be wrapped (``>``, a list marker, ``**…**``) but carry no trailing text.

Wiring this into the merge gate and `gh pr create` is maintainer work, out of
scope here. Pure functions, stdlib only (no third-party packages), no I/O.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

#: GitHub's PR-body cap. A body over this is refused, never truncated.
_MAX_BODY = 65_536

_HEADING_RE = re.compile(r"^#{2,6}[ \t]+Acceptance[ \t]*$", re.IGNORECASE)
_ANY_HEADING_RE = re.compile(r"^#{1,6}(?:\s|$)")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|[0-9]+[.)])\s+(.*)$")
_TASK_MARKER_RE = re.compile(r"^\[[ xX]\]\s*")

_ISSUE_RE = re.compile(
    r"^[ \t]*(?:Closes|Fixes|Resolves|Refs|Part of)[ \t]+#([0-9]+)[ \t]*$",
    re.IGNORECASE,
)
_SPEC_LINE_RE = re.compile(r"^(Spec|Plan):(.*)$")
_SPEC_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
_TRAILING_PUNCT_RE = re.compile(r"[ \t]*[.,;:!?]+[ \t]*$")

#: Copied verbatim from src/genesis/session_awareness/repo_pulse.py — the test
#: asserts equality by ``.pattern`` so the two readers can never drift.
MARKER_RE = re.compile(r"[Ll]edger:\s*([0-9a-f]{32})(?![0-9a-fA-F])")
FOLLOWUP_MARKER_RE = re.compile(
    r"^[ \t]*(?i:follow-?up):[ \t]*([0-9a-f]{32})[ \t]*$", re.MULTILINE
)

_READABLE_BODY_UNSET = object()
_READABLE_BODY_FN = _READABLE_BODY_UNSET


def _load_sibling_readable_body():
    """``readable_body`` from ``check_cc_pin_receipts.py`` — the SAME scanner the
    pin gate and the E2E reader use, so all body-readers share one contract."""
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
        return fn if callable(fn) else None
    except Exception:
        return None


def _readable_body():
    global _READABLE_BODY_FN
    if _READABLE_BODY_FN is _READABLE_BODY_UNSET:
        _READABLE_BODY_FN = _load_sibling_readable_body()
    return _READABLE_BODY_FN


def _acceptance_bullets(lines: list[str]) -> tuple[bool, list[str]]:
    """Locate the Acceptance section and collect its list items.

    A non-blank, non-bullet, non-heading line inside the section is a
    CONTINUATION: it joins the current bullet (single space, each part
    stripped) when no blank line has intervened, or when it is indented by
    two or more spaces. Otherwise it ends the current item.
    """
    in_section = False
    found = False
    bullets: list[str] = []
    item_open = False
    blank_since_item = False
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
        if m:
            text = m.group(1).strip()
            stripped = _TASK_MARKER_RE.sub("", text)
            # An empty/comment-only/task-only line is not a criterion, and a
            # bullet that is only a source pointer is not one either — the
            # pointer still counts via the whole-body scan.
            if stripped and _match_pointer(stripped) is None:
                bullets.append(text)
                item_open = True
            else:
                item_open = False
            blank_since_item = False
            continue
        if not line.strip():
            blank_since_item = True
            continue
        if item_open and (not blank_since_item or len(line) - len(line.lstrip()) >= 2):
            bullets[-1] = bullets[-1] + " " + line.strip()
            blank_since_item = False
        else:
            item_open = False
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


def _match_pointer(line: str) -> tuple[dict | None, str | None] | None:
    """Match one line against the pointer shapes.

    Returns ``None`` when the line is not a pointer attempt at all; otherwise
    ``(source, None)`` on a match or ``(None, problem)`` when the attempt is
    malformed — the first pointer wins even when malformed, so a problem
    stops the scan just like a hit does.
    """
    line = _unwrap(line)
    m = _SPEC_LINE_RE.match(line)
    if m:
        kind = m.group(1).lower()
        value = _TRAILING_PUNCT_RE.sub("", m.group(2)).strip()
        if _SPEC_NAME_RE.fullmatch(value):
            return {"kind": kind, "value": value}, None
        if "/" in value:
            return None, "spec/plan pointer must be a name, not a path"
        return None, "spec/plan pointer is not a valid name"
    line = _TRAILING_PUNCT_RE.sub("", line)
    m = _ISSUE_RE.match(line)
    if m:
        return {"kind": "issue", "value": m.group(1)}, None
    m = MARKER_RE.search(line)
    if m and not line[: m.start()].strip() and not line[m.end() :].strip():
        return {"kind": "ledger", "value": m.group(1)}, None
    m = FOLLOWUP_MARKER_RE.match(line)
    if m:
        return {"kind": "follow_up", "value": m.group(1)}, None
    return None


def _source_pointer(lines: list[str]) -> tuple[dict | None, str | None]:
    """First source-pointer line wins; returns (source, problem)."""
    for raw in lines:
        result = _match_pointer(raw)
        if result is not None:
            return result
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

    readable_body = _readable_body()
    if readable_body is None:
        problems.append("cannot load readable_body from check_cc_pin_receipts.py")
        return result

    lines = readable_body(body.replace("\r\n", "\n").replace("\r", "\n")).split("\n")

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
