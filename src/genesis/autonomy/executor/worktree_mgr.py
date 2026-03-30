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


async def create_worktree(
    task_id: str,
    repo_root: Path,
    worktree_base: Path,
) -> Path:
    """Create a git worktree for code task isolation.

    Returns the worktree path on success.
    Raises RuntimeError if worktree creation fails.
    """
    short_id = task_id[:8]
    branch = f"task/{short_id}"
    wt_path = worktree_base / f"task-{short_id}"

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
    """Remove a worktree. NO --force per CLAUDE.md worktree rules."""
    if not wt_path.exists():
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
