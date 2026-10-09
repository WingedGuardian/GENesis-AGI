"""Worktree management for code task isolation.

Extracted from engine.py to keep file size under 600 LOC.
Provides async functions for creating and cleaning up git worktrees
used by CODE-type task steps (Amendment #7).
"""

from __future__ import annotations

import asyncio
import logging
import os
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
        env=_scrubbed_git_env(),
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


#: Repository-local variables, as ``git rev-parse --local-env-vars`` lists them
#: (git 2.43). Any of them can point git at another repository than repo_root,
#: so every git call here runs without them: a classification read and the
#: mutation that follows it must see the same repository.
#: Except the two command-line config channels (``GIT_CONFIG_PARAMETERS``,
#: ``GIT_CONFIG_COUNT`` with its ``GIT_CONFIG_KEY_n``/``GIT_CONFIG_VALUE_n``):
#: they are protected config, the only place git accepts ``safe.directory``
#: from besides the system and global files, so an install whose checkout uid
#: differs from the executor's passes it there. Scrubbing them makes every git
#: call here fail with "dubious ownership" (MEASURED, git 2.43). Config can
#: still set ``core.worktree``; whoever launched the executor set it on
#: purpose, unlike a ``GIT_DIR`` inherited from a parent git process.
_GIT_LOCATION_VARS = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG",
    "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE", "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
    "GIT_SHALLOW_FILE", "GIT_COMMON_DIR",
)


def _scrubbed_git_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}


async def _git_read(repo_root: Path, *args: str) -> tuple[int, str]:
    """Run a read-only git command; (returncode, stripped stdout). A timeout
    reads as a failure (-1), never a hang."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(repo_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_scrubbed_git_env(),
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_GIT_READ_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("git %s in %s timed out", " ".join(args), repo_root)
        return -1, ""
    # surrogateescape: a path that is not UTF-8 must round-trip, or a worktree
    # lookup by path would miss it.
    return proc.returncode or 0, (out or b"").decode(errors="surrogateescape").strip()


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
        env=_scrubbed_git_env(),
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        # The add that follows then fails on the stale record; say why here.
        logger.warning(
            "git worktree prune failed in %s: %s",
            repo_root, stderr.decode(errors="replace").strip(),
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create_worktree(
    task_id: str,
    repo_root: Path,
    worktree_base: Path,
    base: BaseRef | None = None,
    *,
    keep_branch: bool = False,
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

    ``keep_branch`` is for a RESUMED task (#3060): its branch holds the steps
    already committed, so it is never deleted; the worktree is re-added on it.
    A fresh task deletes a leftover branch and cuts anew, as before.
    """
    short_id = task_id[:8]
    branch = f"task/{short_id}"
    wt_path = worktree_base / f"task-{short_id}"

    # Clean up stale state from previous runs of this task
    if wt_path.exists():
        logger.info("Stale worktree dir %s exists, cleaning up", wt_path)
        await cleanup_worktree(wt_path, repo_root, delete_branch=not keep_branch)
        if wt_path.exists():
            await _clear_stale_dir(wt_path, repo_root, task_id)
    else:
        # No dir but branch might linger from a prior crash
        await _prune_worktrees(repo_root)
        if not keep_branch:
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
        env=_scrubbed_git_env(),
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
                env=_scrubbed_git_env(),
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


class StaleWorktreeError(RuntimeError):
    """A previous task worktree could not be cleared without losing work.

    Nothing was deleted; the message says where it is and why it was left.
    """


async def _worktree_records(repo_root: Path) -> list[dict[str, str]] | None:
    """``git worktree list --porcelain -z`` as one dict per worktree, or None
    when it cannot be read. NUL-delimited, so a path holding a newline cannot
    split a record (git 2.43: fields end in NUL, a record in an empty field)."""
    rc, out = await _git_read(repo_root, "worktree", "list", "--porcelain", "-z")
    if rc != 0:
        return None
    records: list[dict[str, str]] = [{}]
    for field in out.split("\0"):
        if not field:
            records.append({})
            continue
        key, _, value = field.partition(" ")
        records[-1][key] = value
    # git always lists at least the main worktree; an empty listing is unread.
    return [r for r in records if "worktree" in r] or None


def _record_for(records: list[dict[str, str]], wt_path: Path) -> dict[str, str] | None:
    """The record git keeps for ``wt_path``, skipping a ``prunable`` one.

    git marks a record prunable when the worktree's ``.git`` file is gone, i.e.
    the directory was deleted (and possibly re-created) outside git; nothing
    lives there, and treating it as registered would adopt an orphan directory
    in which git walks up to the main checkout (MEASURED, git 2.43). A LOCKED
    record is never marked prunable, so the caller checks the directory too.
    """
    target = wt_path.resolve()
    return next(
        (r for r in records if "prunable" not in r and Path(r["worktree"]).resolve() == target),
        None,
    )


async def _clear_stale_dir(wt_path: Path, repo_root: Path, task_id: str) -> None:
    """Clear a task-worktree path that ``cleanup_worktree`` could not remove.

    A registered worktree survives cleanup because ``git worktree remove``
    (no ``--force``) refused it, i.e. it holds uncommitted work or a lock.
    It is never deleted: the worktree reaper archives it into the worktree
    trash, and re-creating at the same path cannot work while git still has it
    registered (MEASURED: the add fails on the existing branch, then on the
    registered path). A directory git does not know is an orphan; it goes to
    the trash (#2926 G2) so the new worktree can take its place.

    Registration comes from ``git worktree list``, not ``git rev-parse`` inside the directory:
    task worktrees live under the repo, so ``git rev-parse`` inside an orphan
    directory walks up to the main repository and succeeds (MEASURED). An
    unreadable list deletes nothing.
    """
    records = await _worktree_records(repo_root)
    if records is None:  # unreadable or timed out: treat as registered, delete nothing
        raise StaleWorktreeError(
            f"the previous worktree for this task, {wt_path}, was left in place: "
            "git's worktree list could not be read to tell an orphan from live work"
        )
    record = _record_for(records, wt_path)
    if record is not None:
        if "locked" in record:
            raise StaleWorktreeError(
                f"the previous worktree for this task, {wt_path}, is locked; nothing "
                "reaps a locked worktree, so unlock it (git worktree unlock) or "
                "remove it by hand before retrying"
            )
        raise StaleWorktreeError(
            f"the previous worktree for this task, {wt_path}, holds uncommitted work "
            "git would not remove; it was left in place, and the worktree reaper "
            "archives it into the worktree trash once it goes idle"
        )
    from genesis.trash import TrashRefused, trash

    try:
        # Off the event loop: sizing a large orphan tree walks every file.
        stone = await asyncio.to_thread(
            trash,
            wt_path,
            reason=f"orphan task worktree directory, task {task_id}",
            caller="worktree_mgr.create_worktree",
        )
    except TrashRefused as exc:
        raise StaleWorktreeError(
            f"the stale directory {wt_path} could not be moved to the trash: {exc}"
        ) from None
    logger.warning("Moved orphan task worktree dir %s to the trash (%s)", wt_path, stone.entry_id)
    # A prunable record for this path survives the move, and ``git worktree
    # add`` refuses "a missing but already registered worktree" (MEASURED).
    await _prune_worktrees(repo_root)


async def is_registered_worktree(wt_path: Path, repo_root: Path) -> bool:
    """Whether git lists ``wt_path`` as a worktree of ``repo_root`` (#3021).

    Not ``git rev-parse`` inside the directory: task worktrees live under the
    repo, so in an orphan directory it walks up to the main repository and
    succeeds (MEASURED), and a resumed task would then run against the main
    checkout. An unreadable listing raises StaleWorktreeError rather than
    reading as False, so nothing is re-created over a live worktree.
    """
    if not wt_path.exists():
        return False
    records = await _worktree_records(repo_root)
    if records is None:
        # Not False: the caller would re-create, and re-creating removes a
        # clean worktree and force-deletes its branch, committed steps included.
        raise StaleWorktreeError(
            f"the worktree for this task, {wt_path}, was left in place: "
            "git's worktree list could not be read"
        )
    if _record_for(records, wt_path) is None:
        return False
    # Listed is not enough: a locked record is never marked prunable, so a
    # locked worktree whose directory was deleted and re-created still lists.
    # The directory must be that worktree's own top level; in an orphan, git
    # walks up to the main checkout and names it instead (MEASURED).
    # Its git directory must also be this repository's: a directory re-created
    # and given its own `git init` is its own top level, and git does not mark
    # the old record prunable once a .git exists there again (MEASURED).
    here = await _git_read(
        wt_path, "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"
    )
    ours = await _git_read(repo_root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    lines = here[1].split("\n")
    if here[0] != 0 or ours[0] != 0 or len(lines) != 2:
        raise StaleWorktreeError(
            f"the worktree for this task, {wt_path}, was left in place: git "
            "lists it, but could not read it"
        )
    top, common = (Path(line).resolve() for line in lines)
    return top == wt_path.resolve() and common == Path(ours[1]).resolve()


async def cleanup_worktree(
    wt_path: Path,
    repo_root: Path,
    *,
    delete_branch: bool = True,
) -> None:
    """Remove a worktree and (unless ``delete_branch`` is False) its branch.

    NO --force per CLAUDE.md worktree rules.
    """
    branch = _branch_from_wt_path(wt_path) if delete_branch else None

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
        env=_scrubbed_git_env(),
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
