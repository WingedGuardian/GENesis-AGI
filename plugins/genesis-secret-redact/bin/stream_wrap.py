#!/usr/bin/env python3
"""PreToolUse hook: route a Monitor or background Bash command through the redacting stream runner.

Those two tools deliver output that Claude Code also keeps in its own records (queued task notifications, the
task output file), which the plugin's mod cannot rewrite after the fact. Redacting at the source covers them.

The rewrite is an ``updatedInput`` with NO ``permissionDecision``: other PreToolUse hooks (the repo's guards)
run beside this one and judge the ORIGINAL command, and nothing is approved that would otherwise be asked
(both measured on Claude Code 2.1.299). Commands are left alone when there is nothing to redact, when they are
already wrapped, and for any other tool. Any failure here leaves the command unwrapped: a wrap is a privacy
measure for an honest session, never a reason to stop its work.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import shlex
import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parent
_RUNNER = _BIN / "redact_run.sh"
#: What a command this hook already wrapped starts with, exactly. A substring test would let any command that
#: merely mentions the runner (a comment, an echo) skip the wrap.
_PREFIX = f"source {shlex.quote(str(_RUNNER))} "


def _has_needles(root: Path) -> bool:
    spec = importlib.util.spec_from_file_location("_redact_known_values", _BIN / "known_values.py")
    if spec is None or spec.loader is None:
        return False
    known = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(known)
    return bool(known.load(known.repo_root(str(root)))["needles"])


def rewrite(payload: dict, root: Path) -> dict | None:
    """The hook output for this payload, or None to leave the call untouched."""
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip() or command.startswith(_PREFIX):
        return None
    if not (tool == "Monitor" or (tool == "Bash" and tool_input.get("run_in_background") is True)):
        return None
    if not _has_needles(root):
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
