#!/usr/bin/env python3
"""PreToolUse hook (Bash): block rm with recursive+force on broad paths.

Catches rm with recursive+force flags targeting shallow paths that
could wipe important directories. A path must be at least 4 components
deep (e.g., /home/user/project/some_dir) to pass.

Blocks:  rm -rf /  |  rm -r -f ~  |  rm --recursive --force .  |
         rm -Rf ~/project  |  rm -rf -- /  |  rm -rf deep/path /
Allows:  rm -rf /home/user/project/.claude/worktrees/old-branch

Parsing is token-based (shlex): flags accumulate across tokens, `--`
ends flag parsing, and every operand is depth-checked individually —
the 2026-07-10 P1 triage empirically confirmed the old single-token
regex missed the `-r -f`, `--recursive --force`, `-Rf`, and `-- /`
spellings, and folded multiple operands into one pseudo-path.

Stdlib-only. Unparseable commands fall back to the legacy regex match
(fail-open beyond that — this guard must not block legitimate work).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

from hook_input import field, read_payload

# Legacy single-token pattern — kept as the fallback when shlex cannot
# tokenize the command (unmatched quotes etc.).
_RM_RF_PATTERN = re.compile(
    r"\brm\s+"
    r"(?:-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*|-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*)"
    r"\s+"
)

# Dangerous special targets — always block regardless of depth
_ALWAYS_BLOCK = {".", "..", "/", "~", "*"}

# Command separators that start a new simple command within one Bash
# string. Tokens matching these end an rm invocation's argument list.
_SEPARATORS = {"|", "||", "&&", ";", "&", "\n"}


def _check_target(target: str) -> str | None:
    """Reason string if *target* is too broad to rm recursively."""
    clean = target.strip("'\"")
    if clean in _ALWAYS_BLOCK:
        return f"rm -rf on '{clean}' is not allowed."
    expanded = os.path.normpath(os.path.expanduser(clean))
    parts = [p for p in expanded.split("/") if p]
    # A surviving '..' means the path traverses upward from a base the
    # hook cannot know (its cwd need not match the Bash invocation's).
    # normpath collapses interior '..' only against an absolute/~-anchored
    # path; a leading '..' on a RELATIVE path survives and is counted as
    # depth — so `rm -rf ../../../etc` reports depth 4 yet resolves to
    # /etc from filesystem root. Refuse rather than guess. (2026-07-10
    # review: this was a live bypass.)
    if ".." in parts:
        return f"rm -rf on '{clean}' traverses upward ('..') — refusing."
    if len(parts) < 4:
        return f"rm -rf on '{clean}' (depth {len(parts)}) is too broad."
    return None


def _rm_violations(cmd: str) -> list[str] | None:
    """Reasons to block, or None when the command cannot be tokenized."""
    # Make separators standalone tokens so `rm -rf x; other` parses.
    spaced = re.sub(r"(\|\||&&|[|;&\n])", r" \1 ", cmd)
    try:
        tokens = shlex.split(spaced)
    except ValueError:
        return None  # unparseable — caller falls back to the legacy regex

    violations: list[str] = []
    i = 0
    while i < len(tokens):
        if os.path.basename(tokens[i]) != "rm":
            i += 1
            continue

        # Parse this rm invocation until the next command separator.
        recursive = force = False
        operands: list[str] = []
        flags_done = False
        i += 1
        while i < len(tokens) and tokens[i] not in _SEPARATORS:
            arg = tokens[i]
            i += 1
            if not flags_done and arg == "--":
                flags_done = True
                continue
            if not flags_done and arg.startswith("--"):
                # GNU getopt_long accepts unambiguous prefix abbreviations,
                # so `--rec`/`--f` are valid spellings of --recursive/
                # --force. Match by prefix (len>=3 skips the bare '--').
                # Over-matching only over-blocks, which is safe here.
                # (2026-07-10 review: `rm --rec --f /` was a live bypass.)
                if len(arg) >= 3 and "--recursive".startswith(arg):
                    recursive = True
                elif len(arg) >= 3 and "--force".startswith(arg):
                    force = True
                continue  # other long flags carry no target
            if not flags_done and arg.startswith("-") and len(arg) > 1:
                if any(c in "rR" for c in arg[1:]):
                    recursive = True
                if "f" in arg[1:]:
                    force = True
                continue
            operands.append(arg)

        if recursive and force:
            for operand in operands:
                reason = _check_target(operand)
                if reason:
                    violations.append(reason)
    return violations


def main() -> int:
    try:
        cmd = field(read_payload(), "command")
        if not cmd or "rm" not in cmd:
            return 0

        violations = _rm_violations(cmd)
        if violations is None:
            # Tokenizer failed — the legacy regex still catches the
            # common spelling; beyond that we fail open by design.
            if not _RM_RF_PATTERN.search(cmd):
                return 0
            violations = [
                "recursive+force rm inside an unparseable command — blocked conservatively."
            ]

        if violations:
            for reason in violations:
                print(f"BLOCKED: {reason}", file=sys.stderr)
            print(
                "Recursive+force rm targets must be at least 4 levels deep. "
                "If intentional, ask the user to confirm.",
                file=sys.stderr,
            )
            return 2

    except (json.JSONDecodeError, KeyError):
        pass  # Fail-open

    return 0


if __name__ == "__main__":
    sys.exit(main())
