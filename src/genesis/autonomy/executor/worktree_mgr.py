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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create_worktree(
    task_id: str,
    repo_root: Path,
    worktree_base: Path,
    base: BaseRef | None = None,
) -> Path:
    """Create a worktree only when its canonical task state is fresh."""
    branch, wt_path = await assert_fresh_task(task_id, repo_root, worktree_base)
    if base is None:
        rc, start_tip = await _git_read(
            repo_root, "rev-parse", "--verify", "HEAD^{commit}",
        )
        if rc != 0 or not start_tip:
            raise _task_stale(task_id, branch, wt_path, "current HEAD could not be read as a commit")
    else:
        start_tip = base.sha
        if not start_tip:
            raise _task_stale(task_id, branch, wt_path, "selected base has no commit")

    await assert_fresh_task(task_id, repo_root, worktree_base)
    try:
        wt_path.mkdir(parents=True)
    except FileExistsError as exc:
        raise _task_stale(task_id, branch, wt_path, "target path became occupied") from exc
    except OSError as exc:
        raise _task_stale(task_id, branch, wt_path, f"target path could not be claimed: {exc}") from exc

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "worktree", "add", "--no-track", "-b", branch,
            str(wt_path), start_tip,
            cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_scrubbed_git_env(),
        )
        _, stderr = await proc.communicate()
    except Exception as exc:
        raise _task_stale(task_id, branch, wt_path, f"git worktree add failed: {exc}") from exc
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip() or f"exit status {proc.returncode}"
        raise _task_stale(task_id, branch, wt_path, f"git worktree add failed: {detail}")

    try:
        actual_tip = await branch_tip(branch, repo_root)
        if actual_tip != start_tip:
            raise StaleWorktreeError(
                f"created branch tip changed from {start_tip} to {actual_tip}",
            )
        if await _tag_conflict(branch, repo_root):
            raise StaleWorktreeError(f"same-named tag appeared for {branch}")
        records = await _worktree_records(repo_root)
        if records is None:
            raise StaleWorktreeError("git worktree list could not be read after add")
        branch_records = [
            record for record in records
            if record.get("branch") == f"refs/heads/{branch}"
        ]
        if (
            len(branch_records) != 1
            or _as_named(Path(branch_records[0]["worktree"])) != _as_named(wt_path)
        ):
            raise StaleWorktreeError("created branch registration is missing, duplicated, or elsewhere")
        await _validate_live_worktree(wt_path, repo_root, branch, start_tip, records)
    except Exception as exc:
        raise _task_stale(
            task_id, branch, wt_path, f"post-add validation failed: {exc}",
        ) from exc

    logger.info("Created worktree at %s (branch %s)", wt_path, branch)
    return wt_path


class StaleWorktreeError(RuntimeError):
    """Task worktree state is unsafe to create or reuse automatically.

    Nothing was deleted; the message identifies what to inspect before retrying.
    """


def task_locator(task_id: str, worktree_base: Path) -> tuple[str, Path]:
    short_id = task_id[:8]
    return f"task/{short_id}", worktree_base / f"task-{short_id}"


async def assert_fresh_task(
    task_id: str,
    repo_root: Path,
    worktree_base: Path,
) -> tuple[str, Path]:
    """Return the canonical task locator only when no retained state is found."""
    if not isinstance(task_id, str) or not task_id:
        raise StaleWorktreeError(
            f"task {task_id!r}, branch task/<invalid>, worktree {worktree_base}: "
            "task ID must have a non-empty string prefix",
        )
    branch, wt_path = task_locator(task_id, worktree_base)
    prefix = task_id[:8]
    if "/" in prefix or "\\" in prefix or "\0" in prefix:
        raise _task_stale(task_id, branch, wt_path, "task ID prefix is path-bearing")
    try:
        rc, _ = await _git_read(repo_root, "check-ref-format", "--branch", branch)
    except Exception as exc:
        raise _task_stale(
            task_id, branch, wt_path, f"task branch name could not be checked: {exc}",
        ) from exc
    if rc != 0:
        raise _task_stale(task_id, branch, wt_path, "task ID prefix is not a valid Git branch name")

    try:
        tip = await branch_tip(branch, repo_root)
    except Exception as exc:
        raise _task_stale(task_id, branch, wt_path, f"task branch could not be classified: {exc}") from exc
    if tip is not None:
        raise _task_stale(task_id, branch, wt_path, f"retained task branch exists at {tip}")

    try:
        records = await _worktree_records(repo_root)
    except Exception as exc:
        raise _task_stale(
            task_id, branch, wt_path, f"git worktree list could not be read: {exc}",
        ) from exc
    if records is None:
        raise _task_stale(task_id, branch, wt_path, "git worktree list could not be read")
    if _listed_records(records, wt_path):
        raise _task_stale(task_id, branch, wt_path, "target path already has a worktree registration")
    branch_ref = f"refs/heads/{branch}"
    if any(record.get("branch") == branch_ref for record in records):
        raise _task_stale(task_id, branch, wt_path, "task branch is registered at another worktree path")
    try:
        has_tag = await _tag_conflict(branch, repo_root)
    except Exception as exc:
        raise _task_stale(task_id, branch, wt_path, f"task tag could not be classified: {exc}") from exc
    if has_tag:
        raise _task_stale(task_id, branch, wt_path, "same-named tag already exists")
    if os.path.lexists(wt_path):
        raise _task_stale(task_id, branch, wt_path, "target path is already occupied")
    return branch, wt_path


async def branch_tip(branch: str, repo_root: Path) -> str | None:
    """Return a local task branch tip, or None when it is absent."""
    ref = f"refs/heads/{branch}"
    # --exists, not --verify --quiet: that exits 1 for a corrupt ref as well as a
    # missing one. Here 2 alone means missing (git 2.43; older git rejects the
    # flag, which reads as unreadable).
    rc, _ = await _git_read(repo_root, "show-ref", "--exists", ref)
    if rc == 2:
        return None
    if rc != 0:
        raise StaleWorktreeError(f"local branch {branch} could not be read")
    rc, sha = await _git_read(
        repo_root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}",
    )
    if rc != 0 or not sha:
        raise StaleWorktreeError(f"local branch {branch} does not resolve to a commit")
    return sha


async def _tag_conflict(branch: str, repo_root: Path) -> bool:
    rc, _ = await _git_read(repo_root, "show-ref", "--exists", f"refs/tags/{branch}")
    if rc == 0:
        return True
    if rc == 2:
        return False
    raise StaleWorktreeError(f"tag {branch} could not be read")


def _task_stale(task_id: str, branch: str, wt_path: Path, detail: str) -> StaleWorktreeError:
    return StaleWorktreeError(
        f"task {task_id}, branch {branch}, worktree {wt_path}: {detail}; "
        "inspect the retained ref and Git worktree registration manually before retrying",
    )


def _listed_records(
    records: list[dict[str, str]], wt_path: Path,
) -> list[dict[str, str]]:
    target = _as_named(wt_path)
    return [
        record for record in records
        if _as_named(Path(record["worktree"])) == target
    ]


async def _validate_live_worktree(
    wt_path: Path,
    repo_root: Path,
    branch: str,
    tip: str,
    records: list[dict[str, str]],
) -> None:
    listed = _listed_records(records, wt_path)
    ref = f"refs/heads/{branch}"
    if len(listed) != 1:
        raise StaleWorktreeError("worktree registration is missing or duplicated")
    record = listed[0]
    if "prunable" in record:
        raise StaleWorktreeError("worktree registration is prunable")
    if "locked" in record:
        raise StaleWorktreeError("worktree registration is locked")
    if record.get("branch") != ref or not await is_registered_worktree(wt_path, repo_root):
        raise StaleWorktreeError(f"worktree is not safely registered on {branch}")
    rc, head_ref = await _git_read(wt_path, "symbolic-ref", "--quiet", "HEAD")
    rc2, head = await _git_read(wt_path, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if rc != 0 or head_ref != ref or rc2 != 0 or head != tip:
        raise StaleWorktreeError(f"worktree does not match retained {branch}@{tip}")


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


def _as_named(path: Path) -> Path:
    """``path`` with its parent resolved and its last component kept.

    Not ``resolve()``: a task directory replaced by a symlink to the main
    checkout would resolve to the main checkout's own record and top level,
    and a resumed task would run there (#3061 review, reproduced on git 2.43).
    Spelled this way it keeps its own name, so it can match only its own
    record and is never its own top level.
    """
    path = Path(os.path.abspath(path))
    return path.parent.resolve() / path.name


def _record_for(records: list[dict[str, str]], wt_path: Path) -> dict[str, str] | None:
    """The record git keeps for ``wt_path``, skipping a ``prunable`` one.

    git marks a record prunable when the worktree's ``.git`` file is gone, i.e.
    the directory was deleted (and possibly re-created) outside git; nothing
    lives there, and treating it as registered would adopt an orphan directory
    in which git walks up to the main checkout (MEASURED, git 2.43). A LOCKED
    record is never marked prunable, so the caller checks the directory too.
    """
    target = _as_named(wt_path)
    return next(
        (r for r in records if "prunable" not in r and _as_named(Path(r["worktree"])) == target),
        None,
    )


async def is_registered_worktree(wt_path: Path, repo_root: Path) -> bool:
    """Whether ``wt_path`` is a live linked worktree of ``repo_root`` (#3021).

    git's own definition, the one ``git worktree remove`` enforces: the
    directory's ``.git`` FILE points at an admin directory under the
    repository's ``<common-dir>/worktrees/``, and that admin directory's
    ``gitdir`` points back at this directory. Each half alone is imitable,
    and every one of these was MEASURED (git 2.43) to pass a weaker check:
    an orphan directory (git walks up to the main checkout), a record kept
    prunable or locked after the directory was re-created, a directory given
    its own ``git init``, a task path replaced by a symlink to the checkout or
    to another task's worktree, and a ``.git`` file copied from another task.
    Paths are compared as named (``_as_named``), never fully resolved.

    False means this path is not verified as this repository's live worktree.
    Creation callers must independently refuse retained refs and occupied paths
    rather than infer that the branch is disposable. Anything that cannot be
    READ raises StaleWorktreeError instead of reading as False.
    """
    dot_git = wt_path / ".git"
    if wt_path.is_symlink() or not dot_git.is_file():
        return False  # a symlink, an orphan, or its own repository: not a linked worktree

    def unreadable(what: str) -> StaleWorktreeError:
        return StaleWorktreeError(
            f"the worktree for this task, {wt_path}, was left in place: {what} could not be read"
        )

    # One value per git call: a path may contain a newline.
    rc, admin = await _git_read(wt_path, "rev-parse", "--absolute-git-dir")
    rc2, common = await _git_read(
        repo_root, "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    if rc != 0 or rc2 != 0:
        raise unreadable("its git directory")
    admin_dir = Path(admin)
    if admin_dir.parent.resolve() != Path(common).resolve() / "worktrees":
        return False  # not one of this repository's linked worktrees
    try:
        back = (admin_dir / "gitdir").read_text(errors="surrogateescape").rstrip("\n")
    except FileNotFoundError:
        return False  # pruned admin directory: nothing points back
    except OSError:
        raise unreadable("git's record of it") from None
    # worktree.useRelativePaths writes this backlink relative to the admin
    # directory. Keep the named-path check so a symlinked worktree cannot pass.
    back_link = Path(back)
    if not back_link.is_absolute():
        back_link = admin_dir / back_link
    return _as_named(back_link) == _as_named(wt_path) / ".git"


async def cleanup_worktree(
    wt_path: Path,
    repo_root: Path,
) -> None:
    """Leave worktree and Git state unchanged for independent lifecycle management."""
    logger.info(
        "Leaving task worktree state untouched at %s; lifecycle cleanup is independent",
        wt_path,
    )
