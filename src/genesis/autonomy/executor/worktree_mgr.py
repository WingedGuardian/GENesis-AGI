"""Worktree management for code task isolation.

Extracted from engine.py to keep file size under 600 LOC.
Provides async functions for creating and cleaning up git worktrees
used by CODE-type task steps (Amendment #7).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: A local git read. The only way it outlives this is a stuck lock, and the
#: executor has no external watchdog, so it degrades to "unresolved" (the
#: previous behaviour) rather than wedging task creation. Same bound as the
#: scope gate's own git calls in engine.py.
_GIT_READ_TIMEOUT_S = 60.0


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


@dataclass(frozen=True)
class BaseRef:
    """Where a task branch starts: the remote default branch and its commit.

    ``name`` (``main``) is what the task's PR targets. ``sha`` is the commit
    ``origin/<name>`` pointed at when it was read, and it is what the task
    branch is cut from and what the scope gate compares against. A commit, not
    the ref: the ref can move after the cut (a fetch, or anything run inside the
    task's own worktree, since linked worktrees share refs), and a gate that
    diffs against a ref the gated work can move can be narrowed by it.
    """

    name: str
    sha: str


async def _git_read(repo_root: Path, *args: str) -> tuple[int, str]:
    """Run a read-only git command; (returncode, stripped stdout). A timeout
    reads as a failure (-1), never a hang."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(repo_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_GIT_READ_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("git %s in %s timed out", " ".join(args), repo_root)
        return -1, ""
    return proc.returncode or 0, (out or b"").decode(errors="replace").strip()


async def resolve_base(repo_root: Path) -> BaseRef | None:
    """The remote default branch, read locally from ``origin/HEAD`` (no fetch).

    One answer for the cut, the scope gate's diff base and the PR base, so the
    gate evaluates exactly what the PR carries. Not the launching checkout's
    HEAD, which can sit on a branch carrying unreviewed work, and not local
    ``main``, which can lag the remote.

    Returns None when it cannot be read, and callers then keep their previous
    behaviour. That includes an ``origin/HEAD`` that is DANGLING: git does not
    update it on fetch, so after the remote renames its default branch it still
    names a ref that ``fetch --prune`` removed (measured on git 2.43), and
    cutting from it would fail every task. The full ref name is read (not
    ``--short``, which renders an ambiguous name as ``remotes/origin/<x>``).
    """
    prefix = "refs/remotes/origin/"
    rc, ref = await _git_read(repo_root, "symbolic-ref", "refs/remotes/origin/HEAD")
    if rc != 0 or not ref.startswith(prefix) or ref == prefix:
        return None
    rc, sha = await _git_read(repo_root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if rc != 0 or not sha:
        logger.warning("origin/HEAD names %s, which does not resolve; ignoring it", ref)
        return None
    return BaseRef(name=ref[len(prefix):], sha=sha)


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
    base: BaseRef | None = None,
) -> Path:
    """Create a git worktree for code task isolation.

    Cleans up stale state from previous runs before creating.
    Returns the worktree path on success.
    Handles the case where the task branch already exists (e.g., resume
    after restart) by checking it out instead of creating a new branch.
    Raises RuntimeError if worktree creation fails.

    ``base`` (from ``resolve_base``) cuts the new branch from ``base.sha``,
    the default branch's commit, with ``--no-track`` so the task branch has no
    upstream: a bare ``git pull`` or ``git push`` inside the task must not
    target the default branch (cut from a remote-tracking ref, a branch TRACKS
    it by default; measured on git 2.43). ``None`` keeps the previous
    behaviour: cut from HEAD.
    """
    short_id = task_id[:8]
    branch = f"task/{short_id}"
    wt_path = worktree_base / f"task-{short_id}"

    # Clean up stale state from previous runs of this task
    if wt_path.exists():
        logger.info("Stale worktree dir %s exists, cleaning up", wt_path)
        await cleanup_worktree(wt_path, repo_root)
        # If cleanup failed (logged as warning), force-remove the directory
        # so git worktree add doesn't fail on an existing path.
        if wt_path.exists():
            import shutil

            shutil.rmtree(wt_path, ignore_errors=True)
            logger.warning("Force-removed stale worktree dir at %s", wt_path)
    else:
        # No dir but branch might linger from a prior crash
        await _prune_worktrees(repo_root)
        await _delete_branch(branch, repo_root)

    if base is not None:
        add_args = ["--no-track", "-b", branch, str(wt_path), base.sha]
    else:
        add_args = ["-b", branch, str(wt_path)]
    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "add", *add_args,
        cwd=str(repo_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        if "already exists" in err:
            # Branch exists from previous run — check it out
            proc = await asyncio.create_subprocess_exec(
                "git", "worktree", "add", str(wt_path), branch,
                cwd=str(repo_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                err2 = stderr.decode(errors="replace")
                logger.error("Failed to create worktree: %s", err2)
                raise RuntimeError(f"Worktree creation failed: {err2}")
            logger.info(
                "Created worktree at %s (existing branch %s)", wt_path, branch,
            )
        else:
            logger.error("Failed to create worktree: %s", err)
            raise RuntimeError(f"Worktree creation failed: {err}")
    else:
        logger.info("Created worktree at %s (branch %s)", wt_path, branch)

    return wt_path


async def verify_worktree(wt_path: Path) -> bool:
    """Check if a path is a valid git worktree."""
    if not wt_path.exists():
        return False
    proc = await asyncio.create_subprocess_exec(
        "git", "rev-parse", "--git-dir",
        cwd=str(wt_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    return proc.returncode == 0


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
