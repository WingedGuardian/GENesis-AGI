#!/usr/bin/env python3
"""Worktree lifecycle manager — archives stale worktrees, deletes nothing.

A worktree is only ever touched when it is unused, unlocked, has no paused Git
operation, and contains no nested worktree. Past those protections it is
ARCHIVED into the trash as a gzip tarball, together with a tombstone row. It is
never deleted, and nothing in the trash expires.

That is a deliberate reversal of an earlier design that deleted merged
worktrees outright. The reason is that a worktree can hold the only surviving
trace of the session that produced it: MEASURED 2026-09-10, the session that
authored PR #1702 has no ``cc_sessions`` row and no transcript in any of 532 CC
project directories, so its commits are the entire record of its existence.
Whether a piece of context will matter later is not a judgement a daily timer
is in a position to make, and the storage does not justify guessing — the 192
worktrees present that day were ~11 GB raw, ~2.9 GB archived, against 265 GB
free.

Two lanes remain, but they now differ only in WHEN and in the label they carry,
never in whether the work survives:

  MERGED (7+ days idle)     content is also in main, so it drains sooner
  UNMERGED (14+ days idle)  may be the only copy, so it is held longer

Between day 7 and day 14 an unmerged worktree reports as ``at_risk``: a
bounded, draining window that surfaces work about to be archived, rather than a
list that grows forever.

A detached-HEAD worktree (no branch) is judged by whether its HEAD commit is
already in main; without this it would default to branch "unknown" and never be
considered at all.

Every fate is decided in one place (``_classify`` / ``classify_all``) and merely
carried out by ``main``. ``--report-json`` renders that same classification, so
a dashboard cannot describe a worktree one way while the reaper treats it
another.

Usage:
    worktree_lifecycle.py                    # Run: archive stale worktrees
    worktree_lifecycle.py --dry-run          # Show what would happen
    worktree_lifecycle.py --report-json      # Classify everything, change nothing
    worktree_lifecycle.py --no-network       # Skip the one gh call (faster, safe)
    worktree_lifecycle.py --list-trash       # Show archives with age, lane, size
    worktree_lifecycle.py --recover <name>   # Restore an archived worktree

Run daily by the genesis-disk-hygiene.timer systemd unit (via
scripts/disk_hygiene.sh, alongside disk_reclaim.py). Also runnable by hand.

Stdlib-only (no genesis package imports) — disk_hygiene.sh falls back to the
system python3 when the venv is absent, so an import from the genesis package
would break the reaper on exactly the box that needs it. Uses gh CLI for PR
status.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path

# Two lanes, by whether the work is already in main (owner ruling 2026-09-10).
# MERGED work is a duplicate of main, so it drains fast. UNMERGED work may be the
# only copy, so it is held longer AND is never auto-purged from the trash.
MERGED_STALE_DAYS = 7
UNMERGED_STALE_DAYS = 14
STALE_DAYS = UNMERGED_STALE_DAYS  # back-compat alias (the conservative bound)

# Nothing here is ever deleted, so a reaped worktree is compressed instead.
# gzip, not xz, and the reason is measured rather than habitual: on a 58 MB
# worktree from this install (2026-09-10) xz preset 6 gave 10.6 MB in 46.6s
# while gzip gave 14.9 MB in 6.6s. Across the 192 worktrees present that day
# xz would buy ~0.9 GB for ~2 extra hours of CPU, against 265 GB free. When
# disk is the abundant resource and time is not, the weaker ratio is correct.
COMPRESS_LEVEL = 6

# Append-only, one JSON object per reaped worktree, never rewritten. It exists
# so the trash is GREPPABLE: answering "which branch touched X" from the
# archives alone would mean unpacking every one of them. Kilobytes, and it
# outlives the archive it describes.
TOMBSTONE_INDEX = Path.home() / ".genesis" / "worktree-tombstones.jsonl"

# The classification, cached for readers that cannot afford to compute it.
# MEASURED 2026-09-10: classifying the 191 linked worktrees on this install cost
# 20s without the network check and 48s with — dominated by seven `git rev-parse`
# calls per worktree in the in-progress check. That is not a session-start or a
# web-request budget, so the session-context block and the dashboard board both
# read THIS file instead of re-deriving. One producer, so they cannot disagree.
BOARD_CACHE = Path.home() / ".genesis" / "worktree-board.json"
TRASH_DIR = Path.home() / ".genesis" / "worktree-trash"


# Private modes for everything this module writes into the trash. A reaped
# worktree is a verbatim copy of someone's working tree, which routinely holds a
# 0600 `.env`, an SSH key, or a token. Rolling that into a tarball created under
# a normal 022 umask republishes it at 0644, and the containing directory at
# 0755, so a secret that was private in the worktree becomes readable to every
# local account the moment it is archived. The archive must be no more readable
# than the least readable thing it can contain.
_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


class WorktreeScanError(RuntimeError):
    """Enumeration FAILED, as distinct from finding nothing.

    These two were the same value — an empty list — and that is the whole bug.
    A timed-out or erroring ``git worktree list`` produced exactly what a healthy
    repository with no linked worktrees produces, so the board and the JSON
    report published a valid-looking EMPTY view and exited 0. Every worktree then
    reads as gone: not flagged as unknown, not stale, simply absent, until some
    later run happens to succeed. A monitoring surface that reports "nothing"
    when it means "I could not look" is worse than one that reports an error,
    because nothing downstream can tell the difference.
    """



# Ambient git LOCATION overrides. `git rev-parse --local-env-vars` lists these as
# repository-local and they beat `-C`, so with GIT_DIR or GIT_COMMON_DIR exported
# for another repository every git call answers for THAT repository.
#
# Applied to the BUNDLE path specifically, and the reason is the failure shape
# rather than tidiness: everywhere else a redirected repo makes a command FAIL,
# which is noisy and recoverable. Here it would SUCCEED and write another
# project's history into this archive — a wrong archive that verifies, which is
# the one outcome no later check can catch. The remaining call sites in this file
# are unchanged and unaudited for this; that is tracked separately rather than
# swept into a review round about the archive.
_GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")


def _git_env() -> dict[str, str]:
    """The environment with git's repository-location overrides removed."""
    env = dict(os.environ)
    for var in _GIT_LOCATION_VARS:
        env.pop(var, None)
    return env

# Candidate bases to exclude from the bundle, most specific first. Bundling only
# the commits a worktree does NOT share with the mainline keeps it small:
# MEASURED on this repo, a full-history bundle is 27 MB while the unique-commit
# bundles for two real branches are 68 KB and 88 KB (5 and 16 commits).


def _scratch_dir_for(stored: Path) -> Path:
    """The temporary extraction directory for ``stored``, with a BOUNDED name.

    Prepending a prefix to the full archive filename can exceed the 255-byte
    component limit ext4 and most Linux filesystems enforce, and the asymmetry is
    the worst available: the archive is created SUCCESSFULLY and can then never
    be opened, because `mkdir` raises ENAMETOOLONG on a name derived from a name
    that already fit. A fixed-width digest makes "archivable" and "recoverable"
    the same set. The archive's own name identifies it; this directory is
    transient and only has to be unique.

    A function rather than an inline expression so a test can assert the REAL
    derivation — recomputing the formula in the test would pass against any
    implementation, including the broken one.
    """
    digest = hashlib.sha256(stored.name.encode("utf-8", "surrogateescape")).hexdigest()[:24]
    return TRASH_DIR / f".extract-{digest}"


def _default_branch(repo_root: Path) -> str:
    """The repository's default branch, asked of the remote rather than assumed.

    Hardcoding "main" would silently mis-narrow the merged-PR query on any fork
    or mirror whose default differs, and a mis-narrowed query returns nothing —
    which reads as "not merged" and is the safe direction, but for the wrong
    reason and invisibly. Falls back to "main" only when the question cannot be
    answered at all.
    """
    head = _run_git(repo_root, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], timeout=15)
    if head:
        name = head.strip().removeprefix("origin/")
        if name:
            return name
    return "main"


def _run_git(repo_root: Path, args: list[str], *, timeout: int) -> str | None:
    """Run git, returning stdout on success and None on any failure."""
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True,
            cwd=str(repo_root), timeout=timeout, env=_git_env(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    return result.stdout if result.returncode == 0 else None


def _nested_worktrees_under(wt_path: Path, repo_root: Path) -> list[str]:
    """Registered worktrees living INSIDE ``wt_path``, read fresh from git.

    Read at act time rather than reused from the classification snapshot,
    because the hazard is a worktree created DURING the scan — a snapshot taken
    before it existed cannot show it.

    Fails CLOSED in the sense that matters: if git cannot be enumerated, the
    caller is told there may be nested worktrees rather than that there are
    none, so an unanswerable question stops the move instead of permitting it.
    """
    try:
        wts = _list_worktrees(repo_root)
    except WorktreeScanError:
        return [f"<enumeration failed: refusing to move {wt_path.name}>"]
    parent = os.path.realpath(str(wt_path))
    found = []
    for wt in wts:
        other = os.path.realpath(str(wt.get("path", "")))
        if other != parent and other.startswith(parent + os.sep):
            found.append(other)
    return found


def _fsync_path(path: Path) -> None:
    """Flush a file or DIRECTORY to stable storage.

    Directories need this too, and that is the half that is easy to miss: an
    ``os.replace`` makes a name atomically VISIBLE, which is not the same as
    making it DURABLE. Without fsyncing the containing directory a power loss
    can replay the source deletion while losing the rename that published the
    archive.

    Best effort by design — a filesystem that refuses to fsync a directory
    (some network mounts) must not turn archiving into a hard failure, since the
    fallback is merely the durability we had before this existed.
    """
    flags = getattr(os, "O_DIRECTORY", 0) if path.is_dir() else 0
    try:
        fd = os.open(str(path), os.O_RDONLY | flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
LOG_DIR = Path.home() / ".genesis" / "logs"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    """Print a timestamped log line to stdout (captured by cron)."""
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    print(f"{ts} {msg}", flush=True)


def _repo_root() -> Path:
    """Resolve the repo root from this script's location."""
    here = Path(__file__).resolve()
    # scripts/worktree_lifecycle.py → repo root is ../
    return here.parent.parent


def _find_processes_in_dir(dir_path: str) -> list[int]:
    """Return PIDs whose CWD is inside ``dir_path``.

    THIS PROCESS AND ITS PARENT COUNT. They used to be excluded, which is wrong
    for the documented hand-run: `python scripts/worktree_lifecycle.py` invoked
    from inside a cold unmerged worktree has its own cwd — and its shell's — in
    the very directory being considered, so the one process that certainly IS
    using it was the one process guaranteed not to be seen. Entering a directory
    does not refresh any mtime either, so an otherwise idle 14-day-old worktree
    satisfies the staleness test while somebody is standing in it, and it gets
    renamed and archived out from under their shell.

    The exclusion was there so the reaper would not see itself, but a scheduled
    run has its cwd at the repository root and therefore inside NO linked
    worktree — so counting self and parent costs that run nothing and protects
    the interactive one.
    """
    pids: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
            if cwd == dir_path or cwd.startswith(dir_path + "/"):
                pids.append(pid)
        except (OSError, PermissionError, FileNotFoundError):
            continue
    return pids


def _list_worktrees(repo_root: Path) -> list[dict]:
    """Parse git worktree list --porcelain into structured data.

    Returns list of dicts with keys: path, head, branch, detached, locked.
    ``branch`` is absent for a detached HEAD (porcelain emits a bare ``detached``
    line instead), in which case ``detached`` is True. ``locked`` is True when the
    worktree is under ``git worktree lock``.
    Excludes the main worktree (bare=True or first entry).
    """
    # `-z` AND BYTES, for two different failure modes that share a cause: a path
    # is not text and is not line-structured.
    #
    #  * A NEWLINE is legal in a Unix path, and porcelain puts it INSIDE the
    #    `worktree <path>` value, so splitting on lines invents a truncated ghost
    #    path that matches nothing on disk. `-z` terminates records with NUL, so
    #    the value is unambiguous. (`git worktree list -h` documents `-z`.)
    #  * A path containing NON-UTF-8 bytes raised UnicodeDecodeError under
    #    `text=True` — BEFORE the failure normalisation below could turn it into
    #    a WorktreeScanError, so `--report-json` crashed with a traceback instead
    #    of reporting a scan failure. Reading bytes and decoding with
    #    `surrogateescape` round-trips such a path back to the filesystem intact.
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain", "-z"],
            capture_output=True, cwd=str(repo_root), timeout=10,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()[:200]
            raise WorktreeScanError(
                f"git worktree list exited {result.returncode}: {detail}"
            )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        raise WorktreeScanError(f"could not enumerate worktrees: {e}") from e

    worktrees: list[dict] = []
    current: dict = {}
    is_first = True

    # With -z each attribute is its own NUL-terminated field and an EMPTY field
    # ends a record, which is the same shape the old blank-line branch handled.
    fields = result.stdout.decode("utf-8", "surrogateescape").split("\0")
    for line in fields:
        if line.startswith("worktree "):
            if current and "path" in current and not is_first:
                worktrees.append(current)
            current = {"path": line[len("worktree "):]}
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            # refs/heads/branch-name → branch-name
            ref = line[len("branch "):]
            current["branch"] = ref.removeprefix("refs/heads/")
        elif line == "detached":
            # Detached HEAD: porcelain emits a bare "detached" line and NO
            # "branch " line. Mark it so the merge check keys off the HEAD sha.
            current["detached"] = True
        elif line == "locked" or line.startswith("locked "):
            # Explicit `git worktree lock` — an operator "do not touch" signal.
            # (Porcelain emits `locked` since git 2.36; on older git the flag is
            # simply never set and such a worktree falls through to the normal
            # merged/inactive checks — degrades safe, never a hard error.)
            current["locked"] = True
        elif line == "":
            if current and "path" in current and not is_first:
                worktrees.append(current)
            is_first = False
            current = {}

    # Handle last entry (no trailing newline)
    if current and "path" in current and not is_first:
        worktrees.append(current)

    return worktrees


def _last_activity_time(worktree_path: str) -> float:
    """Return the most recent mtime of any file in the worktree.

    Depth-limited walk (top 2 levels) to avoid scanning deep
    directories like node_modules or .git internals.
    """
    latest = os.path.getmtime(worktree_path)
    root = Path(worktree_path)

    for item in root.iterdir():
        if item.name == ".git":
            continue  # Skip git internals
        try:
            mtime = item.stat().st_mtime
            if mtime > latest:
                latest = mtime
            # One level deeper
            if item.is_dir():
                for sub in item.iterdir():
                    try:
                        mtime = sub.stat().st_mtime
                        if mtime > latest:
                            latest = mtime
                    except OSError:
                        continue
        except OSError:
            continue

    return latest


class _SkipNetwork(Exception):
    """Internal sentinel: method 2 was skipped because network use was refused."""


def _is_merged(ref: str, repo_root: Path, *, is_branch: bool = True) -> bool:
    """Whether a worktree's work is already in ``main`` (any method).

    Thin bool wrapper over :func:`_merge_verdict`. Kept because callers that only
    need the yes/no answer should not have to know the method names.
    """
    return bool(_merge_verdict(ref, repo_root, is_branch=is_branch))


def _merge_verdict(
    ref: str, repo_root: Path, *, is_branch: bool = True, allow_network: bool = True,
) -> str:
    """Return WHICH method proved the work is in ``main`` — or "" if none did.

    One of ``"ancestor"``, ``"pr"``, ``"patch-id"``, or ``""`` (not merged).

    The method matters because only ``"ancestor"`` is safe to act on
    IRREVERSIBLY. It means the ref is genuinely reachable from main's history,
    so the commits survive any GC. The other two are inferences: ``"pr"`` trusts
    GitHub's merged flag, and ``"patch-id"`` trusts ``git cherry``, whose own
    blind spot is documented below (it omits merge commits entirely, so a unique
    merge commit reads as "no unique work"). MEASURED on this repo 2026-09-10:
    it squash-merges, so 20 of 26 merged verdicts came from ``patch-id``.
    Irreversibility must not rest on the method with a known blind spot — see
    the tombstone index, which records what a reaped worktree uniquely held.

    ``allow_network=False`` skips method 2 (the only method that hits the
    network), for callers on a latency budget. It can only ever turn a ``"pr"``
    verdict into ``""`` or ``"patch-id"``; it never invents a merged verdict.

    ``ref`` is a branch name (``is_branch=True``) or, for a detached-HEAD
    worktree, its HEAD commit SHA (``is_branch=False``).

    For a BRANCH, three methods in order:
    1. git merge-base --is-ancestor (fast; branch tip is an ancestor of main)
    2. gh pr list --head <branch> --state merged (handles squash merges)
    3. Zero unique commits vs main (git cherry; patch-id equivalence)

    For a DETACHED HEAD, ONLY Method 1 (true ancestor of main) is trusted; the
    patch-id method (3) and the PR method (2) are skipped. This is deliberate: a
    bare commit referenced only by the worktree HEAD has no branch protecting it,
    so reaping a merely patch-equivalent (non-ancestor) commit would let a GC
    collect it inside the recovery window; and ``git cherry`` omits merge commits
    entirely (no patch id), so a unique merge commit would be mis-read as "no
    unique work" and wrongly reaped. A true ancestor is both genuinely in main's
    history AND reachable (GC-safe). The cost is fail-safe: a detached HEAD whose
    work reached main only by squash/rebase (patch-equal but not an ancestor) is
    kept, never reaped.

    Returns "" on any error (fail-safe: an unproven ref is treated as unmerged,
    which routes it to the slower, recoverable lane).
    """
    # Method 1: git merge-base (branch ref or raw SHA) — the ONLY method for a
    # detached HEAD (see docstring: patch-id/PR methods are unsafe for a bare SHA).
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ref, "main"],
            capture_output=True, cwd=str(repo_root), timeout=10,
        )
        if result.returncode == 0:
            return "ancestor"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    if not is_branch:
        return ""  # detached HEAD: ancestor-only, no patch-id/PR fallbacks

    # Method 2: gh pr list (handles squash merges) — branch heads only.
    try:
        if not allow_network:
            raise _SkipNetwork
        # VALIDATED against the base AND the merged head, not merely "a merged PR
        # once used this branch name". Branch names are reused, and a PR merged
        # into a non-default base says nothing about whether this work reached
        # main — so `--head <name>` alone can report "merged" for a branch that
        # still carries unique unmerged commits, and the 7-day merged lane would
        # then archive it a week early.
        #
        # `--base` narrows to the default branch; `mergeCommit`/`headRefOid` let
        # the CURRENT tip be compared with what was actually merged. A PR whose
        # merged head differs from the tip means work landed after the merge.
        result = subprocess.run(
            ["gh", "pr", "list", "--head", ref, "--base", _default_branch(repo_root),
             "--state", "merged", "--limit", "10", "--json", "number,headRefOid"],
            capture_output=True, text=True, cwd=str(repo_root), timeout=30,
        )
        if result.returncode == 0:
            prs = json.loads(result.stdout)
            tip = _run_git(repo_root, ["rev-parse", ref], timeout=15)
            tip = (tip or "").strip()
            for pr in prs:
                # No tip to compare against is NOT a pass: without it this is the
                # name-only check that produced the false positive.
                if tip and pr.get("headRefOid") == tip:
                    return "pr"
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError,
            _SkipNetwork):
        pass

    # Method 3: zero unique commits (patch-id equivalence) — branch heads only.
    try:
        result = subprocess.run(
            ["git", "cherry", "main", ref],
            capture_output=True, text=True, cwd=str(repo_root), timeout=10,
        )
        if result.returncode == 0:
            # Lines starting with '+' are unique commits not in main
            unique = [line for line in result.stdout.strip().splitlines()
                      if line.startswith("+")]
            if not unique:
                return "patch-id"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return ""


# Per-worktree admin-dir markers for an in-progress Git operation. Each lives
# under the worktree's OWN git dir (git resolves them per-worktree), so a paused
# rebase/merge/cherry-pick/revert/bisect in one worktree is detectable there.
_IN_PROGRESS_MARKERS = (
    "rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD",
    "REVERT_HEAD", "BISECT_LOG", "sequencer",
)


def _is_locked_now(worktree_path: Path) -> bool:
    """True if this worktree is under ``git worktree lock`` RIGHT NOW.

    `_classify` already reads the lock from `git worktree list --porcelain`, but
    that answer is minutes old by the time a reap acts on it. A lock is the one
    protection a third party takes DURING the scan — `git worktree lock` is how a
    session declares "I am working here" — so the stale answer is wrong in
    exactly the window that matters.

    Reads the admin dir's ``locked`` file directly rather than re-running
    porcelain: one stat on a path already resolved, against a subprocess per
    worktree. Fails CLOSED — an unreadable `.git` pointer reports LOCKED, because
    the alternative is archiving a worktree whose protection we could not read.
    """
    try:
        dot_git = worktree_path / ".git"
        if dot_git.is_dir():  # the main checkout, never reaped anyway
            return False
        text = dot_git.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("gitdir:"):
            target = line[len("gitdir:") :].strip()
            if not target:
                return True
            admin = Path(target)
            if not admin.is_absolute():
                admin = (worktree_path / admin).resolve()
            return (admin / "locked").exists()
    return True


def _has_in_progress_op(worktree_path: str) -> bool:
    """True if the worktree has a paused Git operation (rebase/merge/…).

    Reaping such a worktree would destroy its sequencer state (it lives in the
    per-worktree admin dir, discarded by ``git worktree prune``), making
    ``git rebase --continue`` etc. impossible. Resolves each marker via
    ``git rev-parse --git-path`` so it hits the worktree's OWN admin dir, not the
    shared one. Fail-CLOSED: if the marker paths can't be resolved (git missing,
    timeout, or the worktree's .git is transiently unreadable/malformed), returns
    True (unknown → assume in-progress and KEEP the worktree) rather than letting a
    possibly-mid-operation worktree be reaped.
    """
    for marker in _IN_PROGRESS_MARKERS:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--git-path", marker],
                capture_output=True, text=True, cwd=worktree_path, timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return True  # fail-closed: can't verify state → protect the worktree
        if result.returncode != 0:
            return True  # can't resolve marker path (broken/unreadable .git) → protect
        rel = result.stdout.strip()
        if not rel:
            return True  # unexpected empty path → protect rather than assume clean
        p = rel if os.path.isabs(rel) else os.path.join(worktree_path, rel)
        if os.path.exists(p):
            return True
    return False


def _has_uncommitted_changes(worktree_path: str) -> bool:
    """True if the worktree has ANY uncommitted state (tracked edits or untracked).

    Fail-CLOSED: any error returns True. A "dirty" verdict only ever routes a
    worktree to the gentler lane (trash instead of permanent delete), so being
    wrong in this direction costs disk, while being wrong the other way destroys
    work that exists nowhere else.

    Untracked files count as dirty on purpose: a forced worktree removal deletes
    them, and an untracked file in a merged worktree is exactly the kind of
    unreferenced work that no branch protects.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=worktree_path, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return True
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


def _dirty_patch(worktree_path: str) -> bytes:
    """A patch of tracked uncommitted changes, or b"" if none/unavailable.

    Saved alongside a trashed worktree so ``--recover`` has something to hand a
    human, since the recovery contract deliberately does not reapply dirty state.
    Untracked files are not in the patch — the trash keeps those as real files.

    BYTES, deliberately. A patch must round-trip exactly, so neither decoding nor
    an ``errors="replace"`` substitution is acceptable here: a worktree holding a
    latin-1 file would have its patch silently corrupted. It also removes the
    crash — with ``text=True`` a non-UTF-8 diff raised UnicodeDecodeError, which
    is a ValueError and so was outside every except tuple on the path; it
    propagated out of main() and every worktree after it in the list was never
    processed.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD", "--binary"],
            capture_output=True, cwd=worktree_path, timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return b""
    return result.stdout if result.returncode == 0 else b""


# ---------------------------------------------------------------------------
# Classification — the single source of truth for both reaping and reporting
# ---------------------------------------------------------------------------

# What a worktree is, as one word. Both the reaper and the dashboard board read
# these, so a worktree can never be described one way and reaped another.
STATE_IN_USE = "in_use"            # a live process is sitting in it
STATE_PROTECTED = "protected"      # locked / mid-rebase / contains a nested worktree
STATE_FRESH = "fresh"              # touched within MERGED_STALE_DAYS
STATE_AT_RISK = "at_risk"          # unmerged, aging, NOT yet reapable  <- the alert set
STATE_REAP_MERGED = "reap_merged"  # merged and old enough — archive now
STATE_REAP_UNMERGED = "reap_unmerged"  # unmerged, past the long window — trash now


def _classify(
    wt: dict, worktrees: list[dict], repo_root: Path, *,
    allow_network: bool = True, now: float | None = None,
) -> dict:
    """Decide what a worktree IS and what should happen to it. No mutation.

    The ordering matters and is not arbitrary: the cheap local protections come
    first, then the age test, and only then the merge verdict — which is the one
    step that can hit the network. A worktree younger than MERGED_STALE_DAYS is
    not reapable in EITHER lane, so returning before the verdict keeps a routine
    run from making one ``gh`` call per worktree (MEASURED 2026-09-10: 181
    linked worktrees on this install, so that ordering is the difference between
    a fast run and a multi-minute one).
    """
    now = time.time() if now is None else now
    wt_path = wt.get("path", "")
    branch = wt.get("branch")  # None for a detached HEAD
    head = wt.get("head", "")

    out = {
        "path": wt_path,
        "branch": branch or "",
        "detached": bool(wt.get("detached")),
        "head": head,
        "age_days": None,
        "merge_method": "",
        "merged": False,
        "dirty": None,
        "state": "",
        "reason": "",
        "action": "none",  # none | trash
    }

    if not wt_path or not Path(wt_path).exists():
        out["state"] = STATE_PROTECTED
        out["reason"] = "directory does not exist (ghost entry)"
        return out

    if wt.get("locked"):
        out["state"] = STATE_PROTECTED
        out["reason"] = "locked (git worktree lock)"
        return out
    if _has_in_progress_op(wt_path):
        out["state"] = STATE_PROTECTED
        out["reason"] = "in-progress or unresolvable git state (rebase/merge/broken .git)"
        return out

    wt_prefix = wt_path.rstrip("/") + os.sep
    nested = [o["path"] for o in worktrees
              if o.get("path") and o["path"] != wt_path
              and o["path"].rstrip("/").startswith(wt_prefix)]
    if nested:
        out["state"] = STATE_PROTECTED
        out["reason"] = f"contains nested worktree(s): {', '.join(nested[:3])}"
        return out

    pids = _find_processes_in_dir(wt_path)
    if pids:
        out["state"] = STATE_IN_USE
        out["reason"] = f"active processes (PIDs: {', '.join(str(p) for p in pids[:5])})"
        return out

    try:
        age_days = (now - _last_activity_time(wt_path)) / 86400
    except OSError as e:
        out["state"] = STATE_PROTECTED
        out["reason"] = f"cannot read activity time: {e}"
        return out
    out["age_days"] = round(age_days, 1)

    # Younger than the SHORTER of the two thresholds — nothing to decide yet, and
    # deciding would cost a network call per worktree.
    if age_days < MERGED_STALE_DAYS:
        out["state"] = STATE_FRESH
        out["reason"] = f"activity {age_days:.0f}d ago (< {MERGED_STALE_DAYS}d)"
        return out

    ref, is_branch = (branch, True) if branch else (head, False)
    verdict = _merge_verdict(
        ref, repo_root, is_branch=is_branch, allow_network=allow_network,
    ) if ref else ""
    out["merge_method"] = verdict
    out["merged"] = bool(verdict)

    if not verdict:
        # Dirty state matters MORE on this lane, not less: unmerged content may be
        # the only copy. It was previously computed only for merged worktrees.
        out["dirty"] = _has_uncommitted_changes(wt_path)
        if age_days < UNMERGED_STALE_DAYS:
            out["state"] = STATE_AT_RISK
            out["reason"] = (
                f"unmerged, {age_days:.0f}d cold — reaped to trash at "
                f"{UNMERGED_STALE_DAYS}d"
            )
            return out
        out["state"] = STATE_REAP_UNMERGED
        out["action"] = "trash"
        out["reason"] = f"unmerged and {age_days:.0f}d cold (>= {UNMERGED_STALE_DAYS}d)"
        return out

    out["state"] = STATE_REAP_MERGED
    out["dirty"] = _has_uncommitted_changes(wt_path)
    out["action"] = "trash"
    out["reason"] = (
        f"merged via {verdict}, {age_days:.0f}d cold"
        + (" (has uncommitted changes)" if out["dirty"] else "")
    )
    return out


def classify_all(
    repo_root: Path, *, allow_network: bool = True,
) -> list[dict]:
    """Classify every linked worktree. The board and the reaper both call this."""
    worktrees = _list_worktrees(repo_root)
    return [
        _classify(wt, worktrees, repo_root, allow_network=allow_network)
        for wt in worktrees
    ]


# ---------------------------------------------------------------------------
# Archiving + the tombstone index
# ---------------------------------------------------------------------------

ARCHIVE_SUFFIX = ".tar.gz"


def _archive_path(trash_path: Path) -> Path:
    """Sibling archive for a trash entry. Built by APPENDING, not by replacing a
    suffix — entry names embed a date and can contain dots, and
    ``Path.with_suffix`` would eat the last segment of one."""
    return Path(str(trash_path) + ARCHIVE_SUFFIX)


def _sidecar_meta_path(trash_path: Path) -> Path:
    """Metadata kept OUTSIDE the archive so listing never has to decompress."""
    return Path(str(trash_path) + ".meta.json")


def _compress_entry(trash_path: Path, meta: dict) -> Path | None:
    """Replace a trashed directory with a gzip tarball. Returns the archive path.

    The original directory is removed ONLY after the archive is written AND read
    back with at least one member. A failure at any point leaves the uncompressed
    directory exactly where it was and returns None: compression is an
    optimisation, and it must never be the reason a recovery is impossible.
    """
    archive = _archive_path(trash_path)
    # Written to a .part first and renamed only after it verifies. A tar.gz
    # written in place is a valid-looking file for the whole time it is being
    # written, so a timer killed mid-write (or a power loss) would leave a
    # truncated archive sitting beside a source directory that the next run then
    # deletes. os.replace is atomic within a filesystem, so a reader sees either
    # no archive or a complete one.
    partial = Path(str(archive) + ".part")
    try:
        with contextlib.suppress(OSError):
            partial.unlink()
        # O_CREAT with an explicit 0600 rather than open-then-chmod: the latter
        # leaves a window in which the archive exists at the umask's mode while
        # the worktree's secrets are being written into it, and that window
        # lasts for the whole compression of a ~50 MB tree.
        fd = os.open(
            str(partial),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            _PRIVATE_FILE_MODE,
        )
        with os.fdopen(fd, "wb") as raw:
            with tarfile.open(fileobj=raw, mode="w:gz", compresslevel=COMPRESS_LEVEL) as tf:
                tf.add(str(trash_path), arcname=trash_path.name)
            # Inside the fdopen block and AFTER the tarfile closed, so gzip's
            # trailer is in the buffer before it is forced to disk. Verifying a
            # file that only exists in the page cache proves the bytes were
            # written, not that they survive a crash.
            raw.flush()
            os.fsync(raw.fileno())
    except (OSError, tarfile.TarError) as e:
        _log(f"WARN compress failed for {trash_path.name}: {e} — kept uncompressed")
        with contextlib.suppress(OSError):
            partial.unlink()
        return None

    # Read the archive back IN FULL before trusting it, and compare the member
    # count against the source.
    #
    # An earlier version checked `tf.next() is not None` — one member HEADER — and
    # that is not a verification. MEASURED: a 61-member archive truncated to 50%
    # passes a first-header read while a full walk raises EOFError, so the check
    # said "clean" and the source directory was then destroyed. Walking every
    # member is what forces gzip's trailing CRC32/ISIZE check, which is the only
    # thing that proves the stream is whole. EOFError is NOT an OSError and must
    # be caught explicitly — leaving it out is how the truncation escaped.
    expected = sum(1 for _ in trash_path.rglob("*")) + 1  # + the root dir member
    try:
        with tarfile.open(partial, "r:gz") as tf:
            got = sum(1 for _ in tf)
        if got < expected:
            raise tarfile.TarError(
                f"archive holds {got} members, source had {expected}"
            )
    except (OSError, tarfile.TarError, EOFError) as e:
        _log(f"WARN archive verify failed for {trash_path.name}: {e} — kept uncompressed")
        with contextlib.suppress(OSError):
            partial.unlink()
        return None

    try:
        os.replace(partial, archive)  # atomic publish, only after verification
    except OSError as e:
        _log(f"WARN could not publish archive for {trash_path.name}: {e} — kept uncompressed")
        with contextlib.suppress(OSError):
            partial.unlink()
        return None

    # Make the RENAME durable before the source is destroyed. os.replace
    # guarantees that no reader sees a half-published name; it guarantees
    # nothing about what survives a power loss. Without this the deletion below
    # can be replayed while the rename that published the archive is not, which
    # is precisely the no-loss guarantee this module exists to make.
    _fsync_path(TRASH_DIR)

    with contextlib.suppress(OSError):
        meta_path = _sidecar_meta_path(trash_path)
        meta_path.write_text(json.dumps(meta, indent=2))
        os.chmod(meta_path, _PRIVATE_FILE_MODE)

    try:
        shutil.rmtree(str(trash_path))
    except OSError as e:
        # The directory is now in an UNKNOWN state: rmtree deletes as it walks, so
        # a mid-walk failure leaves some members gone. Discarding the verified
        # archive here — as an earlier version did, to avoid an ambiguous pair —
        # would throw away the only COMPLETE copy in favour of a partially
        # deleted one. Keep the archive; it was read back and member-counted
        # before this point.
        #
        # The ambiguity that motivated discarding it is handled where it actually
        # bites, in `_recover`: an archive and a directory sharing a base name now
        # resolve to the archive rather than being refused as two matches.
        _log(f"WARN could not fully remove {trash_path} after archiving: {e} — "
             f"KEEPING the verified archive at {archive.name}; the leftover "
             f"directory may be incomplete and should be removed by hand")
        return archive

    return archive


def _write_board_cache(results: list[dict]) -> None:
    """Publish the classification for readers on a latency budget.

    Written atomically (temp file + replace) because the readers are a
    session-start hook and a web request: a half-written file would be parsed by
    whoever looked next, and a board that reads as "no worktrees" is
    indistinguishable from a clean tree. Best-effort — failing to publish must
    never abort a reap.
    """
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "worktrees": results,
    }
    try:
        BOARD_CACHE.parent.mkdir(parents=True, exist_ok=True)
        # The temp name carries the pid: `replace` is atomic, but a SHARED temp
        # path is not — two writers interleave inside it and the loser publishes a
        # half-written document under the winner's name. The dashboard's refresh
        # endpoint made concurrent writers ordinary rather than theoretical.
        tmp = BOARD_CACHE.with_name(f"{BOARD_CACHE.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(BOARD_CACHE)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
    except OSError as e:
        # STDERR. This is the THIRD instance of one class on this branch, so it
        # is fixed as a class rather than a spot: under `--report-json` stdout is
        # a MACHINE-READABLE channel, and `_log` writes to stdout, so any
        # diagnostic emitted on the way to producing that document corrupts it.
        # This path fires exactly when something is already wrong (full or
        # read-only home), which is the worst moment to also hand the caller
        # unparseable JSON — the dashboard board shells out to this flag.
        print(f"WARN could not write board cache {BOARD_CACHE}: {e}", file=sys.stderr)


def _append_tombstone(record: dict) -> None:
    """Append one line to the tombstone index. Best-effort and never fatal.

    Failing to write a tombstone must not abort a reap — the archive is the
    durable artifact and this is the index over it.
    """
    try:
        TOMBSTONE_INDEX.parent.mkdir(parents=True, exist_ok=True)
        with TOMBSTONE_INDEX.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as e:
        _log(f"WARN could not append tombstone for {record.get('name')}: {e}")


def _unique_commits(ref: str, repo_root: Path, limit: int = 50) -> list[str]:
    """Subjects of commits on ``ref`` that are not in main — what would be lost.

    Recorded in the tombstone because it is the one fact about a reaped worktree
    that cannot be reconstructed once the branch ref is gone.
    """
    if not ref:
        return []
    try:
        result = subprocess.run(
            ["git", "log", "--format=%h %s", f"main..{ref}"],
            capture_output=True, text=True, errors="replace",
            cwd=str(repo_root), timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        # errors="replace" makes a UnicodeDecodeError unreachable here, but the
        # ValueError stays in the tuple: this is display text on a path whose
        # failure would otherwise abort the whole run mid-list.
        return []
    if result.returncode != 0:
        return []
    lines = result.stdout.strip().splitlines()
    if len(lines) > limit:
        # Bounded by MEANING: keep whole subjects and say how many were omitted,
        # rather than cutting the list at an arbitrary character count.
        return [*lines[:limit], f"<omitted: {len(lines) - limit} more commits>"]
    return lines


# ---------------------------------------------------------------------------
# Trash operations
# ---------------------------------------------------------------------------


# Filenames that would be a credential if they held real content. Checked so a
# reap can SAY it is archiving one; never used to exclude files, because a
# recovery that silently omits members is worse than one that warns.
_SECRET_SHAPED = (".env", "secrets.env", ".pem", ".key", "id_rsa", ".p12", ".pfx")


def _secret_shaped_files(worktree_path: Path) -> list[str]:
    """Real (non-symlink) files whose name says "credential". Names only.

    Symlinks are excluded deliberately: a tarball stores the LINK, not the target,
    so `secrets.env -> /repo/secrets.env` archives a dangling pointer rather than
    a credential. That distinction is the difference between a warning worth
    printing and noise on every single reap.
    """
    found: list[str] = []
    try:
        for child in worktree_path.rglob("*"):
            if ".git" in child.parts or child.is_symlink() or not child.is_file():
                continue
            name = child.name
            if any(name == s or name.endswith(s) for s in _SECRET_SHAPED):
                found.append(str(child.relative_to(worktree_path)))
                if len(found) >= 20:
                    break
    except OSError:
        return found
    return found


def _trash_name_taken_excluding_claim(trash_path: Path) -> bool:
    """Like ``_trash_name_taken`` but ignoring the directory we just claimed.

    After an atomic ``mkdir`` claim, the directory itself exists by construction,
    so the plain check would always report the name as taken. What still matters
    is whether an ARCHIVE or a SIDECAR under that name survives from an earlier
    reap — those are the forms that would be overwritten.
    """
    return (
        _archive_path(trash_path).exists() or _sidecar_meta_path(trash_path).exists()
    )


def _trash_name_taken(trash_path: Path) -> bool:
    """Whether a trash name is claimed in ANY of the forms a reap can leave.

    A reaped worktree ends up as a directory, an archive, or (transiently) both,
    plus a metadata sidecar. A guard that knows only one of those shapes stops
    protecting the others the moment a post-processing step is introduced.
    """
    return (
        trash_path.exists()
        or _archive_path(trash_path).exists()
        or _sidecar_meta_path(trash_path).exists()
    )


def _trash_worktree(
    wt: dict, repo_root: Path, *, dry_run: bool = False,
    lane: str = "merged", merge_method: str = "",
) -> bool:
    """Move a worktree to the trash directory.

    ``lane`` records WHY it was reaped: ``"unmerged"`` content exists nowhere
    else, while ``"merged"`` content is a duplicate of main. Nothing expires on
    either lane — the field is provenance for a human reading ``--list-trash``,
    not a retention switch.

    Returns True if trashed (or would be trashed in dry-run).
    """
    wt_path = Path(wt["path"])
    branch = wt.get("branch", "")
    detached = wt.get("detached", False)
    name = wt_path.name
    date_str = datetime.now(UTC).strftime("%Y%m%d")
    trash_name = f"{name}-{date_str}"
    trash_path = TRASH_DIR / trash_name

    # Avoid name collisions — against EVERY form a previous reap may have left.
    #
    # Probing only `trash_path.exists()` was blind to the entire archived
    # population, because `_compress_entry` removes the directory and leaves
    # `<name>.tar.gz`. MEASURED: after one archive cycle the loop re-picked the
    # same name and `tarfile.open(..., "w:gz")` truncated the existing tarball,
    # destroying the earlier worktree's only copy — silently, and with no undo in
    # a module whose contract is that it deletes nothing.
    if dry_run:
        # Report the name the loop below WOULD claim, without claiming it.
        probe = trash_path
        counter = 1
        while _trash_name_taken(probe):
            probe = TRASH_DIR / f"{name}-{date_str}-{counter}"
            counter += 1
        _log(f"WOULD TRASH {wt_path}: → {probe}")
        return True

    # Re-check liveness HERE, not just at classification time. Classification now
    # happens for every worktree up front (MEASURED: 19-41s over 191 worktrees),
    # and the archive step adds seconds more per entry, so the gap between "no
    # process is in this worktree" and the move is minutes rather than
    # milliseconds. A session that opens an old worktree during the scan is
    # exactly what `_find_processes_in_dir` exists to protect.
    if not wt_path.exists():
        _log(f"SKIP {wt_path}: disappeared between classification and reap")
        return False
    if _find_processes_in_dir(str(wt_path)) or _has_in_progress_op(str(wt_path)):
        _log(f"SKIP {wt_path}: became active or protected between classification and reap")
        return False
    # AND RE-READ THE LOCK, because `_classify` treats it as PROTECTED and this
    # revalidation previously did not repeat it. A lock is the one protection a
    # third party takes DURING the scan: `git worktree lock` is how a session
    # says "I am working here", so the window this whole block exists for is
    # exactly when it gets taken. Re-reading the on-disk `locked` file rather
    # than re-running `git worktree list` keeps it to one stat on the path we
    # already resolved.
    if _is_locked_now(wt_path):
        _log(f"SKIP {wt_path}: locked between classification and reap")
        return False
    # AND refuse to move a worktree that CONTAINS another registered worktree.
    # Moving the parent relocates the nested tree's files out from under git; a
    # later prune then drops the nested worktree's per-worktree HEAD, which for a
    # detached nested tree is the only ref keeping its commits reachable — and
    # the anchor tag we take is for the PARENT's sha, not the nested one's, so
    # the archive would preserve the wrong history.
    #
    # Deliberately NARROW. The finding that prompted this asked for the complete
    # eligibility check and ref snapshot to be re-run immediately before the
    # rename. That was not taken: re-running everything cannot close a
    # time-of-check gap (the re-run has its own gap), it doubles a scan MEASURED
    # at 19-41s over ~191 worktrees, and each re-derived value is another seam
    # where this file's last several rounds of findings have landed. A direct
    # check for the NAMED hazard is what the argument actually supports.
    #
    # MEASURED 2026-09-14 on this install: 0 of 279 linked worktrees sit inside
    # another linked worktree, so this is a guard against a shape that is
    # possible rather than one that is occurring. It costs one enumeration on the
    # path already being reaped.
    nested = _nested_worktrees_under(wt_path, repo_root)
    if nested:
        _log(
            f"SKIP {wt_path}: contains {len(nested)} registered worktree(s) "
            f"(first: {nested[0]}) — moving the parent would strand them"
        )
        return False

    try:
        TRASH_DIR.mkdir(parents=True, exist_ok=True)
        # chmod separately rather than relying on mkdir(mode=): mkdir's mode is
        # masked by the umask, so 0700 becomes 0700 only when the umask happens
        # to cooperate — and this must hold for a directory that ALREADY exists
        # from an earlier run under a laxer umask, which mode= cannot fix at all.
        with contextlib.suppress(OSError):
            os.chmod(TRASH_DIR, _PRIVATE_DIR_MODE)

        # Claim the name with mkdir(exist_ok=False), which is ATOMIC. The old
        # check-then-act loop had a real window: two reaper invocations handling
        # different worktrees that share a basename could both pass
        # `_trash_name_taken` before either created the destination, and the
        # second would then archive over the first. `shutil.move` onto an
        # existing empty directory places the source INSIDE it, so the claimed
        # directory is removed immediately before the move and re-created by it —
        # the claim's only job is to win the race, not to survive it.
        claimed = False
        for counter in range(1000):  # bounded; a spin here would hang the timer
            candidate = trash_path if counter == 0 else TRASH_DIR / f"{name}-{date_str}-{counter}"
            try:
                candidate.mkdir(exist_ok=False)
            except FileExistsError:
                continue
            except OSError as e:
                _log(f"ERROR claiming trash name for {wt_path}: {e}")
                return False
            # mkdir only proves no DIRECTORY held the name. A previous reap
            # leaves an ARCHIVE and a SIDECAR and no directory, so those forms
            # must be checked too — and checked HERE, inside the loop, so a
            # collision advances to the next candidate instead of refusing the
            # reap outright.
            if _trash_name_taken_excluding_claim(candidate):
                with contextlib.suppress(OSError):
                    candidate.rmdir()
                continue
            trash_path = candidate
            claimed = True
            break
        if not claimed:
            _log(f"ERROR {wt_path}: could not claim a free trash name after 1000 tries")
            return False
        # THE CLAIM IS HELD, not handed over. Releasing it here — the old
        # `trash_path.rmdir()` — reopened the very race the atomic `mkdir` above
        # closes, because between the rmdir and the move completing the name is
        # free again. A second invocation archiving a same-basename worktree
        # could claim it, and `shutil.move` onto a directory that now EXISTS
        # nests the source inside it: one archive holding two worktrees, from
        # which neither can be independently recovered, with whichever staging
        # metadata landed last.
        #
        # `os.rename` replaces an EMPTY directory atomically on POSIX, so the
        # claim can survive right up to the instant it becomes the moved
        # worktree. VERIFIED on this platform, including that the worktree and
        # trash roots share a device, so that is the path actually taken here.
        # The move itself stays BELOW, after the metadata capture — `git diff`
        # and `git log` stop resolving once the directory leaves its registered
        # path, so nothing may move before those run.

        # Write metadata to staging file BEFORE the move. If the move
        # fails we just have a harmless orphan file. If the process
        # dies after the move but before metadata lands inside the
        # trash dir, we still have it at the staging path.
        # Assembled COMPLETE up front, because it is written three times — the
        # staging file, the copy that ends up inside the archive, and the sidecar
        # beside it. Fields added after the first write produced three copies of
        # "the metadata" and no complete one.
        patch_text = _dirty_patch(str(wt_path))
        meta = {
            "original_path": str(wt_path),
            "branch": branch,
            "commit": wt.get("head", ""),
            "detached": detached,
            "trashed_at": datetime.now(UTC).isoformat(),
            "lane": lane,
            "merge_method": merge_method,
            "name": trash_path.name,
            "unique_commits": _unique_commits(branch or wt.get("head", ""), repo_root),
            # DERIVED FROM STATUS, not from patch presence. `_dirty_patch` runs
            # `git diff HEAD`, which sees TRACKED changes only — so a worktree
            # whose only uncommitted content is an untracked file produced an
            # empty patch and a tombstone saying there was nothing uncommitted.
            # That is the wrong answer for precisely the case archives exist for:
            # an untracked file is the one thing no branch and no commit
            # protects. The tombstone is the greppable durable index, so a wrong
            # value here is a wrong answer for as long as the archive lasts.
            "had_uncommitted_changes": _has_uncommitted_changes(str(wt_path)),
            "had_tracked_patch": bool(patch_text),
            "secret_files": _secret_shaped_files(wt_path),
        }
        # Both the patch and the commit list above are captured BEFORE the move:
        # once the directory leaves its registered path, `git diff` and
        # `git log main..<ref>` no longer resolve against it.
        staging_meta = TRASH_DIR / f".{trash_path.name}.meta.staging"
        staging_meta.write_text(json.dumps(meta, indent=2))

        if meta["secret_files"]:
            # S9: nothing here expires any more, so an archived credential lives
            # indefinitely. Say so at reap time rather than discovering it later.
            # MEASURED 2026-09-10: 0 real secret files across the 48 worktrees due
            # for archiving (the `secrets.env` entries are symlinks, so the LINK
            # is stored, never the content) — this warns if that ever changes.
            _log(f"  NOTE {trash_path.name} archives secret-shaped file(s): "
                 f"{', '.join(meta['secret_files'][:5])} — retained indefinitely")

        # Move worktree to trash, WITHOUT ever releasing the claimed name.
        # `os.rename` atomically replaces the empty claim directory, so the name
        # is never free between the claim and the move. Only EXDEV — a trash root
        # on another filesystem, where rename cannot reach — falls back to the
        # copy, and that path must release the claim first because `shutil.move`
        # onto an existing directory would nest the source inside it. The
        # fallback therefore keeps the original (narrower) race; it is logged
        # rather than hidden, so a cross-filesystem install knows it has it.
        try:
            os.rename(str(wt_path), str(trash_path))
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            _log(f"  NOTE {trash_path.name}: trash is on another filesystem — "
                 "copying instead; the claimed name is briefly unheld")
            trash_path.rmdir()
            shutil.move(str(wt_path), str(trash_path))

        # Move staging metadata into the trash entry.
        #
        # COLLISION-CHECKED FIRST, for the same reason `.dirty.patch` is below:
        # `.trash_meta.json` is not a reserved name and a worktree may legitimately
        # contain its own untracked one. `Path.rename` REPLACES the destination
        # silently, so an unconditional move destroyed the user's file in the
        # worktree AND in the archive, since the archive is made from the moved
        # directory. For a module contracted to delete nothing, that is the
        # contract breaking.
        #
        # OURS keeps the canonical name rather than being suffixed, because
        # recovery locates an entry BY that name — including the archives already
        # on disk. So the worktree's own file is the one moved aside, and the log
        # says where it went rather than leaving it to be discovered.
        final_meta = trash_path / ".trash_meta.json"
        if final_meta.exists():
            preserved = None
            for n in range(1, 1000):
                candidate = trash_path / f".trash_meta.json.from-worktree-{n}"
                if not candidate.exists():
                    preserved = candidate
                    break
            if preserved is not None:
                with contextlib.suppress(OSError):
                    final_meta.rename(preserved)
                _log(f"  NOTE {trash_path.name} contained its own .trash_meta.json — "
                     f"kept as {preserved.name} so it survives in the archive")
            else:
                _log(f"  WARN {trash_path.name} contains a .trash_meta.json and no free "
                     "name was available to preserve it; it will be REPLACED")
        staging_meta.rename(final_meta)

        if patch_text:
            # N1: the success log used to sit INSIDE the suppress, so a failed
            # write produced no output at all while the tombstone still recorded
            # had_uncommitted_changes=True — an index claiming a patch that is not
            # there. Report both outcomes.
            # COLLISION-CHECKED, because this name is not reserved. A worktree
            # may legitimately contain an untracked `.dirty.patch` of its own,
            # and an unconditional write would destroy it — in the archive AND in
            # the worktree, since the archive is made from the moved directory.
            # For a module whose contract is that it deletes nothing, silently
            # replacing a user's file is the contract breaking, not a detail.
            # Falling back to a suffixed name keeps both.
            target = trash_path / ".dirty.patch"
            if target.exists():
                for n in range(1, 1000):
                    alt = trash_path / f".dirty.patch.archived-{n}"
                    if not alt.exists():
                        target = alt
                        break
                else:
                    target = None  # pathological; better to warn than to guess
                if target is not None:
                    _log(f"  NOTE {trash_path.name} already contains .dirty.patch — "
                         f"saving recovery patch as {target.name} so the original survives")
            if target is None:
                _log(f"  WARN could not find a free name for the recovery patch in "
                     f"{trash_path.name}; the uncommitted changes are still inside "
                     "the archive, but no patch file was written")
            else:
                try:
                    # 0600 from creation. This patch is a verbatim diff of the
                    # worktree's uncommitted changes, so it can contain anything
                    # the working tree did — including a secret staged but not
                    # yet committed. It also SURVIVES a failed compression, when
                    # it sits in a plain directory rather than inside the
                    # archive, which is exactly when its mode is what protects it.
                    fd = os.open(
                        str(target),
                        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                        _PRIVATE_FILE_MODE,
                    )
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(patch_text)
                    _log(f"  saved uncommitted tracked changes → {trash_path}/{target.name}")
                except OSError as e:
                    _log(f"  WARN could not save {target.name} for {trash_path.name}: {e}")

        # THE REGISTRATION IS LEFT IN PLACE, DELIBERATELY.
        #
        # `git worktree prune` drops the per-worktree HEAD. For a DETACHED
        # worktree that ref is the only thing keeping its commits reachable,
        # and a branch is no safer: autonomy/executor/worktree_mgr.py deletes
        # a reaped worktree branch with `git branch -D`, which git documents
        # as removing even an unmerged one. Either way the tarball would hold
        # checked-out FILES and a pointer to commits a later GC can collect --
        # silent, delayed, and invisible until someone tries to recover.
        #
        # Pruning is therefore only safe once the archive carries its OWN copy
        # of the commit graph. That work ships separately. Until it lands this
        # module takes the tradeoff it states everywhere else: a stale
        # worktree registration is recoverable, lost commits are not. So the
        # archive is written and the registration is left for a later
        # `git worktree prune` to clear, once preservation exists to make it
        # safe.
        #
        # The visible cost is that `git worktree list` keeps naming a
        # directory that is now a tarball. Cosmetic and recoverable, and the
        # correct side of this trade to land on.
        kind = "detached HEAD" if detached else f"branch {branch}"
        # LOCK the registration. Leaving it merely unpruned is NOT an anchor:
        # `autonomy/executor/worktree_mgr.py` prunes on every task-worktree
        # creation, `contribution/pr_opener.py` prunes on every contribution run,
        # this module's own --recover used to prune, and `git gc` prunes such
        # registrations by itself past `gc.worktreePruneExpire` (default 3
        # months). Any one of those would silently de-anchor the archive and let
        # a later gc collect the commits it points at.
        #
        # MEASURED on git 2.43, all three directions:
        #   * a LOCKED registration survives `worktree prune`, `prune --expire
        #     now`, and `gc` with gc.worktreePruneExpire=now;
        #   * an UNLOCKED sibling did not — its commit was collected;
        #   * locking CLEARS the `prunable` porcelain marker, which is what keeps
        #     the zero-drop sweep from holding an archived worktree's findings
        #     open forever.
        # Locking works AFTER the directory has already moved, so there is no
        # window where a failed move leaves a live worktree locked.
        locked_anchor = _run_git(
            repo_root,
            ["worktree", "lock", "--reason", f"archived by the reaper -> {trash_path.name}; recover with --recover",
             str(wt_path)],
            timeout=15,
        )
        if locked_anchor is None:
            _log(f"  WARN could not lock the registration for {trash_path.name} — its "
                 f"history is anchored only until the next `git worktree prune`")
        _log(f"  archived {kind}; registration LOCKED as the history anchor "
             f"(pruning it needs the in-archive commit graph, which ships separately)")

        # RE-READ HEAD and rewrite the metadata. The file was placed from
        # staging BEFORE this point, so it still carries the CLASSIFICATION
        # SNAPSHOT sha -- sampled before the scan and before the archive step,
        # with a session free to commit in between. Recovery reads `commit`,
        # so the snapshot value would send it to a commit this archive never
        # captured. That is the stale-HEAD defect, and it is INDEPENDENT of
        # how history is preserved: it has to survive the bundle leaving this
        # PR, which it would not have while it lived inside the bundling
        # helper. VERIFIED that `rev-parse HEAD` still answers from a worktree
        # that has already been moved, so the fresh read is available here.
        fresh = _run_git(repo_root, ["-C", str(trash_path), "rev-parse", "HEAD"], timeout=15)
        if fresh and fresh.strip():
            meta["head"] = meta["commit"] = fresh.strip()
        with contextlib.suppress(OSError):
            final_meta.write_text(json.dumps(meta, indent=2))
            os.chmod(final_meta, _PRIVATE_FILE_MODE)

        ref_label = f"branch={branch}" if branch else f"detached {wt.get('head', '')[:8]}"

        archive = _compress_entry(trash_path, meta)
        stored = archive if archive is not None else trash_path

        meta["archive"] = str(archive) if archive else ""
        meta["stored_at"] = str(stored)
        _append_tombstone(meta)

        _log(f"TRASH {wt_path}: {ref_label} [{lane}] → {stored}")
        return True
    except subprocess.TimeoutExpired as e:
        # CAUGHT HERE, per worktree, rather than allowed to unwind. The `git tag`
        # anchor and the `git worktree prune` below it both carry timeouts, and
        # an uncaught TimeoutExpired aborts the WHOLE lifecycle run — after this
        # worktree has already been moved. Every remaining stale worktree is then
        # skipped for the day, and the moved one gets no tombstone, so the
        # greppable index silently omits an archive that exists on disk.
        # A slow git call is not a reason to stop archiving everything else.
        _log(f"ERROR trashing {wt_path}: git command timed out ({e.cmd}); "
             "the worktree may already be in the trash — check `--list-trash` "
             "before re-running")
        return False
    except (OSError, shutil.Error) as e:
        _log(f"ERROR trashing {wt_path}: {e}")
        return False


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def _recover(name: str, repo_root: Path) -> bool:
    """Resolve a trash entry by name prefix and restore it.

    A stored entry is either a directory or a ``.tar.gz``. Archives are extracted
    to a scratch directory and then handed to the SAME restore path a directory
    takes — the restore logic carries hard-won symlink-safety invariants, and
    forking it for archives would be the obvious way to lose one of them.

    An ARCHIVE is kept after a successful recovery — the restore is a copy, and
    the archive stays as the durable record. A LEGACY plain-directory entry is
    consumed instead: `_restore_from_dir` moves its contents back and removes the
    entry, because for those the trash directory IS the only copy and leaving a
    duplicate would double the disk for no benefit.
    """
    if not TRASH_DIR.exists():
        print(f"No trash directory found at {TRASH_DIR}", file=sys.stderr)
        return False

    matches = [
        stored for stored, _ in _iter_trash_entries() if stored.name.startswith(name)
    ]
    if not matches:
        print(f"No trash entry matching '{name}'", file=sys.stderr)
        return False
    if len(matches) > 1:
        # A directory and an archive sharing a base name is not real ambiguity:
        # it is the partial-rmtree state above, where the archive is the verified
        # COMPLETE copy and the directory may be missing members. Prefer the
        # archive rather than refusing both.
        archives = [m for m in matches if m.name.endswith(ARCHIVE_SUFFIX)]
        bases = {m.name[: -len(ARCHIVE_SUFFIX)] for m in archives}
        if len(archives) == 1 and all(
            m in archives or m.name in bases for m in matches
        ):
            matches = archives
    if len(matches) > 1:
        print(f"Multiple matches for '{name}':", file=sys.stderr)
        for m in matches:
            print(f"  {m.name}", file=sys.stderr)
        print("Be more specific.", file=sys.stderr)
        return False

    stored = matches[0]
    if stored.is_dir():
        return _restore_from_dir(stored, repo_root)

    # BOUNDED scratch name. Prepending `.extract-` to the full archive filename
    # can exceed the 255-byte component limit that ext4 and most Linux
    # filesystems enforce, and the failure is asymmetric in the worst way: the
    # archive is created SUCCESSFULLY and can then never be recovered, because
    # `mkdir` raises ENAMETOOLONG on a name derived from a name that already fit.
    # A digest is fixed-width, so no archive can be archivable and unrecoverable.
    # The archive's own name is what identifies it; this directory is transient
    # and only has to be unique.
    scratch = _scratch_dir_for(stored)
    try:
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True, exist_ok=True)
        # filter="data" refuses absolute paths, ".." escapes, and device nodes.
        # It also refuses OUR OWN worktrees: the standing convention symlinks
        # `secrets.env` to the repo root, and `data` raises AbsoluteLinkError on
        # the first such link, ABORTING the extraction partway — leaving a
        # directory that looks restored and is not. MEASURED 2026-09-10: 3 of the
        # 48 worktrees due for archiving carry exactly that link.
        #
        # So fall back to `tar`, which preserves absolute links instead of
        # refusing them, and rely on the containment invariants `_restore_from_dir`
        # applies per-destination anyway (lexists, realpath-under-root, symlinks
        # recreated as symlinks). Those are the real defense; `data` was a second
        # layer, and a second layer that destroys the first is not worth keeping.
        try:
            try:
                with tarfile.open(stored, "r:gz") as tf:
                    tf.extractall(str(scratch), filter="data")
            except (tarfile.AbsoluteLinkError, tarfile.LinkOutsideDestinationError):
                # BOTH link errors, not just the absolute one. `data_filter`
                # raises AbsoluteLinkError for `/abs/target` and
                # LinkOutsideDestinationError for a RELATIVE escape such as
                # `../../shared/secrets.env` — and this repo's own convention
                # produces both shapes. Catching only the first made the second
                # fall through to the outer handler and fail the whole recovery,
                # even though the fallback below is designed to recreate exactly
                # these links safely without dereferencing them.
                shutil.rmtree(scratch, ignore_errors=True)
                scratch.mkdir(parents=True, exist_ok=True)
                with tarfile.open(stored, "r:gz") as tf:
                    tf.extractall(str(scratch), filter="tar")
        except (OSError, tarfile.TarError, EOFError, TypeError, ValueError) as e:
            print(f"Failed to extract {stored}: {e}", file=sys.stderr)
            return False

        inner = [c for c in scratch.iterdir() if c.is_dir()]
        if len(inner) != 1:
            print(f"Unexpected archive layout in {stored}: {inner}", file=sys.stderr)
            return False

        ok = _restore_from_dir(inner[0], repo_root)
        if ok:
            print(f"Archive kept at {stored} (recovery copies; it does not consume)")
        return ok
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _restore_from_dir(trash_path: Path, repo_root: Path) -> bool:
    """Recreate a worktree from an UNPACKED trash directory.

    Recovery contract: recreates the worktree at its recorded ref (the branch, or
    for a detached HEAD its commit) and restores untracked files that were in the
    trash. It does NOT reconstruct the full dirty working state — uncommitted
    tracked modifications, deletions, mode-only changes, and the staged/unstaged
    split are not reapplied. This is intentional: the reaper only trashes worktrees
    already merged into main, so committed content is always recoverable from main,
    and overlaying arbitrary dirty state risks writing through checked-out symlinks.
    (Full working-state recovery would require preserving the git admin dir at trash
    time via ``git worktree move`` instead of ``shutil.move`` + ``git worktree prune``.)
    """
    meta_path = trash_path / ".trash_meta.json"

    if not meta_path.exists():
        print(f"No .trash_meta.json in {trash_path}", file=sys.stderr)
        return False

    meta = json.loads(meta_path.read_text())
    original_path = meta.get("original_path", "")
    branch = meta.get("branch", "")
    commit = meta.get("commit", "")
    detached = meta.get("detached", False)

    # Recoverable if we have a place to put it AND a ref to recreate it from
    # (a branch, or — for a detached HEAD — its commit).
    if not original_path or (not branch and not commit):
        print(f"Incomplete metadata in {meta_path}", file=sys.stderr)
        return False

    # Check if original path is already occupied
    if Path(original_path).exists():
        print(f"Original path already exists: {original_path}", file=sys.stderr)
        return False

    # RELEASE THIS ONE REGISTRATION, and only this one.
    #
    # Archiving LOCKS the registration (that lock is the history anchor), and
    # `git worktree add` refuses a path that is still registered:
    #     fatal: '<path>' is a missing but already registered worktree;
    #            use 'add -f' to override, or 'prune' or 'remove' to clear
    # `prune` is the wrong tool for that: it clears EVERY registration whose
    # directory is missing, repo-wide — and under this design every OTHER
    # archive's registration is exactly that, and is the only thing keeping its
    # commits reachable. MEASURED: recovering one archive with a repo-wide prune
    # de-anchored a sibling archive and the next `gc --prune=now` COLLECTED its
    # commit. A recovery must not be able to destroy a different archive.
    #
    # So: unlock this path, then override this path with `--force`. Measured
    # rc=0 on a still-registered missing path, with a working checkout after.
    _run_git(repo_root, ["worktree", "unlock", original_path], timeout=10)

    # Recreate the worktree: detached at its commit, or checked out on its branch.
    if detached or not branch:
        add_cmd = ["git", "worktree", "add", "--force", "--detach", original_path, commit]
    else:
        add_cmd = ["git", "worktree", "add", "--force", original_path, branch]
    result = subprocess.run(
        add_cmd,
        capture_output=True, text=True, cwd=str(repo_root), timeout=30,
    )

    if result.returncode != 0 and commit and not (detached or not branch):
        # THE BRANCH IS GONE, AND THE COMMIT IS NOT. This is the ordinary case
        # rather than an exotic one: `autonomy/executor/worktree_mgr.py` deletes
        # a task worktree's branch with `git branch -D` right after reaping it,
        # so by recovery time the branch named in the metadata routinely does not
        # exist. Falling straight through to the plain-directory move — as this
        # did — restored a tree whose `.git` file points at a pruned admin
        # directory, so `git status` inside it fails while `_recover` has already
        # reported success. A recovery that returns true and leaves an unusable
        # checkout is worse than one that fails loudly.
        #
        # What makes this recoverable is that the commit is still REACHABLE:
        # this module no longer prunes when it archives, so the per-worktree HEAD
        # that keeps the chain alive is still registered right up until the
        # `prune` above — by which point `git worktree add` is about to put the
        # commit back in a real worktree. So retry DETACHED at the recorded
        # commit, which reconstructs a real,
        # working worktree; the branch name is recoverable from there by hand
        # with one `git switch -c`, and that is stated rather than left implicit.
        retry = subprocess.run(
            ["git", "worktree", "add", "--force", "--detach", original_path, commit],
            capture_output=True, text=True, cwd=str(repo_root), timeout=30,
        )
        if retry.returncode == 0:
            print(
                f"Branch {branch!r} no longer exists; recreated as a DETACHED "
                f"worktree at {commit[:8]}. To restore the branch name: "
                f"git -C {original_path} switch -c {branch}",
                file=sys.stderr,
            )
            result = retry
        else:
            print(
                f"git worktree add --detach also failed: {retry.stderr.strip()}",
                file=sys.stderr,
            )

    if result.returncode != 0:
        # Neither the branch nor the commit could produce a worktree — fall back
        # to moving the files back. This leaves a PLAIN DIRECTORY with a dangling
        # `.git` pointer, which the message says out loud rather than reporting a
        # clean recovery.
        print(f"git worktree add failed: {result.stderr.strip()}", file=sys.stderr)

        # `git worktree add` can FAIL AFTER creating and registering the
        # destination — a `post-checkout` hook exiting non-zero is the
        # reproducible case. The directory then exists, and `shutil.move` onto an
        # existing directory places the source INSIDE it, so the whole archive
        # lands one level down as `<path>/<name>/...` while this function reports
        # success. Everything is present and nothing is where recovery said it
        # would be, which is worse than a clean failure.
        #
        # So clear the half-made destination first, and only when it is one git
        # itself just made and left EMPTY of real content. Anything else is
        # somebody's data and is refused instead.
        if Path(original_path).exists():
            leftover = [p for p in Path(original_path).iterdir() if p.name != ".git"]
            if leftover:
                print(
                    f"Refusing to move onto {original_path}: it exists and is not "
                    f"empty ({len(leftover)} entries). Recovery ABORTED rather than "
                    "nesting the archive inside it.",
                    file=sys.stderr,
                )
                return False
            try:
                shutil.rmtree(original_path)
                subprocess.run(
                    ["git", "worktree", "prune"], capture_output=True,
                    cwd=str(repo_root), timeout=30, env=_git_env(),
                )
                print(
                    f"cleared the empty directory {original_path} that the failed "
                    "worktree add left behind",
                    file=sys.stderr,
                )
            except OSError as e:
                print(f"Could not clear {original_path}: {e}", file=sys.stderr)
                return False

        print(f"Moving trash contents back to {original_path}...", file=sys.stderr)
        try:
            shutil.move(str(trash_path), original_path)
            print(f"Recovered to {original_path} (as plain directory, not git worktree)")
            return True
        except (OSError, shutil.Error) as e:
            print(f"Failed to move: {e}", file=sys.stderr)
            return False

    # Restore UNTRACKED files/symlinks that were in the trash but not recreated by
    # the fresh checkout (copy-only-missing). Reconstructing the full dirty state
    # (uncommitted tracked edits, deletions, mode-only changes, the staged/unstaged
    # split) is not attempted — see the recovery contract in the function docstring;
    # committed content is always safe in main.
    #
    # Two hard safety invariants (a recovery must NEVER write outside the worktree):
    #  1. copy-only-missing keyed on os.path.lexists (does NOT dereference), so an
    #     existing OR dangling destination symlink is left untouched — never written
    #     through to whatever it points at.
    #  2. the resolved parent of every destination must stay INSIDE the worktree
    #     root; a symlinked path component that would redirect the write outside is
    #     refused. Symlinks are recreated AS symlinks (os.symlink), never dereferenced.
    worktree_root = os.path.realpath(original_path)
    trash_files = set()
    for item in trash_path.rglob("*"):
        if ".git" in item.parts:
            continue
        if not (item.is_symlink() or item.is_file()):
            continue  # dirs are created implicitly; skip FIFOs/sockets/etc.
        rel = item.relative_to(trash_path)
        if rel.name == ".trash_meta.json":
            continue
        target = Path(original_path) / rel
        if os.path.lexists(str(target)):
            continue  # invariant 1: never overwrite / never write through a dest symlink
        parent_real = os.path.realpath(str(target.parent))
        if parent_real != worktree_root and not parent_real.startswith(worktree_root + os.sep):
            continue  # invariant 2: a symlinked path component would escape the worktree
        target.parent.mkdir(parents=True, exist_ok=True)
        if item.is_symlink():
            os.symlink(os.readlink(str(item)), str(target))  # restore the link itself
        else:
            shutil.copy2(str(item), str(target))
        trash_files.add(str(rel))

    # Clean up trash entry
    shutil.rmtree(str(trash_path))

    ref_label = f"branch: {branch}" if branch else f"detached at {commit[:8]}"
    print(f"Recovered to {original_path} ({ref_label})")
    if trash_files:
        print(f"Restored {len(trash_files)} untracked file(s) from trash")
    print(
        "Note: committed state restored at the ref plus untracked files; uncommitted "
        "tracked edits, deletions, mode changes, and staged state are NOT reapplied "
        "(committed content is always recoverable from main).",
        file=sys.stderr,
    )
    return True


# ---------------------------------------------------------------------------
# List trash
# ---------------------------------------------------------------------------


def _iter_trash_entries() -> list[tuple[Path, Path]]:
    """Every trash entry as ``(stored_path, meta_path)``.

    An entry is either a plain directory (metadata inside it) or a ``.tar.gz``
    archive (metadata in a sidecar next to it, so a listing never has to
    decompress). Sidecar files are not themselves entries.
    """
    out: list[tuple[Path, Path]] = []
    if not TRASH_DIR.exists():
        return out
    for e in sorted(TRASH_DIR.iterdir()):
        # Skip OUR OWN scratch and sidecar artifacts by name, not every
        # dot-prefixed entry. A worktree whose basename legitimately starts with
        # a dot is archived under a dot-prefixed name, and a blanket filter hid
        # both it and its `.tar.gz` — `--list-trash` omitted it and `_recover`
        # reported no match, while the archive sat there the whole time. A
        # protection that silently hides a recoverable archive is the same class
        # of failure as deleting it.
        if e.name.endswith(".meta.json") or e.name.endswith(".meta.staging"):
            continue
        if e.is_dir():
            out.append((e, e / ".trash_meta.json"))
        elif e.name.endswith(ARCHIVE_SUFFIX):
            base = Path(str(e)[: -len(ARCHIVE_SUFFIX)])
            out.append((e, _sidecar_meta_path(base)))
    return out


def _list_trash() -> None:
    """Show trash contents with age, lane, and size.

    There is no "purge in Nd" column any more, because nothing here expires. The
    columns that replace it are the ones a reader actually needs: which LANE an
    entry came from (merged content is a duplicate of main; unmerged content is
    not, and is the only copy) and how much space it occupies.
    """
    entries = _iter_trash_entries()
    if not entries:
        print("Trash is empty." if TRASH_DIR.exists() else "No trash directory found.")
        return

    now = time.time()
    total_mb = 0.0
    print(f"{'Name':<44} {'Age':>5} {'Lane':<9} {'Size':>8}  {'Branch':<28} Original Path")
    print("-" * 130)

    for stored, meta_path in entries:
        branch = original = ""
        lane = "?"
        age_days = 0.0
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                branch = meta.get("branch", "") or ("detached " + meta.get("commit", "")[:8])
                original = meta.get("original_path", "")
                lane = meta.get("lane") or "merged"
                ts = meta.get("trashed_at", "")
                if ts:
                    age_days = (now - datetime.fromisoformat(ts).timestamp()) / 86400
            except (json.JSONDecodeError, ValueError, OSError):
                pass

        if age_days == 0:
            with contextlib.suppress(OSError):
                age_days = (now - stored.stat().st_mtime) / 86400

        # DIRECTORIES ARE MEASURED TOO. An entry that stayed uncompressed —
        # because compression failed, or because it predates archiving — was
        # assigned zero bytes and shown as "dir", so the footer could report
        # "0 MB archived" while gigabytes sat in the trash. That is most
        # misleading in the case that produces it most often: compression
        # failing under storage pressure, exactly when the number is being read
        # to decide whether there is a problem.
        size_mb = 0.0
        if stored.is_file():
            with contextlib.suppress(OSError):
                size_mb = stored.stat().st_size / 1048576
        elif stored.is_dir():
            with contextlib.suppress(OSError):
                size_mb = sum(
                    f.stat().st_size for f in stored.rglob("*") if f.is_file()
                ) / 1048576
        total_mb += size_mb
        size_str = f"{size_mb:.1f}M" if stored.is_file() else f"{size_mb:.1f}M*"

        print(f"{stored.name:<44} {age_days:>4.0f}d {lane:<9} {size_str:>8}  "
              f"{branch:<28} {original}")

    print(f"\n{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}, "
          f"{total_mb:.0f} MB archived. Nothing here is deleted on a timer.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Worktree lifecycle manager")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without doing it")
    parser.add_argument("--list-trash", action="store_true",
                        help="Show trash contents")
    parser.add_argument("--recover", metavar="NAME",
                        help="Recover a trashed worktree")
    parser.add_argument("--report-json", action="store_true",
                        help="Print the classification of every worktree as JSON "
                             "and exit, changing nothing")
    parser.add_argument("--no-network", action="store_true",
                        help="Skip the one merge check that hits the network "
                             "(gh pr list). Faster, and can only ever under-report "
                             "a branch as unmerged — never the reverse")
    args = parser.parse_args()

    if args.list_trash:
        _list_trash()
        return 0

    repo_root = _repo_root()

    if args.recover:
        return 0 if _recover(args.recover, repo_root) else 1

    if args.report_json:
        try:
            results = classify_all(repo_root, allow_network=not args.no_network)
        except WorktreeScanError as e:
            # Exit non-zero and publish NOTHING. Printing an empty board here
            # would be indistinguishable from a healthy repo with no worktrees,
            # and the caller pipes this into surfaces that cannot tell the two
            # apart. The diagnostic goes to stderr so it cannot be mistaken for
            # the JSON document on stdout.
            print(f"ERROR: could not enumerate worktrees: {e}", file=sys.stderr)
            return 2
        # `--dry-run` means "change nothing", and publishing the board cache is a
        # change — to state the dashboard and the session-start block both read.
        # The two flags are independently accepted, so the combination was
        # reachable and silently mutating shared state during what the caller
        # asked to be a non-mutating inspection.
        publish = not args.no_network and not args.dry_run
        # Publish ONLY a network-complete classification. --no-network skips the
        # gh PR check, which can only ever demote a merged branch to "unmerged" —
        # harmless for the caller who asked for it, corrosive as SHARED state.
        # MEASURED on this install: the degraded run reported 42 at-risk where
        # the complete one reported 11, so publishing it would have put 31
        # false alarms into the session-start block and the dashboard, which
        # cannot tell a degraded board from a current one.
        if args.no_network:
            # STDERR, not _log. `_log` writes to stdout, and under --report-json
            # stdout is a MACHINE-READABLE channel: a prose line ahead of the
            # document makes the whole thing unparseable, which is not a cosmetic
            # problem when the dashboard board shells out to this exact flag and
            # feeds the result to a JSON parser. Verified by running it —
            # `--report-json --no-network | json.load` raised "Extra data: line 1
            # column 5" until this moved. The note still needs saying, so it goes
            # to the stream a human reads and a parser does not.
            print(
                "NOTE --no-network: printing only; the shared board cache is "
                "left as it was (a degraded classification must not become the "
                "board other surfaces read).",
                file=sys.stderr,
            )
        elif args.dry_run:
            print(
                "NOTE --dry-run: printing only; the shared board cache is left "
                "as it was.",
                file=sys.stderr,
            )
        if publish:
            _write_board_cache(results)
        print(json.dumps(results, indent=2))
        return 0

    # Normal run: archive stale worktrees into the trash. Nothing is deleted.
    _log("Worktree lifecycle check starting")

    try:
        results = classify_all(repo_root, allow_network=not args.no_network)
    except WorktreeScanError as e:
        # Do not publish, and do not reap. An empty result here would mean the
        # loop below simply does nothing, which is safe — but _write_board_cache
        # would still overwrite a good board with an empty one, telling every
        # reader that no worktrees exist.
        _log(f"ERROR could not enumerate worktrees: {e} — board NOT updated, nothing reaped")
        return 2
    _log(f"Found {len(results)} linked worktree(s)")
    # Publish BEFORE acting: if a reap below fails partway, the board still
    # describes the tree the run actually saw. NOT under --dry-run, whose whole
    # contract is that it changes nothing on disk.
    if not args.dry_run and not args.no_network:
        _write_board_cache(results)

    # The classification above is the ONLY place a fate is decided; this loop
    # just carries it out. That is deliberate — `--report-json` renders the very
    # same list, so the board cannot describe a worktree one way while the reaper
    # treats it another.
    #
    # There is exactly one destructive action available here, and it is
    # reversible: archive into the trash. The reaper does not delete. A worktree
    # can hold the only surviving trace of the session that produced it —
    # MEASURED 2026-09-10, session 59b971ca authored PR #1702 and has no
    # cc_sessions row and no transcript in any of 532 CC project directories, so
    # its commits are the whole record. A timer must not be what ends that.
    for r in results:
        if r["action"] == "none":
            _log(f"SKIP {r['path']}: {r['reason']}")
            continue
        _trash_worktree(
            r, repo_root, dry_run=args.dry_run,
            lane="merged" if r["merged"] else "unmerged",
            merge_method=r["merge_method"],
        )

    # Republish AFTER acting. The pre-flight publish above is for crash-safety;
    # left alone it would advertise `action: trash` for the next 24 hours against
    # worktrees that were archived seconds later and no longer exist. Reclassifying
    # would cost another full scan, so the acted-on rows are simply retired in
    # place — which is exactly what the dashboard needs to stop showing ghosts.
    if not args.dry_run and not args.no_network:
        for r in results:
            if r["action"] != "none" and not Path(r["path"]).exists():
                r["state"] = "archived"
                r["action"] = "none"
                r["reason"] = "archived by this run; recover with --recover"
        _write_board_cache(results)

    tally: dict[str, int] = {}
    for r in results:
        tally[r["state"]] = tally.get(r["state"], 0) + 1
    _log("States: " + ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))

    _log("Worktree lifecycle check complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
