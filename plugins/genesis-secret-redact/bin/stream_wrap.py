#!/usr/bin/env python3
"""PreToolUse hook: route a Monitor or background Bash command through the redacting stream runner.

Those two tools deliver output that Claude Code also keeps in its own records (queued task notifications, the
task output file), which the plugin's mod cannot rewrite after the fact. Redacting at the source covers them.

The rewrite is an ``updatedInput`` with NO ``permissionDecision``: other PreToolUse hooks (the repo's guards)
run beside this one and judge the ORIGINAL command, and nothing is approved that would otherwise be asked
(both measured on Claude Code 2.1.299). It is made ONLY in a session whose ``permission_mode`` is
``bypassPermissions``. Claude Code's own permission check runs on the rewritten command, and the wrapper starts
with ``source``, which it treats as code to evaluate (measured: in ``default`` mode an allow-listed background
command was refused, "'source' evaluates arguments as shell code", and allow and deny rules would be matched
against the wrapper). Commands are left alone in every other mode, when they are already wrapped, and for any
other tool. This hook never reads the secret values: whether there is anything to redact is the filter's question,
asked while the command runs, so a secrets file the command itself creates is covered too. Any failure here
leaves the command unwrapped: a wrap is a privacy measure for an honest session, never a reason to stop its work.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parent
_RUNNER = _BIN / "redact_run.sh"
#: What a command this hook already wrapped starts with. Only the exact wrapped shape (this prefix, one base64
#: word, the quoted root, nothing else) counts as wrapped: a test on the prefix alone would let a command that
#: merely starts with it (`source <runner> x; other`) skip the wrap.
_PREFIX = f"source {shlex.quote(str(_RUNNER))} "


def _is_wrapped(command: str, root: Path) -> bool:
    shape = re.escape(_PREFIX) + r"[A-Za-z0-9+/]+=*" + re.escape(" " + shlex.quote(str(root)))
    return re.fullmatch(shape, command) is not None


def rewrite(payload: dict, root: Path) -> dict | None:
    """The hook output for this payload, or None to leave the call untouched."""
    if payload.get("permission_mode") != "bypassPermissions":
        return None
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip() or _is_wrapped(command, root):
        return None
    if not (tool == "Monitor" or (tool == "Bash" and tool_input.get("run_in_background") is True)):
        return None
    encoded = base64.b64encode(command.encode()).decode()
    wrapped = f"{_PREFIX}{encoded} {shlex.quote(str(root))}"
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": {**tool_input, "command": wrapped},
        }
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        out = rewrite(payload, _BIN.parents[2])
    except Exception as exc:  # noqa: BLE001 - never block a tool call over a privacy wrap
        print(f"secret-redact: stream wrap skipped ({type(exc).__name__})", file=sys.stderr)
        return 0
    if out is not None:
        json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
