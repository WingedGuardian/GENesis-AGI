#!/usr/bin/env python3
"""PreToolUse hook: worktree safety guard (removal protection + relocation block).

Three modes:
  1. Bash matcher (default) — intercepts `git worktree remove` commands.
  2. ExitWorktree matcher (--exit-worktree) — intercepts ExitWorktree tool
     with action "remove".
  3. EnterWorktree matcher (--enter-worktree) — hard-blocks the EnterWorktree
     tool, which would RELOCATE the session into a worktree and make the
     conversation unfindable via /resume (see _handle_enter_worktree).

Removal modes (1 + 2):
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
Incident 3: 2026-06-29 — EnterWorktree silently relocated a multi-day session
into the `morning-report-nextsteps` worktree; its transcript moved to a
separate Claude Code project slug, so /resume from the main repo no longer
listed it (11 such `wt-*` relocation stubs had accumulated).

Stdlib-only. Fail-open on parse errors.
"""

from __future__ import annotations

import json
import os
import re
import sys

# Self-locate so hook_input resolves whether run as a script or imported (tests).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import read_payload, tool_input  # noqa: E402

# Matches 'git worktree remove' with optional flags
_WORKTREE_REMOVE = re.compile(r"\bgit\s+worktree\s+remove\b")


def _extract_worktree_targets(cmd: str) -> list[str]:
    """Extract target paths from git worktree remove commands."""
    targets: list[str] = []
    for match in _WORKTREE_REMOVE.finditer(cmd):
        rest = cmd[match.end() :]
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
        "This would brick those sessions. Wait for them to finish or use the lifecycle manager.",
        file=sys.stderr,
    )
    return 2


def _block_no_direct_removal(target: str) -> int:
    """Print lifecycle-manager redirect and return exit code 2."""
    print(
        "BLOCKED: Direct worktree removal is disabled.",
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


def _handle_enter_worktree(data: dict) -> int:
    """Handle EnterWorktree tool — hard-block to keep sessions findable.

    EnterWorktree re-roots the live session into a git worktree: the harness
    mints a NEW session id whose transcript is written under a DIFFERENT Claude
    Code project slug (``…<repo>--claude-worktrees-<name>/``), leaving only a
    ``wt-<id>.jsonl`` pointer stub behind in the original project dir. The
    conversation continues seamlessly on screen, but ``/resume`` launched from
    the original directory no longer lists it — the session is, in effect, lost.

    Worktree *isolation* never requires relocating the session, so block
    unconditionally and redirect to non-relocating alternatives. Always returns
    exit code 2 regardless of input (``name`` / ``path`` / empty).
    """
    target = "(auto-named worktree)"
    if isinstance(data, dict):
        target = data.get("name") or data.get("path") or target
    print(
        f"BLOCKED: EnterWorktree is disabled — entering '{target}' would "
        "relocate this session into a worktree and make it unfindable.",
        file=sys.stderr,
    )
    print(
        "Why: the harness re-roots the session and writes its transcript under "
        "a separate '<repo>--claude-worktrees-<name>' project dir, leaving only "
        "a 'wt-<id>.jsonl' stub behind. /resume from the original directory will "
        "no longer list this conversation.",
        file=sys.stderr,
    )
    print("Keep the session findable — do this instead:", file=sys.stderr)
    print(
        "  - Isolated file changes: `git worktree add .claude/worktrees/<name> "
        "-b <scope>/<desc> origin/main`, then edit via the worktree's ABSOLUTE "
        "paths and test with `PYTHONPATH=<worktree>/src pytest <files>`. Your "
        "session stays in the main repo and in /resume.",
        file=sys.stderr,
    )
    print(
        "  - Parallel isolated work: dispatch a subagent (Agent tool, "
        'isolation="worktree") — the child runs in its own worktree; your '
        "session is untouched.",
        file=sys.stderr,
    )
    print(
        "  - If a worktree-ROOTED session is genuinely wanted, the USER should "
        "launch Claude Code from that directory, so it is findable there from "
        "the start.",
        file=sys.stderr,
    )
    return 2


def main() -> int:
    # EnterWorktree is hard-blocked UNCONDITIONALLY: it relocates the session
    # regardless of arguments or input, so block before any parse that could
    # otherwise fail-open (empty/missing/malformed payload) and let
    # the relocation through. Input is parsed only to name the worktree in the
    # message; failure to parse still blocks.
    if "--enter-worktree" in sys.argv:
        return _handle_enter_worktree(tool_input(read_payload()))

    try:
        # Handlers operate on the tool-input dict (command / action fields).
        data = tool_input(read_payload())

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
