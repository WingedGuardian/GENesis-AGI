#!/usr/bin/env python3
"""PreToolUse hook: block worktree removal to protect active sessions.

Two modes:
  1. Bash matcher (default) — intercepts `git worktree remove` commands.
  2. ExitWorktree matcher (--exit-worktree) — intercepts ExitWorktree tool
     with action "remove".

In both modes:
  - If another process has its CWD inside the target worktree → hard block
    with PID list (cross-session safety).
  - If the current session's CWD IS the target → hard block (self-brick
    prevention).
  - If no conflicts → still block. Worktrees are never removed directly.
    The lifecycle manager (scripts/worktree_lifecycle.py) handles cleanup
    via a trash bin with 7-day recovery.

Incident 1: 2026-05-27 — Session bricked after deleting its own worktree.
Incident 2: 2026-06-09 — Session B deleted worktree still used by Session A,
turning A into a zombie.

Stdlib-only. Fail-open on parse errors.
"""

from __future__ import annotations

import json
import os
import re
import sys

# Matches 'git worktree remove' with optional flags
_WORKTREE_REMOVE = re.compile(
    r"\bgit\s+worktree\s+remove\b"
)


def _extract_worktree_targets(cmd: str) -> list[str]:
    """Extract target paths from git worktree remove commands."""
    targets: list[str] = []
    for match in _WORKTREE_REMOVE.finditer(cmd):
        rest = cmd[match.end():]
        # Skip flags, grab paths
        for token in rest.split():
            token = token.strip("'\"")
            if not token:
                continue
            if token.startswith("-"):
                continue  # skip flags like --force
            # This looks like a path
            targets.append(token)
            break  # git worktree remove takes one path
    return targets


def _resolve_path(path: str) -> str:
    """Resolve a path to its absolute, real form."""
    expanded = os.path.expanduser(path)
    return os.path.realpath(os.path.abspath(expanded))


def _find_processes_in_dir(dir_path: str) -> list[int]:
    """Return PIDs (excluding self and parent) with CWD inside dir_path.

    Scans /proc/[0-9]*/cwd symlinks. Each readlink is a single syscall.
    Processes that vanish between enumeration and readlink are silently
    skipped. ~250ms for ~100 processes on this container.

    Excludes own PID and parent PID. The parent is the CC session that
    fired this hook (genesis-hook uses exec, so the hook's ppid IS the
    CC process). Excluding it avoids false positives from the current
    session's own process chain.
    """
    exclude = {os.getpid(), os.getppid()}
    pids: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids  # fail-open
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in exclude:
            continue
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
            # Normalize — readlink may return path with trailing " (deleted)"
            # for processes whose CWD has been removed, but we care about
            # preventing removal, so at PreToolUse time the dir still exists.
            if cwd == dir_path or cwd.startswith(dir_path + "/"):
                pids.append(pid)
        except (OSError, PermissionError, FileNotFoundError):
            continue
    return pids


def _block_with_pids(target: str, pids: list[int]) -> int:
    """Print cross-session block message and return exit code 2."""
    pid_str = ", ".join(str(p) for p in pids[:10])
    if len(pids) > 10:
        pid_str += f" (+{len(pids) - 10} more)"
    print(
        f"BLOCKED: Cannot remove worktree '{target}' — "
        f"{len(pids)} other process(es) have their working directory inside it.",
        file=sys.stderr,
    )
    print(f"PIDs: {pid_str}", file=sys.stderr)
    print(
        "This would brick those sessions. Wait for them to finish "
        "or use the lifecycle manager.",
        file=sys.stderr,
    )
    return 2


def _block_no_direct_removal(target: str) -> int:
    """Print lifecycle-manager redirect and return exit code 2."""
    print(
        f"BLOCKED: Direct worktree removal is disabled.",
        file=sys.stderr,
    )
    print(
        "Worktrees are managed by the lifecycle manager "
        "(scripts/worktree_lifecycle.py) which uses a trash bin "
        "with 7-day recovery.",
        file=sys.stderr,
    )
    print(
        "The lifecycle manager runs daily via cron. To manually trigger: "
        "python scripts/worktree_lifecycle.py --dry-run",
        file=sys.stderr,
    )
    return 2


def _handle_bash(data: dict) -> int:
    """Handle Bash tool — intercept `git worktree remove` commands."""
    cmd = data.get("command", "")
    if not cmd:
        return 0

    if not _WORKTREE_REMOVE.search(cmd):
        return 0

    # Get the current working directory
    cwd = os.getcwd()
    cwd_real = os.path.realpath(cwd)

    targets = _extract_worktree_targets(cmd)
    for target in targets:
        target_real = _resolve_path(target)

        # Check 1: Self-CWD — would brick this session
        if cwd_real == target_real or cwd_real.startswith(target_real + "/"):
            print(
                f"BLOCKED: Cannot remove worktree '{target}' — "
                f"it is your current working directory.",
                file=sys.stderr,
            )
            print(
                "This would brick the session (every Bash command would "
                "fail with 'Path does not exist').",
                file=sys.stderr,
            )
            return 2

        # Check 2: Cross-session — another process is using it
        other_pids = _find_processes_in_dir(target_real)
        if other_pids:
            return _block_with_pids(target, other_pids)

        # Check 3: No conflict — still block, redirect to lifecycle manager
        return _block_no_direct_removal(target)

    # Fallthrough (no targets parsed) — shouldn't happen, but fail-open
    return 0


def _handle_exit_worktree(data: dict) -> int:
    """Handle ExitWorktree tool — block action "remove"."""
    action = data.get("action", "")
    if action != "remove":
        return 0  # "keep" is always allowed

    # The session is still in the worktree at PreToolUse time
    cwd = os.getcwd()
    cwd_real = os.path.realpath(cwd)

    # Check for other processes in this worktree
    other_pids = _find_processes_in_dir(cwd_real)
    if other_pids:
        return _block_with_pids(cwd, other_pids)

    # No conflict — still block, redirect to "keep"
    print(
        "BLOCKED: Direct worktree removal is disabled.",
        file=sys.stderr,
    )
    print(
        "Use ExitWorktree with action 'keep' instead. "
        "The lifecycle manager (scripts/worktree_lifecycle.py) handles "
        "cleanup automatically via a trash bin with 7-day recovery.",
        file=sys.stderr,
    )
    return 2


def main() -> int:
    try:
        raw = os.environ.get("CLAUDE_TOOL_INPUT", "")
        if not raw:
            return 0

        data = json.loads(raw)

        # Determine mode from CLI args
        if "--exit-worktree" in sys.argv:
            return _handle_exit_worktree(data)
        else:
            return _handle_bash(data)

    except (json.JSONDecodeError, KeyError, OSError):
        pass  # Fail-open

    return 0


if __name__ == "__main__":
    sys.exit(main())
