#!/usr/bin/env python3
"""PostToolUse hook (Bash): invalidate review marker after successful git commit.

Every commit must be preceded by a fresh review. This hook clears the marker
after any successful git commit, so the next commit will require review again.

Reads CLAUDE_TOOL_USE_RESULT from environment (set by CC hook framework).

Exit codes:
  0 = always (PostToolUse hooks cannot block)
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_COMMIT_PATTERN = re.compile(r"\bgit\s+commit\b")
_STATE_FILE = Path.home() / ".genesis" / "review_state.json"


def main() -> None:
    result_raw = os.environ.get("CLAUDE_TOOL_USE_RESULT", "")
    if not result_raw:
        sys.exit(0)

    # Check if the tool input contained a git commit command
    tool_input_raw = os.environ.get("CLAUDE_TOOL_INPUT", "")
    if not tool_input_raw:
        sys.exit(0)

    try:
        tool_input = json.loads(tool_input_raw)
    except json.JSONDecodeError:
        sys.exit(0)

    command = tool_input.get("command", "")
    if not _COMMIT_PATTERN.search(command):
        sys.exit(0)

    # Only invalidate on successful commits (exit code 0)
    try:
        result = json.loads(result_raw)
        # CC wraps Bash results — check for error indicators
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        # A successful git commit prints to stdout with the branch and hash
        # A failed commit (e.g. pre-commit hook) has non-zero exit
        if "error" in result and result["error"]:
            sys.exit(0)  # Commit failed, don't invalidate
    except (json.JSONDecodeError, AttributeError):
        pass  # Can't parse result — be conservative, invalidate anyway

    # Clear the review marker
    if _STATE_FILE.exists():
        try:
            _STATE_FILE.unlink()
        except OSError:
            pass

    sys.exit(0)


if __name__ == "__main__":
    main()
