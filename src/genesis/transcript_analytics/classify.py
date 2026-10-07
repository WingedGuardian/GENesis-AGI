"""Structural classification of tool-result failures.

This sorts failure TEXT into coarse classes so they can be counted; it does not
diagnose causes (that is a judgement for whoever reads the rows, with the full
error text kept beside the class). Rules were derived from a full-corpus pass
on 2026-10-04: 6,730 flagged errors, 11 left in ``other`` (plan §5, §14).

Two regimes, because ``is_error`` is absent on ~31% of tool results and absent
does not mean success (plan §14):

* flagged (``is_error`` is True): every rule applies; no match -> ``other``.
* unflagged (``None``/``False``): only STRONG, start-anchored patterns that a
  successful result does not produce, plus explicit denial kinds. A generic
  ``Error``/``Exit code``/``Traceback`` prefix is NOT enough here: a successful
  Read of a log file can start that way.
"""

from __future__ import annotations

import re

_HOOK_RE = re.compile(
    r"^(?P<event>PreToolUse|PostToolUse|PermissionRequest|UserPromptSubmit|Stop|SubagentStop)"
    r"(?::(?P<tool>\S+))? hook error: \[(?P<command>.*?)\]:",
    re.S,
)

# (class, pattern) in priority order. Matched against the first 400 chars.
_FLAGGED_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.S))
    for name, pattern in (
        ("user_rejected", r"^The user doesn't want to proceed"),
        ("oversize_result", r"^Error: result \(.*?\) exceeds maximum allowed tokens"),
        ("tool_use_error", r"^<tool_use_error>"),
        ("validation", r"^\d+ validation errors? for "),
        ("exit_nonzero", r"^Exit code \d+"),
        ("enospc", r"ENOSPC"),
        ("fs_error", r"^(EISDIR|ENOENT|File does not exist|Path does not exist)"),
        ("stale_session", r"^This Claude Code session is running st"),
        ("schema_mismatch", r"^Output does not match required schema"),
        ("automode_denied", r"^Permission for this action was denied by the Claude Code auto"),
        ("file_too_large", r"^File content \(.*?\) exceeds maximum"),
        ("timeout", r"timed out"),
        ("parse_refused", r"^This command could not be parsed safely"),
        ("model_unavailable", r"is temporarily unavailable"),
        # A JSON error object counts only with a STRING message: a success can
        # carry "error": null (none observed in the corpus, 2026-10-04).
        ("mcp_error", r'^(Error calling tool|\{"error":\s*"|MCP error)'),
        ("malformed_call", r"^POSSIBLY a malformed tool call"),
        ("stopped", r"^(Not run: |\[Request interrupted)"),
        ("permission_denied", r"^(Permission to use|Permission denied)"),
    )
)

# Subset that may classify a result whose is_error flag is absent or False.
_STRONG = frozenset({"oversize_result", "tool_use_error", "mcp_error"})
_UNFLAGGED_RULES = tuple((n, rx) for n, rx in _FLAGGED_RULES if n in _STRONG)

_HEAD = 400


def parse_hook_block(text: str) -> dict | None:
    """Parse a hook-block result into event, tool, command and script name."""
    m = _HOOK_RE.match(text.strip())
    if not m:
        return None
    command = m.group("command")
    return {
        "hook_event": m.group("event"),
        "hook_tool": m.group("tool"),
        "hook_command": command,
        "hook_script": _hook_script(command),
    }


_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_INTERPRETERS = ("bash", "sh", "python3", "python", "node")


def _hook_script(command: str) -> str:
    """Name of the hook script, never an argument value.

    Leading ``NAME=value`` assignments and ``env`` are skipped: the first token
    of ``GH_TOKEN=… guard.sh`` is a credential, not the script (review SF-1).
    """
    tokens = command.split()
    while tokens and (tokens[0] == "env" or _ENV_ASSIGN.match(tokens[0])):
        tokens = tokens[1:]
    if not tokens:
        return "<empty>"
    if "-c" in tokens[:2] and tokens[0] in ("bash", "sh"):
        return "<inline>"
    for i, tok in enumerate(tokens):
        if tok.endswith("genesis-hook") and i + 1 < len(tokens):
            return tokens[i + 1].rsplit("/", 1)[-1]
    # First token that looks like a script path, skipping an interpreter.
    start = 1 if tokens[0] in _INTERPRETERS and len(tokens) > 1 else 0
    return tokens[start].rsplit("/", 1)[-1]


def classify(
    text: str, *, is_error: bool | None, denial_kind: str | None
) -> tuple[str | None, str | None]:
    """Return (error_class, error_source) or (None, None) for a non-failure.

    ``error_source`` is ``"flag"`` when CC marked the result ``is_error`` and
    ``"text"`` when the failure was detected from the text alone.

    ``text`` must be the tool_result CONTENT block (what the model saw). CC's
    ``toolUseResult`` string is usually ``"Error: " + content`` (5,183 of 6,747
    flagged results, 2026-10-04) and would defeat the start-anchored rules.
    """
    full = (text or "").strip()
    head = full[:_HEAD]
    # The hook prefix is matched on the FULL text: an inline `bash -c` hook puts
    # its closing "]:" past char 1,500 (71 rows, 2026-10-04). The other rules are
    # start-anchored (or near-start) and only need the head window.
    is_hook = _HOOK_RE.match(full) is not None
    if is_error is True:
        if is_hook:
            return "hook_block", "flag"
        if denial_kind == "user-rejected":
            return "user_rejected", "flag"
        for name, rx in _FLAGGED_RULES:
            if rx.search(head):
                return name, "flag"
        if denial_kind:
            return f"denied_{denial_kind}", "flag"
        return "other", "flag"
    # Unflagged: strong evidence only.
    if is_hook:
        return "hook_block", "text"
    if denial_kind:
        return (
            "user_rejected" if denial_kind == "user-rejected" else f"denied_{denial_kind}"
        ), "text"
    for name, rx in _UNFLAGGED_RULES:
        if rx.search(head):
            return name, "text"
    return None, None
