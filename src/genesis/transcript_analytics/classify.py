"""Structural classification of tool-result failures.

This sorts failure TEXT into coarse classes so they can be counted; it does not
diagnose causes (that is a judgement for whoever reads the rows, with the full
error text kept beside the class). Rules were derived from a full-corpus pass
on 2026-10-04: 6,730 flagged errors, 11 left in ``other`` (plan §5, §14).

Three regimes, because ``is_error`` is absent on ~31% of tool results and absent
does not mean success (plan §14):

* flagged (``is_error`` is True): every rule applies; no match -> ``other``.
* explicit success (``False``): never a failure.
* unflagged (``None``): only STRONG, start-anchored patterns that a
  successful result does not produce, plus explicit denial kinds. A generic
  ``Error``/``Exit code``/``Traceback`` prefix is NOT enough here: a successful
  Read of a log file can start that way.
"""

from __future__ import annotations

import json
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
        ("agent_api_error", r"^Agent terminated early due to an API error:"),
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
        ("mcp_error", r"^(Error calling tool|MCP error)"),
        ("malformed_call", r"^POSSIBLY a malformed tool call"),
        ("stopped", r"^(Not run: |\[Request interrupted)"),
        ("permission_denied", r"^(Permission to use|Permission denied)"),
    )
)

# Subset that may classify a result whose is_error flag is absent.
_STRONG = frozenset({"oversize_result", "tool_use_error", "mcp_error", "agent_api_error"})
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
_SHELL_SYNTAX = frozenset("'\"\\`;&|<>()")
_SCRIPT_EXPANSION = frozenset("$*?{}[]")


# GNU env options that take a value: as the next token, attached to a short
# option (``-uNAME``) or after ``=`` on a long one (``--unset=NAME``).
_ENV_VALUE_SHORT = frozenset("uC")
_ENV_VALUE_LONG = ("--unset", "--chdir")
_ENV_SPLIT_LONG = "--split-string"
_QUOTES = "'\""


def _strip_quotes(tokens: list[str]) -> list[str]:
    return [tok.strip(_QUOTES) for tok in tokens if tok.strip(_QUOTES)]


def _skip_env_options(tokens: list[str]) -> list[str]:
    """The tokens after ``env``'s own options (``tokens[0]`` is ``env``).

    ``-S``/``--split-string`` carries the command itself, so its value is kept
    as the following tokens (with the quoting that grouped it removed) and
    option parsing continues inside it, as env does.
    """
    rest = tokens[1:]
    while rest:
        tok = rest[0]
        if tok == "--":
            return rest[1:]
        if not tok.startswith("-"):
            return rest
        if tok == "-":  # same as -i
            rest = rest[1:]
            continue
        if tok.startswith("--"):
            name, eq, value = tok.partition("=")
            if name == _ENV_SPLIT_LONG:
                split = [value] if eq else []
                rest = _strip_quotes(split + rest[1:])
            elif name in _ENV_VALUE_LONG and not eq:
                rest = rest[2:]
            else:
                rest = rest[1:]
            continue
        # A cluster of short options, e.g. ``-iu NAME`` or ``-S'cmd arg'``.
        rest = rest[1:]
        for i, ch in enumerate(tok[1:], start=1):
            attached = tok[i + 1 :]
            if ch == "S":
                rest = _strip_quotes(([attached] if attached else []) + rest)
                break
            if ch in _ENV_VALUE_SHORT:
                if not attached:
                    rest = rest[1:]
                break
    return rest


def _hook_script(command: str) -> str:
    """Literal command target or option-free interpreter script, never an option.

    Leading ``NAME=value`` assignments and ``env`` with its options are
    skipped: the first token of ``GH_TOKEN=… guard.sh`` is a credential, and
    in ``env -u GH_TOKEN guard.sh`` it is an option, not the script (review
    SF-1). Unsupported interpreter options and ambiguous shell syntax are unknown;
    the separately retained command remains available for analysis.
    """
    tokens = command.split()
    while tokens:
        if tokens[0].rsplit("/", 1)[-1] == "env":
            tokens = _skip_env_options(tokens)
        elif _ENV_ASSIGN.match(tokens[0]):
            tokens = tokens[1:]
        else:
            break
    if not tokens:
        return "<empty>"
    program = tokens[0].rsplit("/", 1)[-1]
    if len(tokens) > 1 and tokens[1] == "-c" and program in ("bash", "sh"):
        return "<inline>"
    operands = iter(tokens[1:] if program in _INTERPRETERS else tokens)
    script = next(operands, "")
    # Dispatch only a command target, never a genesis-hook mentioned in arguments.
    if script.rsplit("/", 1)[-1] == "genesis-hook":
        script = next(operands, "")
    if not script or script.startswith("-"):
        return "<unknown>"
    if not _SHELL_SYNTAX.isdisjoint(" ".join(tokens)):
        return "<unknown>"
    if not _SCRIPT_EXPANSION.isdisjoint(script):
        return "<unknown>"
    return script.rsplit("/", 1)[-1] or "<unknown>"


def _is_error_value(value) -> bool:
    """A string error, or a JSON-RPC error object (integer ``code``, string ``message``)."""
    if isinstance(value, str):
        return True
    return (
        isinstance(value, dict)
        and isinstance(value.get("code"), int)
        and not isinstance(value.get("code"), bool)
        and isinstance(value.get("message"), str)
    )


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
    try:
        error_object = json.loads(full)
    except (ValueError, RecursionError):
        error_object = None
    structural_error = isinstance(error_object, dict) and _is_error_value(error_object.get("error"))
    if is_error is True:
        if is_hook:
            return "hook_block", "flag"
        if denial_kind == "user-rejected":
            return "user_rejected", "flag"
        if structural_error:
            return "mcp_error", "flag"
        for name, rx in _FLAGGED_RULES:
            if rx.search(head):
                return name, "flag"
        if denial_kind:
            return f"denied_{denial_kind}", "flag"
        return "other", "flag"
    if is_error is False:
        return None, None
    if structural_error:
        return "mcp_error", "text"
    # Absent flag: strong evidence only.
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
