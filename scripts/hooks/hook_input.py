#!/usr/bin/env python3
"""Shared CC hook input parsing — the ONE place that knows the payload contract.

Why this exists: Claude Code delivers each hook's payload as **JSON on stdin**
(the documented contract). An earlier generation of Genesis hooks instead read a
``CLAUDE_TOOL_INPUT`` environment variable that held *just the tool-input dict*.
Current Claude Code (2.1.x) does not set that variable, so every hook that read
it saw an empty value and silently fell open — the CRITICAL-path guard, the
push/merge gate, the destructive-``rm`` guard, and a dozen others all became
no-ops (verified live 2026-07-23). See docs/reference/cc-compatibility.md.

The two payload shapes this bridges:

* NEW (stdin, full payload)::

      {"hook_event_name": "PreToolUse", "tool_name": "Bash",
       "tool_input": {"command": "..."}, "tool_response": {...}, ...}

* LEGACY (``CLAUDE_TOOL_INPUT`` env, tool-input dict only)::

      {"command": "..."}      # or {"file_path": "..."}, {"url": "..."}, ...

``tool_input()`` returns the ``tool_input`` sub-dict for the new shape and falls
back to the payload itself for the legacy shape, so a single ``field(p, "command")``
call works under both — no hook needs to know which contract produced the data.

Stdlib-only and fail-open by contract: any parse/read failure yields an empty
dict, never an exception. Hooks must never crash CC.
"""

from __future__ import annotations

import json
import os
import re
import sys

_LEGACY_INPUT_ENV = "CLAUDE_TOOL_INPUT"
_LEGACY_RESULT_ENV = "CLAUDE_TOOL_USE_RESULT"


def _loads(raw: str) -> dict:
    """Parse a JSON object, returning {} on any failure or non-object."""
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def read_payload() -> dict:
    """Return the full CC hook payload as a dict.

    Reads JSON from stdin (the current contract). If stdin is empty — e.g. a
    settings.json wrapper that piped an empty legacy env var, or an older CC —
    falls back to the ``CLAUDE_TOOL_INPUT`` environment variable so the hook
    still works on both. Returns ``{}`` on any failure (fail-open).
    """
    raw = ""
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        raw = ""
    if raw and raw.strip():
        payload = _loads(raw)
        if payload:
            return payload
        # Non-empty but not a JSON object — a genuine malformation (never
        # normal empty stdin). Surface it so a silent fail-open can't hide a
        # broken payload contract, then fall through to the legacy fallback.
        print("WARNING: hook payload on stdin was not a JSON object", file=sys.stderr)
    # Empty or unparseable stdin: the legacy env var held the tool-input dict itself.
    return _loads(os.environ.get(_LEGACY_INPUT_ENV, ""))


def tool_input(payload: dict) -> dict:
    """The ``tool_input`` sub-dict.

    New-shape payloads nest it under ``tool_input``; legacy env-var payloads ARE
    the tool-input dict, so fall back to the payload itself when the key is
    absent. Always returns a dict.
    """
    if not isinstance(payload, dict):
        return {}
    ti = payload.get("tool_input")
    if isinstance(ti, dict):
        return ti
    return payload


def field(payload: dict, name: str, default: str = "") -> str:
    """Extract a single tool-input field (``command``, ``file_path``, ``url``…).

    Returns ``default`` when absent or non-string, so callers can treat the
    result as a plain string without isinstance juggling.
    """
    value = tool_input(payload).get(name, default)
    return value if isinstance(value, str) else default


def tool_response(payload: dict) -> dict:
    """The tool result for PostToolUse hooks.

    New shape carries it as ``tool_response``; the legacy contract used the
    ``CLAUDE_TOOL_USE_RESULT`` env var. Returns {} when unavailable.
    """
    if isinstance(payload, dict):
        resp = payload.get("tool_response")
        if isinstance(resp, dict):
            return resp
    return _loads(os.environ.get(_LEGACY_RESULT_ENV, ""))


def session_id(payload: dict, default: str = "unknown") -> str:
    """The CC session id.

    New shape carries it as a top-level ``session_id``; the legacy contract
    used the ``CLAUDE_SESSION_ID`` env var. Falls back to ``default`` so a
    per-session sentinel never silently collapses to one global key.
    """
    if isinstance(payload, dict):
        sid = payload.get("session_id")
        if isinstance(sid, str) and sid:
            return sid
    env_sid = os.environ.get("CLAUDE_SESSION_ID", "")
    return env_sid or default


def run_guard(main_fn, name: str) -> None:
    """Run a fail-closed guard's ``main()``, turning an UNEXPECTED crash into a
    BLOCK (exit 2) instead of the exit 1 that CC treats as a *non-blocking* error.

    Why: CC's PreToolUse contract is "exit 2 = block; ANY other code = non-blocking
    error → the tool RUNS". So an uncaught exception in a guard (exit 1) is a silent
    FAIL-OPEN — the destructive command it was meant to stop executes anyway. For the
    handful of irreversible-action guards (recursive rm, protected-path delete,
    worktree removal, push/merge, CRITICAL-path Write/Edit) that is the wrong
    direction; they must fail CLOSED.

    ``main_fn`` returns the intended exit code (0 allow / 2 block). Any ``Exception``
    it raises is caught here, logged loudly (so a guard bug is visible, per "never
    hide broken things"), and converted to exit 2. ``SystemExit``/``KeyboardInterrupt``
    (``BaseException``, not ``Exception``) propagate untouched. This only catches what
    the guard did NOT handle — a guard's OWN documented inner fail-opens (parse-
    ambiguity paths that deliberately ``return 0``) are unaffected.

    Only wire this for guards where fail-closed is correct — never for advisory or
    convenience guards, which must stay fail-open so a bug never blocks legit work.
    """
    try:
        code = main_fn()
    except Exception as exc:  # noqa: BLE001 — fail CLOSED on ANY unexpected error
        print(
            f"GUARD ERROR ({name}): failing CLOSED (blocking) — {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(code if isinstance(code, int) else 0)


_BRACE_RE = re.compile(r"\{([^{}]*,[^{}]*)\}")
_BRACE_MAX = 1024


def brace_expand(token: str) -> list[str]:
    """Expand a bash comma-brace token into the paths it actually produces.

    ``a/{b,c}`` → ``["a/b", "a/c"]``; ``a/{b,}`` → ``["a/b", "a/"]`` (an empty
    option yields the parent). Recurses so multiple/nested groups expand. A
    token with no comma-brace returns ``[token]`` unchanged.

    Why the rm guards need this: bash brace-expands an unquoted target BEFORE
    ``rm`` ever runs, so ``rm -rf ~/genesis/{data,logs}`` deletes
    ``~/genesis/data`` — but a guard that inspects the single opaque token
    ``~/genesis/{data,logs}`` sees no glob, no exact/ancestor match, and a
    depth of 4, so it waves the command through. Expanding first makes each real
    target visible to the depth/protected checks.

    Numeric ranges (``{1..3}``) are deliberately NOT expanded — a rare shape for
    a deletion target. An absurd combinatorial blow-up (a brace bomb) raises
    ValueError; callers are run_guard-wrapped, so that fails CLOSED (blocks).
    Quote/expansion nuances are handled by shlex upstream, matching the sibling
    guards' "shlex is the sole authority on quoting" posture.
    """
    result: list[str] = []
    stack = [token]
    while stack:
        t = stack.pop()
        m = _BRACE_RE.search(t)
        if not m:
            result.append(t)
            if len(result) > _BRACE_MAX:
                raise ValueError("brace expansion too large — refusing (fail closed)")
            continue
        pre, post = t[: m.start()], t[m.end() :]
        opts = m.group(1).split(",")
        if len(stack) + len(opts) > _BRACE_MAX:
            raise ValueError("brace expansion too large — refusing (fail closed)")
        for opt in opts:
            stack.append(pre + opt + post)
    return result


def strip_quoted(command: str) -> str:
    """Return ``command`` with quoted regions removed.

    Removes double-quoted (``"…"``, honoring backslash escapes) and
    single-quoted (``'…'``, literal per POSIX) spans, so a caller can test for
    shell syntax that only counts OUTSIDE quotes — a trailing ``# comment``, an
    operator. A token found only inside the removed spans (e.g. inside a
    ``git commit -m "…"`` message, where it would be committed into history) is
    absent from the result, so ``strip_quoted(cmd)`` vs the raw ``cmd`` lets a
    guard distinguish a genuine trailing-comment override from a token buried in
    a commit message.

    Stdlib-only, fail-open: returns the input unchanged on any error.
    """
    if not command:
        return command
    try:
        return re.sub(r"\"(?:\\.|[^\"\\])*\"|'[^']*'", "", command)
    except (re.error, TypeError):
        return command
