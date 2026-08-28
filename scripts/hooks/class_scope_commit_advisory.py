#!/usr/bin/env python3
"""PreToolUse advisory on a commit command: consolidated class-scope divergence.

Deliberately a SEPARATE hook rather than a few lines inside
``review_enforcement_commit.py``. That file is the security gate covering
direct-to-main, ``--no-verify``, unreviewed changes and the escalation cap, and
it runs under one shared hook timeout. An advisory sharing that budget can
starve the gate on exactly the large changesets where the gate matters most, and
a timed-out PreToolUse hook does not deny — it lets the call through. Separate
hooks get separate budgets, so the worst case here is that this advisory is
killed and says nothing, which is what an advisory should do when out of time.

Never blocks: always exits 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from class_scope_scan import (  # noqa: E402
    find_orphaned_literals,
    find_unrevisited_uses,
)
from hook_input import field, read_payload  # noqa: E402
from shell_parse import analyze  # noqa: E402

# Well inside this hook's own registered timeout, and inside what a person will
# wait for before a commit lands.
_BUDGET_SECONDS = 2.0
_MAX_FILES = 12
_MAX_NOTES = 8


def _commit_target_cwd(command: str) -> str | None:
    """The directory the commit will actually run in, or None if not a commit.

    Repository discovery must follow the COMMAND, not the hook's own cwd: a
    session committing another worktree with `git -C <path> commit` would
    otherwise have its staged changes read from the wrong checkout, and the
    advisory would say nothing about the commit that is about to happen.
    """
    try:
        segments = analyze(command)
    except Exception:  # noqa: BLE001 — advisory, never fail loudly
        return None
    for seg in segments:
        argv = getattr(seg, "argv", None) or []
        if not argv or argv[0] != "git":
            continue
        # Walk git's own options, capturing -C, to find the SUBCOMMAND — so
        # `git tag commit` (a ref merely NAMED commit) does not fire.
        target = getattr(seg, "cwd", None) or os.getcwd()
        i = 1
        while i < len(argv) and argv[i].startswith("-"):
            if argv[i] == "-C" and i + 1 < len(argv):
                candidate = argv[i + 1]
                target = (
                    candidate if os.path.isabs(candidate)
                    else os.path.join(target, candidate)
                )
                i += 2
            elif argv[i] in {"-c", "--git-dir", "--work-tree"}:
                i += 2
            else:
                i += 1
        if i < len(argv) and argv[i] == "commit":
            return target if os.path.isdir(target) else None
    return None


def _git(args: list[str], cwd: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5
        )
    except (subprocess.SubprocessError, OSError, ValueError):
        return ""
    return out.stdout if out.returncode == 0 else ""


def main() -> int:
    command = field(read_payload(), "command")
    if not command:
        return 0
    target_cwd = _commit_target_cwd(command)
    if target_cwd is None:
        return 0

    repo_root = _git(["rev-parse", "--show-toplevel"], target_cwd).strip()
    if not repo_root:
        return 0
    repo = Path(repo_root)

    staged = [
        r for r in _git(
            # M and R: a renamed-AND-edited file has status R, and excluding it
            # drops exactly the kind of change most likely to leave siblings.
            ["diff", "--cached", "--name-only", "--diff-filter=MR", "-z"],
            repo_root,
        ).split("\0")
        # -z, split on NUL: whitespace-splitting turns "my file.py" into two
        # bogus paths and the real file is silently skipped, and without -z a
        # non-ASCII path comes back quoted and fails the same way.
        if r.endswith(".py")
    ][:_MAX_FILES]

    notes: list[str] = []
    # One budget shared across ALL files: cost is files x literals x repo size,
    # so a per-file budget would still multiply.
    per_file = _BUDGET_SECONDS / max(len(staged), 1)
    for rel in staged:
        old_src = _git(["show", f"HEAD:{rel}"], repo_root)
        if not old_src:
            continue
        # The INDEX blob, not the worktree. `git add -p` or an edit made after
        # staging means the two differ, and reviewing the worktree would report
        # on text that is not being committed (and miss divergence that exists
        # only in what is).
        new_src = _git(["show", f":{rel}"], repo_root)
        if not new_src:
            continue
        for f in find_orphaned_literals(
            repo / rel, old_src, new_src, repo, budget_seconds=per_file
        ):
            survivors = ", ".join(str(x.relative_to(repo)) for x in f["survivors"][:3])
            notes.append(
                f"  · {rel}: changed {f['literal'].strip()[:50]!r}, "
                f"same text still in {survivors}"
            )
        for f in find_unrevisited_uses(old_src, new_src):
            notes.append(
                f"  · {rel}: `{f['variable']}` in {f['function']}() now comes from "
                f"{f['now']}() not {f['was']}(); uses untouched here at "
                f"lines {f['unrevisited']}"
            )

    if notes:
        # The documented PreToolUse channel, NOT stderr: stderr from a hook that
        # exits 0 is discarded, so the advisory would be inert. permissionDecision
        # stays "allow" — this never blocks.
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "additionalContext": (
                        "[class-scope] This change may fix instances while "
                        "leaving siblings:\n" + "\n".join(notes[:_MAX_NOTES])
                        + "\n  Not blocking. But enumerate the class first — "
                        "this is the pattern the mode-switch gate exists to "
                        "stop, seen earlier."
                    ),
                }
            },
            sys.stdout,
        )
    return 0


if __name__ == "__main__":
    if os.environ.get("GENESIS_CLASS_SCOPE_ADVISORY", "").lower() in {"0", "off", "false"}:
        sys.exit(0)
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — must never block
        sys.exit(0)
