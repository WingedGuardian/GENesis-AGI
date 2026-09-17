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

import contextlib
import os
import sys

# Self-locate so hook_input/shell_parse resolve whether run as a script or imported.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from hook_input import read_payload, tool_input  # noqa: E402
except Exception:  # noqa: BLE001 — an unimportable hook_input must BLOCK, not vanish.
    if __name__ != "__main__":
        raise
    # NOTHING TO FALL BACK ON: hook_input is the module that would recover us, so this
    # guard refuses outright. An unguarded import exits 1, which Claude Code reads as
    # NON-BLOCKING.
    #
    # Note the asymmetry with this guard's shell_parse import, which is deliberately
    # left bare: there, a degraded matcher would have to key on the pipe character —
    # 70.30% of 74,282 real commands — which would make a broken tree unrepairable
    # rather than safe. Here there is no matcher and no choice: the module that would
    # read the payload is the one that failed.
    #
    # The exception is not rendered (even __str__ can raise) and the exit uses
    # os._exit, because sys.exit lets the interpreter retry a failed stream flush
    # during shutdown and replace the status with 120 — which is not 2.
    try:
        sys.stderr.write(
            "GUARD DEGRADED (background_pipe_guard): shared hook_input could not be "
            "imported; BLOCKING until the hook tree is repaired.\n"
        )
        sys.stderr.flush()
    except BaseException:  # noqa: BLE001 — diagnostics cannot change fail direction.
        pass
    os._exit(2)
from shell_parse import has_top_level_pipe  # noqa: E402

try:  # noqa: E402
    import discarded_write
except Exception:  # noqa: BLE001 — GUARDED: an unguarded import failure would abort
    # module load → exit 1 → CC reads non-2 as NON-blocking → the command RUNS.
    discarded_write = None  # type: ignore[assignment]


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
    if discarded_write is not None:
        with contextlib.suppress(Exception):  # not run_guard-wrapped: a raise here exits 1 = NON-blocking
            discarded_write.remember(cmd)
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
        if discarded_write is not None:
            with contextlib.suppress(Exception):  # not run_guard-wrapped: a raise here exits 1 = NON-blocking
                discarded_write.warn()
        sys.exit(2)


if __name__ == "__main__":
    main()
