"""PreToolUse hook: advise when ``$?`` is read after a pipeline.

``$?`` after ``a | b`` is **b's** exit status, not a's. ``systemd-run --wait … |
tail -2`` followed by ``echo $?`` reports *tail* succeeding while the unit failed
— a confident WRONG answer rather than an error, which is what makes it worth a
hook: the command "works" and the reading is garbage. (Origin 2026-08-27: that
exact shape reported a passing verification for a systemd unit that had failed.)

Suppressed when the command shows the author already knows: ``set -o pipefail``
makes ``$?`` the right thing to read, and ``PIPESTATUS`` IS the remedy this hook
recommends — advising against it would punish the correct fix.

ADVISORY, never blocking: ``$?`` after a pipeline is legitimate under pipefail or
when you genuinely care about the last stage, so a block would cost more than the
trap. Sibling of ``background_pipe_guard.py`` (a backgrounded pipe's stdout is
swallowed) — same family, same shape, same canonical quote/redirect-aware parser
via ``shell_parse.has_top_level_pipe``. No bespoke shell grammar.

DELIBERATELY NOT COVERED — the other half of the pipe family. Every component of
a pipeline runs in a SUBSHELL, so a variable the left-hand side sets does not
survive (``some_func … | sed …`` then reading ``$SOME_STATE`` gets a stale
value). A heuristic for that was built and MEASURED as unusable here: flagging an
upper-case variable a command reads but never assigns fired on 3 of 16 (19%) of
this repo's own documented piped commands — every one an ordinary environment
variable (``$GITHUB_TOKEN``, ``$PGHOST``) — because Claude Code's Bash tool keeps
a PERSISTENT SHELL across tool calls, making "reads a variable it did not assign"
the normal case rather than the suspicious one. It also went silent on the
careful form of the very trap it targeted (``STATE=unknown; func | sed; echo
$STATE``), so it fired on the sloppy version and not the careful one. A guard
that noisy gets muted, which costs more than the trap it was meant to catch.

Accepted residuals for this narrowed check (all over-advise, never under-block,
and all cost exactly one advisory line): a ``$?`` inside single quotes, a ``$?``
appearing BEFORE the pipe, an unrelated ``$?`` on a later line of a multi-line
command, and ``case a|b)`` alternation (inherited from ``has_top_level_pipe``'s
documented quote-model residual).
"""

from __future__ import annotations

import json
import os
import sys

# Self-locate so hook_input/shell_parse resolve whether run as a script or imported.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import read_payload, tool_input  # noqa: E402
from shell_parse import has_top_level_pipe  # noqa: E402

_ADVICE = (
    "ADVISORY: this command pipes and then reads `$?`. After a pipeline `$?` is the "
    "LAST component's exit status (the filter), not the command you care about — so "
    "`prog | tail` can report success while `prog` failed. Read ${PIPESTATUS[0]}, add "
    "`set -o pipefail`, or capture the status before piping. This fails by returning a "
    "confident WRONG value rather than an error, so a passing-looking check can be "
    "meaningless."
)


def _advisory(command: str) -> str | None:
    """The nudge for *command*, or None when there is nothing to say."""
    if "$?" not in command:
        return None
    # The author already handled it: pipefail makes `$?` correct, and PIPESTATUS is
    # this hook's own recommended remedy.
    if "pipefail" in command or "PIPESTATUS" in command:
        return None
    if not has_top_level_pipe(command):
        return None
    return _ADVICE


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
    # PreToolUse structured stdout. exit 0 + stderr counts as "no objection" and is
    # NOT surfaced to the model, which would make this invisible in the exact path
    # it protects (same reason cc-deploy-timeout-guard uses additionalContext).
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
    main()
