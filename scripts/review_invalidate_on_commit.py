#!/usr/bin/env python3
"""PostToolUse hook (Bash): invalidate review marker after successful git commit.

Every commit must be preceded by a fresh review. This hook clears the marker
after any successful git commit, so the next commit will require review again.

Reads the CC PostToolUse payload from stdin (via hook_input) — the git
command from tool_input, the outcome from tool_response.

Exit codes:
  0 = always (PostToolUse hooks cannot block)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# The shared hook-input helper lives in scripts/hooks/; this script runs from
# scripts/ (a different sys.path[0]), so add the hooks dir before importing it.
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
from hook_input import field, read_payload, tool_response  # noqa: E402

_COMMIT_PATTERN = re.compile(r"\bgit\s+commit\b")
_STATE_FILE = Path.home() / ".genesis" / "review_state.json"


def main() -> None:
    payload = read_payload()

    # Check if the tool input contained a git commit command
    command = field(payload, "command")
    if not _COMMIT_PATTERN.search(command):
        sys.exit(0)

    # Only invalidate on successful commits (exit code 0)
    try:
        result = tool_response(payload)
        # CC wraps Bash results — check for error indicators
        _stdout = result.get("stdout", "")  # noqa: F841
        _stderr = result.get("stderr", "")  # noqa: F841
        # A successful git commit prints to stdout with the branch and hash
        # A failed commit (e.g. pre-commit hook) has non-zero exit
        if "error" in result and result["error"]:
            sys.exit(0)  # Commit failed, don't invalidate
    except (json.JSONDecodeError, AttributeError):
        pass  # Can't parse result — be conservative, invalidate anyway

    # Clear the review marker
    import contextlib

    with contextlib.suppress(OSError):
        _STATE_FILE.unlink(missing_ok=True)

    sys.exit(0)


if __name__ == "__main__":
    main()
