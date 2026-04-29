"""Worktree management for code task isolation.

Extracted from engine.py to keep file size under 600 LOC.
Provides async functions for creating and cleaning up git worktrees
used by CODE-type task steps (Amendment #7).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _branch_from_wt_path(wt_path: Path) -> str | None:
    """Derive branch name from worktree path: task-XXXX → task/XXXX."""
    name = wt_path.name
    if name.startswith("task-"):
        return f"task/{name[5:]}"
    return None


async def _delete_branch(branch: str, repo_root: Path) -> None:
    """Delete a local git branch. Logs result, never raises."""
    proc = await asyncio.create_subprocess_exec(
        "git", "branch", "-D", branch,
        cwd=str(repo_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode == 0:
        logger.info("Deleted branch %s", branch)
    else:
        # Branch may not exist — that's fine
        logger.debug(
            "Branch %s not deleted (may not exist): %s",
            branch, stderr.decode(errors="replace").strip(),
        )


async def _prune_worktrees(repo_root: Path) -> None:
    """Run git worktree prune to clean orphaned entries."""
    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "prune",
        cwd=str(repo_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create_worktree(
    task_id: str,
    repo_root: Path,
    worktree_base: Path,
) -> Path:
    """Create a git worktree for code task isolation.

    Cleans up stale state from previous runs before creating.
    Returns the worktree path on success.
    Raises RuntimeError if worktree creation fails.
    """
    short_id = task_id[:8]
    branch = f"task/{short_id}"
    wt_path = worktree_base / f"task-{short_id}"

    # Clean up stale state from previous runs of this task
    if wt_path.exists():
        logger.info("Stale worktree dir %s exists, cleaning up", wt_path)
        await cleanup_worktree(wt_path, repo_root)
    else:
        # No dir but branch might linger from a prior crash
        await _prune_worktrees(repo_root)
        await _delete_branch(branch, repo_root)

    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "add", "-b", branch, str(wt_path),
        cwd=str(repo_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        logger.error("Failed to create worktree: %s", err)
        raise RuntimeError(f"Worktree creation failed: {err}")

    logger.info("Created worktree at %s (branch %s)", wt_path, branch)
    return wt_path


async def cleanup_worktree(
    wt_path: Path,
    repo_root: Path,
) -> None:
    """Remove a worktree and its associated branch.

    NO --force per CLAUDE.md worktree rules.
    """
    branch = _branch_from_wt_path(wt_path)

    if not wt_path.exists():
        # Worktree dir gone but branch might linger
        if branch:
            await _delete_branch(branch, repo_root)
        return

    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "remove", str(wt_path),
        cwd=str(repo_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.warning(
            "Worktree cleanup failed for %s: %s",
            wt_path, stderr.decode(errors="replace"),
        )
    else:
        logger.info("Cleaned up worktree at %s", wt_path)

    # Delete the branch (even if worktree removal failed, try anyway)
    if branch:
        await _delete_branch(branch, repo_root)
