#!/usr/bin/env python3
"""PreToolUse hook (Bash): block rm/rmdir on protected data directories.

Complements destructive_command_guard.py (depth-based) with an explicit
blocklist of named paths containing irreplaceable data: session transcripts,
encrypted backups, Qdrant snapshots, browser profiles, and the production
database.

Files *inside* protected directories can still be written/modified — only
deletion of the directories themselves (or commands that would remove them
as a side-effect) is blocked.

Stdlib-only. Fail-open on parse errors.
"""

from __future__ import annotations

import json
import os
import re
import sys

# Directories that must never be deleted.  Relative to $HOME.
# Each entry is joined with os.path.expanduser("~") at runtime.
_PROTECTED_RELATIVE = [
    ".claude/projects",       # CC session transcripts (JSONL)
    "backups",                # Encrypted Genesis backups
    "snapshots",              # Qdrant snapshots
    ".genesis/camoufox-profile",  # Camoufox browser profile
    ".genesis/browser-profile",   # Chromium browser profile
    "genesis/data",           # Production database (genesis.db)
]

# Matches rm or rmdir as a word boundary
_RM_PATTERN = re.compile(r"\brm\b|\brmdir\b")


def _build_protected_paths() -> list[str]:
    """Expand relative paths to absolute, including common aliases."""
    home = os.path.expanduser("~")
    paths: list[str] = []
    for rel in _PROTECTED_RELATIVE:
        # Absolute form: /home/ubuntu/.claude/projects
        paths.append(os.path.join(home, rel))
        # Tilde form: ~/.claude/projects
        paths.append(os.path.join("~", rel))
        # $HOME form: $HOME/.claude/projects
        paths.append(os.path.join("$HOME", rel))
    return paths


def main() -> int:
    try:
        raw = os.environ.get("CLAUDE_TOOL_INPUT", "")
        if not raw:
            return 0

        data = json.loads(raw)
        cmd = data.get("command", "")
        if not cmd:
            return 0

        # Fast path: no rm/rmdir in command
        if not _RM_PATTERN.search(cmd):
            return 0

        protected = _build_protected_paths()
        for path in protected:
            if path in cmd:
                print(
                    f"BLOCKED: Cannot delete protected directory: {path}",
                    file=sys.stderr,
                )
                print(
                    "This directory contains irreplaceable data "
                    "(session transcripts, backups, snapshots, or browser profiles).",
                    file=sys.stderr,
                )
                print(
                    "To remove specific files inside it, target them directly.",
                    file=sys.stderr,
                )
                return 2

    except (json.JSONDecodeError, KeyError):
        pass  # Fail-open

    return 0


if __name__ == "__main__":
    sys.exit(main())
