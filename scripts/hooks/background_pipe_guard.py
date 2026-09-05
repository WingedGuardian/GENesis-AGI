"""PreToolUse hook: block a run_in_background Bash command that contains a real pipe.

A piped background command's stdout is swallowed by the harness (the pipe's
output never reaches the caller), so `cmd | filter` run in the background yields
empty output — a silent footgun. This blocks ONLY when the command has a genuine
top-level shell PIPE, decided by the canonical quote/redirect-aware parser
(`shell_parse.has_top_level_pipe`), so a `|` inside a quoted jq program, a
`grep -F '|'`, a `||` control operator, or a `>|` redirect no longer false-blocks
(the prior inline `${CMD//||/ } | grep -qF "|"` check over-blocked on all of these).

Convenience guard, not a security gate: fail-open on a malformed payload, and
accept the documented residual that a `|` inside a heredoc body or a `case`
pattern may still over-block (shell_parse does not track those) — the worst case
is a reworked command, never a bypass.
"""

from __future__ import annotations

import os
import sys

try:  # A refusal discards the WHOLE Bash call, not just the piped step.
    from discarded_write import warn as _warn_discarded  # noqa: E402
except Exception:  # noqa: BLE001

    def _warn_discarded(_command=None):
        """No-op stand-in.

        The note is cosmetic, but an UNGUARDED import that failed would abort
        this module's load — and CC reads a non-2 exit as a NON-blocking error,
        so the command this hook exists to refuse would RUN. A missing note must
        never become a missing block.
        """

# Self-locate so hook_input/shell_parse resolve whether run as a script or imported.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import read_payload, tool_input  # noqa: E402
from shell_parse import has_top_level_pipe  # noqa: E402


def _is_background(value: object) -> bool:
    """``run_in_background`` truthiness across the bool / string payload shapes."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return False


def main() -> None:
    payload = read_payload()
    ti = tool_input(payload)
    if not _is_background(ti.get("run_in_background")):
        return
    cmd = ti.get("command")
    if not isinstance(cmd, str) or not cmd:
        return
    if has_top_level_pipe(cmd):
        print(
            "BLOCKED: a run_in_background command with a pipe produces empty output "
            "(the piped stdout is swallowed). Run it without the pipe, run it in the "
            "foreground, or move the pipeline into a script file and background "
            "`bash that_script.sh`.",
            file=sys.stderr,
        )
        # This exit discards the WHOLE call, so a `cp a b && producer | consumer`
        # also loses the copy — and the message above names only the pipe, which
        # reads as "the pipeline was rejected", never "and your earlier write
        # never happened". Every other refusal point emits this; this guard was
        # wired without it, so its refusals were the silent ones.
        _warn_discarded(cmd)
        sys.exit(2)


if __name__ == "__main__":
    main()
