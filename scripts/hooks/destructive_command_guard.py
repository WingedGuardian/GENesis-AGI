#!/usr/bin/env python3
"""PreToolUse hook (Bash): block rm -rf on broad paths.

Catches rm with recursive+force flags targeting shallow paths that
could wipe important directories. A path must be at least 4 components
deep (e.g., /home/user/project/some_dir) to pass.

Blocks:  rm -rf /  |  rm -rf ~  |  rm -rf .  |  rm -rf ~/project
Allows:  rm -rf /home/user/project/.claude/worktrees/old-branch

Depth threshold is 4 components (e.g., /home/user/project/subdir).

Stdlib-only. Fail-open on parse errors.
"""

from __future__ import annotations

import json
import os
import re
import sys

# Matches rm with any combination of -r and -f flags
_RM_RF_PATTERN = re.compile(
    r"\brm\s+"
    r"(?:-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*|-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*)"
    r"\s+"
)

# Dangerous special targets — always block regardless of depth
_ALWAYS_BLOCK = {".", "..", "/"}


def _path_depth(path: str) -> int:
    """Count meaningful path components after expansion."""
    expanded = os.path.expanduser(path)
    expanded = os.path.normpath(expanded)
    # Count non-empty components
    parts = [p for p in expanded.split("/") if p]
    return len(parts)


def _extract_rm_targets(cmd: str) -> list[str]:
    """Extract target paths from rm -rf commands."""
    targets: list[str] = []
    # Find all rm -rf occurrences and grab the paths after flags
    for match in _RM_RF_PATTERN.finditer(cmd):
        rest = cmd[match.end():]
        # Grab paths until a command separator
        for token in re.split(r"[|;&]|\s+\d*[<>]", rest):
            token = token.strip()
            if not token or token.startswith("-"):
                break
            targets.append(token)
    return targets


def main() -> int:
    try:
        raw = os.environ.get("CLAUDE_TOOL_INPUT", "")
        if not raw:
            return 0

        data = json.loads(raw)
        cmd = data.get("command", "")
        if not cmd:
            return 0

        if not _RM_RF_PATTERN.search(cmd):
            return 0

        targets = _extract_rm_targets(cmd)
        for target in targets:
            clean = target.strip("'\"")

            # Always block special targets
            if clean in _ALWAYS_BLOCK:
                print(
                    f"BLOCKED: rm -rf on '{clean}' is not allowed.",
                    file=sys.stderr,
                )
                return 2

            # Block shallow paths (fewer than 3 components)
            depth = _path_depth(clean)
            if depth < 4:
                print(
                    f"BLOCKED: rm -rf on '{clean}' (depth {depth}) is too broad.",
                    file=sys.stderr,
                )
                print(
                    "Target must be at least 4 levels deep. "
                    "If intentional, ask the user to confirm.",
                    file=sys.stderr,
                )
                return 2

    except (json.JSONDecodeError, KeyError):
        pass  # Fail-open

    return 0


if __name__ == "__main__":
    sys.exit(main())
