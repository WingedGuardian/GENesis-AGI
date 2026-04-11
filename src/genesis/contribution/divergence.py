"""Phase 6 contribution — divergence / conflict detection.

Uses `git merge-tree --write-tree` (git 2.38+) to compute a virtual
merge between the user's commit and upstream HEAD without touching
the working tree. If the merge is clean, the contribution proceeds.
If there's a conflict, it's surfaced to the user with an actionable
update instruction. No auto-rebase in MVP — Phase 6.4.

`git merge-tree --write-tree <base> <branch>` writes the result to
the object database and prints the tree SHA on success. On conflict
it prints the tree SHA + conflicted file list (separated by NUL or
newline depending on version) and exits non-zero.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .findings import DivergenceResult

_TIMEOUT = 30  # seconds — git merge-tree on a large repo can take a few seconds
_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")


def _safe_sha(sha: str) -> str:
    """Reject non-SHA strings so they can't be interpreted as git options."""
    if not sha or not _SHA_RE.match(sha):
        raise ValueError(f"invalid SHA passed to divergence check: {sha!r}")
    return sha


def check_divergence(
    user_sha: str,
    upstream_sha: str,
    *,
    repo_path: Path | None = None,
) -> DivergenceResult:
    """Run `git merge-tree` to detect conflicts between user_sha and upstream_sha.

    Args:
        user_sha: The commit the user wants to contribute (their fix).
        upstream_sha: The upstream HEAD they'd PR against.
        repo_path: Repo root to run git in. Defaults to cwd.

    Returns:
        DivergenceResult with `clean=True` if the merge is trivial,
        `clean=False` with conflict_files populated otherwise.
    """
    try:
        user_sha = _safe_sha(user_sha)
        upstream_sha = _safe_sha(upstream_sha)
    except ValueError as e:
        return DivergenceResult(clean=False, message=str(e))

    cwd = str(repo_path) if repo_path else None
    try:
        # --write-tree prints a tree sha on success; on conflict it prints
        # the sha plus conflicted pathnames and exits non-zero.
        # --name-only restricts post-sha output to just the filenames.
        proc = subprocess.run(
            [
                "git",
                "merge-tree",
                "--write-tree",
                "--name-only",
                upstream_sha,  # base
                user_sha,      # ours (the fix being proposed)
            ],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return DivergenceResult(
            clean=False,
            message=(
                f"git merge-tree timed out after {_TIMEOUT}s — repository may be "
                "unusually large or git is stuck. Try again."
            ),
        )
    except FileNotFoundError:
        return DivergenceResult(
            clean=False,
            message="git binary not found on PATH — cannot check divergence.",
        )

    if proc.returncode == 0:
        return DivergenceResult(
            clean=True,
            message="Merge is clean — no conflicts with upstream HEAD.",
        )

    # Non-zero: conflict. Parse conflicted filenames. Format is:
    #   <tree-sha>\n
    #   <file1>\n
    #   <file2>\n
    #   ...
    #   \n
    #   <conflict messages>
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    conflict_files: list[str] = []
    # First line is the tree SHA (40 hex chars). Skip it.
    for i, line in enumerate(lines):
        if i == 0 and len(line) == 40 and all(c in "0123456789abcdef" for c in line):
            continue
        # Subsequent lines up to the first blank/informational line are paths.
        # Conflict messages usually start with "CONFLICT" or contain ":".
        if line.startswith("CONFLICT") or line.startswith("Auto-merging"):
            break
        conflict_files.append(line)

    if not conflict_files:
        # Merge-tree exited non-zero but we couldn't parse filenames.
        # Surface stderr for debugging but still report the conflict.
        err_tail = (proc.stderr or "").strip().splitlines()[-1:] or ["unknown"]
        return DivergenceResult(
            clean=False,
            message=f"git merge-tree reported a conflict: {err_tail[0]}",
        )

    files_str = ", ".join(conflict_files[:5])
    more = "" if len(conflict_files) <= 5 else f" (+{len(conflict_files) - 5} more)"
    return DivergenceResult(
        clean=False,
        conflict_files=conflict_files,
        message=(
            f"Your fix conflicts with upstream HEAD in: {files_str}{more}. "
            "Update your install with `git pull upstream main`, resolve the "
            "conflict locally, re-commit, and try again."
        ),
    )
