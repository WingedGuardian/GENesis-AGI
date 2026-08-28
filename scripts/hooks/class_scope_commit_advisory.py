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


def _targets_a_commit(command: str) -> bool:
    try:
        segments = analyze(command)
    except Exception:  # noqa: BLE001 — advisory, never fail loudly
        return False
    for seg in segments:
        argv = getattr(seg, "argv", None) or []
        if len(argv) >= 2 and argv[0] == "git" and "commit" in argv[1:]:
            return True
    return False


def _git(args: list[str], cwd: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    return out.stdout if out.returncode == 0 else ""


def main() -> int:
    command = field(read_payload(), "command")
    if not command or not _targets_a_commit(command):
        return 0

    repo_root = _git(["rev-parse", "--show-toplevel"], os.getcwd()).strip()
    if not repo_root:
        return 0
    repo = Path(repo_root)

    staged = [
        r for r in _git(
            ["diff", "--cached", "--name-only", "--diff-filter=M"], repo_root
        ).split()
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
        try:
            new_src = (repo / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # A non-UTF-8 .py file must skip only ITSELF. Letting this raise
            # would abandon every remaining staged file too.
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
        print(
            "[class-scope] This change may fix instances while leaving siblings:\n"
            + "\n".join(notes[:_MAX_NOTES])
            + "\n  Not blocking. But enumerate the class first — this is the "
            "pattern the mode-switch gate exists to stop, seen earlier.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    if os.environ.get("GENESIS_CLASS_SCOPE_ADVISORY", "").lower() in {"0", "off", "false"}:
        sys.exit(0)
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — must never block
        sys.exit(0)
