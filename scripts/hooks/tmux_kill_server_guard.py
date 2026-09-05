#!/usr/bin/env python3
"""Advisory: a `tmux kill-server` with no explicit socket binding.

Origin (2026-09-04, measured): a session's scratch-server cleanup ran a
bare ``tmux kill-server``. The inherited ``$TMUX`` environment variable
outranks ``TMUX_TMPDIR`` in tmux's server resolution, so the kill aimed
at a throwaway probe server addressed the MAIN default server instead —
reaping every live CC session on the box at once. The command shape is
indistinguishable from an intentional default-server kill by parsing
alone, which is exactly why this is ADVISORY, never a block: the cost of
a false positive is one extra line of context; the cost of the miss is
the incident above, and the session that runs it also kills itself.

Deliberately narrow: ``kill-session`` is unguarded — the slot launcher
(`cc-slot.sh`) legitimately kills named sessions on the default server,
and session-scoped kills cannot take out the server. Only the
server-wide verb with no ``-S``/``-L`` binding earns the advisory.

Degraded parse → silence: an advisory has no block to fail open from,
and shell_parse's fallback split cannot distinguish a mention from an
execution, so unparseable text gets no advice rather than noise
(contrast with the blocking guards, which fail closed at their callers).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hook_input import read_payload, tool_input  # noqa: E402
from shell_parse import analyze, untokenizable  # noqa: E402

_ADVICE = (
    "ADVISORY: this runs `tmux kill-server` with no explicit socket binding "
    "(-S/-L). tmux resolves the target server from an inherited $TMUX first — "
    "TMUX_TMPDIR does NOT override it, and clearing $TMUX only re-targets the "
    "DEFAULT socket, which is usually the main server too — so inside a tmux "
    "pane this kills the MAIN server and every CC session on it, including "
    "this one. If a scratch or probe server is the target, bind the kill to "
    "ITS socket explicitly: `tmux -S <path> kill-server` / `tmux -L <name> "
    "kill-server`. If the default server really is the target, proceed "
    "knowingly."
)


def _has_socket_binding(argv: list[str]) -> bool:
    """True when argv carries an explicit -S/-L server binding.

    Both options take a value, separate (``-S path``) or glued
    (``-Spath``). Any occurrence binds the whole invocation — tmux server
    options precede the command word, and a stray later occurrence would
    be a tmux usage error, not an unbound kill.
    """
    return any(
        tok in ("-S", "-L") or (len(tok) > 2 and tok.startswith(("-S", "-L")))
        for tok in argv
    )


# tmux global options that CONSUME a value, per the tmux(1) synopsis — a
# closed set, which is the whole design: the guard walks the option prefix
# with this table, takes the first non-option token as the COMMAND WORD, and
# advises only when that word is kill-server with no genuinely-consumed
# -S/-L binding. No raw-text scanning, no env/quoting semantics modeled —
# an earlier draft suppressed on `env -u TMUX` in the raw segment and was
# wrong three ways at once (matched inside comments and quoted values, and
# the "remedy" it honored still kills the default server). Mis-classifying
# a future tmux option here shifts one advisory line, never a verdict that
# authorizes anything.
_VALUE_OPTS = {"-c", "-f", "-L", "-S", "-T"}


def _command_word_and_binding(argv: list[str]) -> tuple[str | None, bool]:
    """(first command word, saw a real -S/-L server binding) for a tmux argv.

    Separate (``-S path``) and glued (``-Spath``) forms both bind; a ``-S``
    consumed as ANOTHER option's value (``tmux -f -S kill-server`` — ``-f``
    takes a file) does not. Glued values of other options (``-f/dev/null``)
    are one token and consume nothing extra."""
    bound = False
    i = 1
    while i < len(argv):
        tok = argv[i]
        if not tok.startswith("-") or tok == "-":
            return tok, bound
        if tok in _VALUE_OPTS:
            if tok in ("-S", "-L"):
                bound = True
            i += 2  # the next token is this option's value, whatever it looks like
            continue
        if len(tok) > 2 and tok[:2] in ("-S", "-L"):
            bound = True
        i += 1
    return None, bound


def _advisory(command: str) -> str | None:
    if untokenizable(command):
        return None
    for seg in analyze(command):
        if seg.exe != "tmux":
            continue
        word, bound = _command_word_and_binding(seg.argv)
        if word == "kill-server" and not bound:
            return _ADVICE
    return None


def main() -> None:
    payload = read_payload()
    ti = tool_input(payload)
    cmd = ti.get("command")
    if not isinstance(cmd, str) or not cmd:
        return
    try:
        note = _advisory(cmd)
    except Exception:
        return  # advisory only: never let a guard bug interfere with a command
    if not note:
        return
    # exit 0 + stderr is "no objection" and never reaches the model —
    # structured stdout is the only advisory channel (pipe_status_guard's
    # documented contract).
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": note,
                }
            }
        )
    )


if __name__ == "__main__":
    # advisory: any failure is silence, never interference
    with contextlib.suppress(Exception):
        main()
