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
import shlex
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



# Ambient REPOSITORY-LOCAL overrides, removed from every git call this module
# makes. The list is git's own: `git rev-parse --local-env-vars` on git 2.43,
# which is the set git itself clears before it runs a command in another
# repository. They beat `-C`, so with GIT_DIR or GIT_COMMON_DIR exported for
# another repository every call answers for THAT repository, GIT_INDEX_FILE sends
# an apply's staged half to a different index (MEASURED), and GIT_OBJECT_DIRECTORY
# reads and writes objects elsewhere. A redirected call usually FAILS, which is
# noisy; the dangerous case is one that SUCCEEDS against the wrong repository,
# which no later check can tell apart from a correct one.
_GIT_LOCATION_VARS = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT", "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE", "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
    "GIT_SHALLOW_FILE", "GIT_COMMON_DIR",
)


def _git_env() -> dict[str, str]:
    """The environment with git's repository-location overrides removed."""
    env = dict(os.environ)
    for var in _GIT_LOCATION_VARS:
        env.pop(var, None)
    return env


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
    """Run git, returning stdout on success and None on any failure.

    Decoded with ``surrogateescape``: git prints paths as raw bytes (always, with
    ``core.quotePath=false``), and a strict decode RAISED out of this helper on a
    non-UTF-8 name. Surrogates round-trip through ``os.fsencode`` to the same path.
    """
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, errors="surrogateescape",
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


def _git_activity_time(worktree_path: str) -> float:
    """Activity git can see that a shallow mtime walk cannot. 0.0 if unknown.

    ONE signal: the mtimes of paths git reports as modified or untracked. That
    is someone EDITING, at any depth, which is exactly what the walk below
    cannot see.

    NOT the HEAD commit timestamp, which is the obvious second signal and was
    tried first. It answers the wrong question -- when the COMMIT was made, not
    when this WORKTREE was used -- so a worktree cut from a fresh mainline
    commit and then abandoned reads as active forever and is never reclaimed.
    Nine existing tests failed on precisely that, which is the suite correctly
    refusing a signal that cannot tell a new checkout from a used one.

    Failures are absorbed and contribute 0.0, because this only ever RAISES the
    measured activity: a git call that fails degrades to the old mtime answer
    rather than making a worktree look more idle than it is.
    """
    newest = 0.0
    root = Path(worktree_path)

    # `-uall` so an untracked file deep in the tree counts; `--porcelain=v1`
    # pins the format, whose first 3 columns are status + a space.
    dirty = _run_git(root, ["status", "--porcelain=v1", "-uall"], timeout=60)
    if dirty:
        for line in dirty.splitlines()[:_DIRTY_SCAN_CAP]:
            if len(line) < 4:
                continue
            rel = line[3:]
            # A rename reads "R  old -> new"; the NEW path is the one on disk.
            if " -> " in rel:
                rel = rel.split(" -> ", 1)[1]
            try:
                newest = max(newest, (root / rel.strip('"')).lstat().st_mtime)
            except OSError:
                continue
    return newest


#: Bound on the dirty-path scan. A worktree with more changed paths than this is
#: self-evidently active, so the cap cannot make one look idle — the commit
#: timestamp above is already in hand, and every path examined only raises the
#: answer. Bounded because a first-run worktree can report tens of thousands of
#: untracked paths and this runs per worktree.
_DIRTY_SCAN_CAP = 2000


def _last_activity_time(worktree_path: str) -> float:
    """Most recent evidence of activity in the worktree.

    THE SHALLOW WALK IS UNSOUND ALONE, which is why git is consulted too. It
    samples the root and TWO levels, but modifying a file updates that file's
    mtime and never its ancestors' — and nearly all source in this repo lives
    below the sampled depth.

    MEASURED: backdate a worktree 19 days, then edit
    `src/genesis/memory/store.py`. The walk still reports 19.0 days and the
    worktree stays eligible for archiving, while `git status` on the same tree
    shows the modification. The control moves as expected — editing a depth-1
    file such as `README.md` does report 0.0 days — which is exactly what made
    the gap invisible: the obvious test passes.

    So the answer is the MAXIMUM of the walk and what git can see. Combining by
    max is what makes the addition safe: a failing or slow git call can only
    leave the old, lower answer standing, never invent idleness.
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

    return max(latest, _git_activity_time(worktree_path))


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


def _tip_on_mainline(ref: str, repo_root: Path) -> bool:
    """True when ``ref`` resolves to a commit on ``main``'s FIRST-PARENT line.

    For a ref that is already an ancestor of main, this separates "main made this
    commit itself", so the ref contributed nothing of its own, from "this commit
    reached main through a merge", so the ref had real work that got merged.
    See the call site in `_classify` for why that matters and for the one case
    it cannot tell apart (a fast-forward).

    Fails TRUE, and that direction is deliberate. The caller only acts on True
    for a DIRTY worktree, and acting means holding it for the longer unmerged
    window. An error therefore costs a week of disk, never a worktree archived
    early. The walk is one ``rev-list`` over main's first-parent history,
    which MEASURED 1,916 commits on this repo's main on 2026-09-25, and it runs
    only for an ancestor verdict past the merged threshold.
    """
    tip = _run_git(repo_root, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], timeout=15)
    tip = (tip or "").strip()
    if not tip:
        return True
    mainline = _run_git(repo_root, ["rev-list", "--first-parent", "main"], timeout=60)
    if mainline is None:
        return True
    return tip in mainline.split()


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
        # BYTES: with `core.quotePath=false` git prints a non-UTF-8 filename raw,
        # and a strict text decode raised out of this fail-closed check.
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, cwd=worktree_path, timeout=30, env=_git_env(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return True
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


# The PLUMBING commands, not porcelain `git diff`. Porcelain honours the user's
# diff configuration, and several settings make its output unappliable:
# `diff.noprefix`, `color.diff=always`, `diff.submodule=log`, `diff.context=0`
# (MEASURED: every apply failed under each), plus a `diff.<driver>.textconv`
# attribute, which printed the converter's output instead of the file's bytes
# (an uppercasing textconv saved `old -> new` as `OLD -> NEW`). `diff-index` and
# `diff-files` do none of that. A recovery patch has to carry the bytes.
# `-M`: a staged rename stays a rename (plumbing does not detect them by default).
# As a delete plus a create it no longer applies once the branch has moved the
# source, and the retry's move-aside step keys on rename destinations.
# `--ita-invisible-in-index`: an intent-to-add entry otherwise appears in the
# staged patch as an EMPTY new file and again in the unstaged one as the file with
# its content, so the second apply refused ("already exists") and took every other
# change down with it. MEASURED. With the flag it rides the unstaged patch alone.
_STAGED_DIFF = (
    "diff-index", "--cached", "--ita-invisible-in-index", "-p", "--binary", "-M", "HEAD",
)
_UNSTAGED_DIFF = ("diff-files", "-p", "--binary")


def _dirty_patches(worktree_path: str) -> tuple[bytes, bytes, bool]:
    """``(staged, unstaged, captured)`` patches of tracked uncommitted changes.

    TWO patches, because one cannot hold both states. A HEAD→worktree patch lost
    the staged version of a file whose working copy differed from it — MEASURED:
    stage ``STAGED UNIQUE``, then edit the file to ``WORKTREE UNIQUE``, and the
    only patch that existed carried the second. So the staged patch is HEAD→index
    and the unstaged one is index→worktree; the fallback recovery applies them in
    that order.

    ``captured`` is False when either diff failed. Both patches are then empty and
    the archive RECORDS the failure, so recovery cannot mistake a failed capture
    for a clean tree and delete the only copy of its edits.

    Untracked files are not in either patch; the trash keeps those as real files.
    BYTES, deliberately: a patch must round-trip exactly, so neither decoding nor
    an ``errors="replace"`` substitution is acceptable here.
    """
    out = []
    for args in (_STAGED_DIFF, _UNSTAGED_DIFF):
        try:
            result = subprocess.run(
                ["git", *args], capture_output=True, cwd=worktree_path,
                timeout=60, env=_git_env(),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
            return b"", b"", False
        if result.returncode != 0:
            return b"", b"", False
        out.append(result.stdout)
    return out[0], out[1], True


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

    # AN "ANCESTOR" VERDICT CAN BE VACUOUS. `merge-base --is-ancestor` passes for
    # any tip main can reach, and the tip of a branch that never got a commit of
    # its own is a main commit, so it always passes, with nothing merged. Such a
    # worktree can still hold uncommitted work, and that work is not in main.
    #
    # "No commits of its own" is read as "the tip sits on main's FIRST-PARENT
    # line". `main..<ref>` cannot answer it: that range is empty for EVERY
    # ancestor, including a branch whose real commits were merged. A branch
    # merged with a merge commit has its tip on a second parent, so it is off
    # main's first-parent line and stays merged. A branch that was fast-forwarded
    # into main looks the same as one that never had a commit. That ambiguity
    # fails safe: the worktree waits the longer unmerged window. It applies to
    # detached HEADs too, since `git worktree add --detach <path> main` followed
    # by edits is the same situation.
    #
    # THE ONE MISS FAILS UNSAFE, and it is stated rather than hidden. A branch or
    # detached HEAD with no commits of its own, cut from a commit that reached
    # main as a SECOND parent (e.g. a PR head that was later merged with a merge
    # commit), sits off the first-parent line, so it keeps the merged lane and
    # reaps at the shorter threshold while dirty. MEASURED in an isolated repo.
    # Structurally it is indistinguishable from a real merge-commit merge. The
    # cost is bounded: `--recover` now reapplies the saved patch.
    #
    # ONLY A DIRTY WORKTREE MOVES LANES. A clean one holds nothing main lacks, so
    # the merged clock loses nothing. Moving it would also add it to the at-risk
    # alert set for a week and report a loss that cannot happen. The rule is
    # therefore: a vacuous ancestor verdict plus uncommitted work means unmerged.
    vacuous = False
    if verdict == "ancestor" and _tip_on_mainline(ref, repo_root):
        out["dirty"] = _has_uncommitted_changes(wt_path)
        if out["dirty"]:
            verdict, vacuous = "", True
    out["merge_method"] = verdict
    out["merged"] = bool(verdict)

    if not verdict:
        # Dirty state matters MORE on this lane, not less: unmerged content may be
        # the only copy. It was previously computed only for merged worktrees.
        if out["dirty"] is None:
            out["dirty"] = _has_uncommitted_changes(wt_path)
        what = ("uncommitted changes on a tip that sits on main's own history "
                "(no commits of its own, or fast-forwarded into main)" if vacuous
                else "unmerged")
        if age_days < UNMERGED_STALE_DAYS:
            out["state"] = STATE_AT_RISK
            out["reason"] = (
                f"{what}, {age_days:.0f}d cold — reaped to trash at "
                f"{UNMERGED_STALE_DAYS}d"
            )
            return out
        out["state"] = STATE_REAP_UNMERGED
        out["action"] = "trash"
        out["reason"] = f"{what} and {age_days:.0f}d cold (>= {UNMERGED_STALE_DAYS}d)"
        return out

    out["state"] = STATE_REAP_MERGED
    if out["dirty"] is None:
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
        index_patch, patch_text, patch_captured = _dirty_patches(str(wt_path))
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
            # DERIVED FROM STATUS, not from patch presence. `_dirty_patches` runs
            # `git diff`, which sees TRACKED changes only — so a worktree
            # whose only uncommitted content is an untracked file produced an
            # empty patch and a tombstone saying there was nothing uncommitted.
            # That is the wrong answer for precisely the case archives exist for:
            # an untracked file is the one thing no branch and no commit
            # protects. The tombstone is the greppable durable index, so a wrong
            # value here is a wrong answer for as long as the archive lasts.
            "had_uncommitted_changes": _has_uncommitted_changes(str(wt_path)),
            "had_tracked_patch": bool(index_patch or patch_text),
            # PRESENT FROM THE FIRST WRITE, as "nothing of ours written yet". The
            # names are filled in once each patch lands. If a later metadata rewrite
            # fails, every copy still says this archive records its patch names, so
            # recovery never falls back to GUESSING a name for a new archive — a guess
            # that could apply the worktree's own `.dirty.patch` and report success.
            "patch_format": 2,
            "patch_file": None,
            "index_patch_file": None,
            # WHAT WAS EXPECTED, recorded apart from what got written. A recorded
            # name can only say a write succeeded; a half whose write FAILED left a
            # None that read as "nothing to save", and recovery then reported the
            # edits restored and deleted the only copy (MEASURED with a simulated
            # ENOSPC on either half).
            "patch_capture_ok": patch_captured,
            "index_patch_expected": bool(index_patch),
            "patch_expected": bool(patch_text),
            "preserved_meta": None,
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
        # LEXISTS, not exists(): a DANGLING symlink named `.trash_meta.json` is
        # a real directory entry that `Path.exists()` reports as absent (it
        # resolves the link), so the collision check missed it entirely and
        # `rename` then replaced the user's entry. `os.path.lexists` asks about
        # the entry, which is the question being asked here.
        final_meta = trash_path / ".trash_meta.json"
        if os.path.lexists(final_meta):
            preserved = None
            for n in range(1, 1000):
                candidate = trash_path / f".trash_meta.json.from-worktree-{n}"
                if not os.path.lexists(candidate):
                    preserved = candidate
                    break
            renamed = False
            if preserved is not None:
                try:
                    final_meta.rename(preserved)
                    renamed = True
                    # So a reattach can give the worktree its own file back.
                    meta["preserved_meta"] = preserved.name
                except OSError as e:
                    _log(f"  WARN {trash_path.name}: could not move its own "
                         f".trash_meta.json aside ({e})")
            if renamed:
                _log(f"  NOTE {trash_path.name} contained its own .trash_meta.json — "
                     f"kept as {preserved.name} so it survives in the archive")
            else:
                # SAY WHAT ACTUALLY HAPPENS. An earlier version of this branch
                # refused to take the name when preservation failed, on the
                # reasoning that a destroyed file is worse than an entry
                # `--recover` cannot find. That reasoning was right and the
                # implementation did not deliver it: the metadata is rewritten
                # at this same path further down, unconditionally and with
                # `write_text`, which FOLLOWS a symlink -- so refusing here
                # destroyed the file anyway, and for a dangling symlink it wrote
                # OUTSIDE the tree, which is the hole the `.dirty.patch` guard
                # above exists to close. Doing it properly means preserving
                # BEFORE the worktree is moved and skipping the reap when that
                # cannot be done, which is a larger change than this one.
                _log(f"  WARN {trash_path.name} contains a .trash_meta.json that could "
                     "not be preserved; it is being REPLACED")
        staging_meta.rename(final_meta)

        for key, base, data in (
            ("index_patch_file", _INDEX_PATCH_NAME, index_patch),
            ("patch_file", _PATCH_NAME, patch_text),
        ):
            if data:
                meta[key] = _write_recovery_patch(trash_path, base, data)

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
            ["worktree", "lock", "--reason", f"{_LOCK_PREFIX}{trash_path.name}; recover with --recover",
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


def _recover(name: str, repo_root: Path, report: dict | None = None) -> bool:
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
        return _restore_from_dir(stored, repo_root, report, stored=stored)

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

        ok = _restore_from_dir(inner[0], repo_root, report, stored=stored)
        if ok:
            print(f"Archive kept at {stored} (recovery copies; it does not consume)")
        return ok
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _restore_from_dir(
    trash_path: Path, repo_root: Path, report: dict | None = None, *, stored: Path | None = None,
) -> bool:
    """Recreate a worktree from an UNPACKED trash directory.

    Recovery contract. It recreates the worktree at its recorded ref (the branch,
    or for a detached HEAD its commit), then does two things.

    1. It REAPPLIES THE SAVED PATCHES. At archive time `_trash_worktree` saves the
       staged changes (HEAD→index) as ``.dirty.index.patch`` and the unstaged ones
       (index→worktree) as ``.dirty.patch``, each under a ``.archived-N`` name when
       the worktree owned a file of that name, and records the names it used. They
       cover tracked edits, deletions, mode changes and staged new files, as raw
       bytes (no textconv, no external diff driver). Recovery applies the staged
       patch with ``--3way`` and the unstaged one plainly on top, so every change
       comes back staged or unstaged as it was archived (see `_reapply_patches`;
       an older single-patch archive is applied the same way as an unstaged one).
       An earlier version skipped all of this because "the reaper only trashes
       worktrees already merged into main". That was false. The unmerged lane
       exists, and a branch with no commits of its own passed the merge test
       vacuously, so uncommitted work was archived and never came back. This is
       not the file overlay that was removed for writing through checked-out
       symlinks. MEASURED on git 2.43 with a patch creating ``d/new.txt`` against
       a tree where ``d`` is a symlink to an outside directory: git refused with
       "affected file 'd/new.txt' is beyond a symbolic link", and nothing was
       written outside.

       If an apply fails, for example because the branch moved on since the
       archive and the edits conflict, the worktree is reset to its clean
       checkout, the patches are left in it under their own names (or the first
       free ``.archived-N``), a loud message says the edits were NOT reapplied and
       prints the retry commands, and a legacy directory entry is kept. If the
       metadata says changes were saved but the patch holding them is missing,
       NOTHING is applied and the trash is kept, rather than guessing which file
       is the patch. When the applies succeed, no patch file is left in the tree.

    2. It restores the UNTRACKED files and symlinks that were in the trash and
       that neither the checkout nor the patch recreated (copy-only-missing).
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

    # Check if original path is already occupied (a dangling symlink counts).
    if os.path.lexists(original_path):
        print(f"Original path already exists: {original_path}", file=sys.stderr)
        return False
    if report is None:
        report = {}
    # Where the archive really lives. For a tarball, `trash_path` is a scratch
    # extraction that `_recover` deletes on the way out, so naming it in a message
    # sent the user to a path that no longer exists.
    where = stored or trash_path

    # ANOTHER GENERATION OWNS THIS PATH. When a path is archived, recovered and
    # archived again, both archives name the same registration, and its `gitdir`
    # points back at the same path. So the older archive passed every path check
    # and came back under the NEWER archive's index, reported as exact, with the
    # newer archive's history anchor removed. MEASURED. The lock reason names the
    # archive that owns the registration, so read it; and refuse outright rather
    # than rebuild, because the rebuild's `unlock` + `worktree add --force` would
    # destroy that other archive's registration.
    read, lock = _registration_lock(original_path, repo_root)
    if not read:
        print(f"Could not read the worktree registrations to check who owns "
              f"{original_path}. Nothing was changed; retry --recover.", file=sys.stderr)
        return False
    owner = None
    if lock and lock.startswith(_LOCK_PREFIX) and not lock.startswith(
            f"{_LOCK_PREFIX}{trash_path.name}; "):
        owner = lock[len(_LOCK_PREFIX):].split("; ", 1)[0]
    if owner is not None:
        print(f"{original_path} is registered to a NEWER archive, {owner}; recovering "
              f"{where} here would replace that archive's index and remove its history "
              f"anchor. Nothing was changed. Recover {owner} instead, or recover this "
              "one to a different path by hand.", file=sys.stderr)
        return False

    # REATTACH FIRST. Archiving keeps the worktree's registration (locked, as the
    # history anchor), and with it the worktree's own INDEX. Moving the archived
    # directory back under that registration restores the tree EXACTLY — staged
    # and unstaged split, intent-to-add and skip-worktree bits, modes, untracked
    # files — which no patch can carry (MEASURED: the patch route loses the
    # content of a skip-worktree file and the intent-to-add flag). Rebuilding with
    # `git worktree add --force` instead REPLACES that registration and its index,
    # destroying the one complete copy of the staged state before rebuilding a
    # lossy one. The rebuild below is kept for archives whose registration is gone.
    admin = _reattach_target(trash_path, original_path, repo_root)
    if admin is not None:
        outcome = _reattach(trash_path, meta, repo_root, original_path, admin, report, where)
        if outcome == "done":
            return True
        if outcome in ("stuck", "abort"):
            return False
        # "fallback": the tree is back where it was; rebuild from the patches.

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

    def _relock() -> None:
        """Put the history anchor back after a FAILED recovery.

        THE LOCK IS WHAT KEEPS THE ARCHIVED COMMITS REACHABLE. Unlocking above is
        only safe because `git worktree add --force` is about to take the path
        over; if that does not happen, an unlocked registration is one
        `git worktree prune` or `git gc` away from being removed, and the
        archive's commits become collectable with the tarball still sitting in
        the trash. Every failure path below therefore restores it before
        returning, and the reason says why it is back.
        """
        _run_git(
            repo_root,
            ["worktree", "lock", "--reason",
             f"{_LOCK_PREFIX}{trash_path.name}; recovery did not "
             "complete, still recoverable with --recover",
             original_path],
            timeout=15,
        )

    # Recreate the worktree: detached at its commit, or checked out on its branch.
    if detached or not branch:
        add_cmd = ["git", "worktree", "add", "--force", "--detach", original_path, commit]
    else:
        add_cmd = ["git", "worktree", "add", "--force", original_path, branch]
    result = subprocess.run(
        add_cmd,
        capture_output=True, text=True, cwd=str(repo_root), timeout=30, env=_git_env(),
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
        # The commit usually survives the branch: this worktree's HEAD reflog
        # keeps it reachable until that reflog expires (gc.reflogExpireUnreachable,
        # 30 days by default). The lock keeps the REGISTRATION, not the commit, so
        # past that a gc can collect it and this retry fails loudly below. While
        # it holds, retry DETACHED at the recorded commit, which reconstructs a
        # real, working worktree; the branch name is recoverable from there by
        # hand with one `git switch -c`, and that is stated rather than implicit.
        retry = subprocess.run(
            ["git", "worktree", "add", "--force", "--detach", original_path, commit],
            capture_output=True, text=True, cwd=str(repo_root), timeout=30, env=_git_env(),
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
                _relock()
                return False
            try:
                shutil.rmtree(original_path)
                # NO `git worktree prune` HERE. It is repo-wide, and while every
                # OTHER archive's registration survives it (locked registrations
                # are not pruned -- measured), THIS path's registration was
                # unlocked a few lines above precisely so `worktree add` could
                # take it over. Pruning now would remove the one anchor that is
                # currently unprotected, and the fallback move below would then
                # restore a directory whose commits nothing keeps reachable.
                # Leaving the registration in place also means the move below
                # restores a worktree git still knows about, rather than a plain
                # directory with a dangling pointer.
                print(
                    f"cleared the empty directory {original_path} that the failed "
                    "worktree add left behind",
                    file=sys.stderr,
                )
            except OSError as e:
                print(f"Could not clear {original_path}: {e}", file=sys.stderr)
                _relock()
                return False

        print(f"Moving trash contents back to {original_path}...", file=sys.stderr)
        try:
            shutil.move(str(trash_path), original_path)
            print(f"Recovered to {original_path} (as plain directory, not git worktree)")
            # Git does not recognise this tree, so the recovery is not complete.
            report["incomplete"] = True
            return True
        except (OSError, shutil.Error) as e:
            print(f"Failed to move: {e}", file=sys.stderr)
            # The archive is still in the trash and still needs its anchor.
            _relock()
            return False

    # REAPPLY THE SAVED PATCH, BEFORE the untracked copy below. The order matters
    # for two reasons. A staged new file sits in the trash as a plain file AND in
    # the patch as a creation, so copying first would make the apply fail with
    # "already exists". And a failed apply is rolled back with `reset --hard`,
    # which is only harmless while the tree holds nothing but the checkout.
    index_src, patch_src, missing = _saved_patches_in(trash_path, meta)
    ours = [p for p in (index_src, patch_src) if p is not None]
    patch_outcome = (
        None if (missing or not ours)
        else _reapply_patches(index_src, patch_src, original_path)
    )

    # Restore UNTRACKED files/symlinks that were in the trash but not recreated by
    # the fresh checkout or the patch (copy-only-missing). The reaper's own patch
    # is excluded: when it applied it has done its job, and when it did not it is
    # placed below under a name that cannot displace the worktree's own files.
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
        rel = item.relative_to(trash_path)
        if _is_reaper_owned(rel):
            continue
        if not (item.is_symlink() or item.is_file()):
            continue  # dirs are created implicitly; skip FIFOs/sockets/etc.
        if item in ours:
            continue  # the reaper's patches, handled above and below, never as user files
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

    _restore_preserved_meta(Path(original_path), meta)

    # Patches that did NOT apply are kept IN the worktree, after the untracked
    # copy so they can never take the name of a file the worktree owned.
    failed = patch_outcome is not None and not patch_outcome[0]
    kept: list[Path] = []
    keep_errors: list[str] = []
    if failed:
        for src in ours:
            base = _INDEX_PATCH_NAME if src is index_src else _PATCH_NAME
            dest, err = _keep_unapplied_patch(src, original_path, base)
            if dest is not None:
                kept.append(dest)
            else:
                keep_errors.append(f"{src.name}: {err}")

    # VERIFY BEFORE DELETING. A legacy directory entry is the only complete copy
    # of its worktree, so it is deleted only when every regular file and symlink
    # in it (apart from the reaper's own) came back byte-for-byte. A patch that
    # merged onto a moved branch, a file the copy refused for safety, or anything
    # the patches could not carry leaves a difference, and the entry is kept.
    skip = {p.resolve() for p in ours}
    mismatched = _unrestored(trash_path, original_path, skip, meta)
    legacy_dir = trash_path.parent == TRASH_DIR
    if legacy_dir and (failed or missing or mismatched):
        print(f"Trash entry left in place because it still holds content the "
              f"recovered tree does not match: {trash_path}", file=sys.stderr)
    else:
        shutil.rmtree(str(trash_path))
    if failed or missing or mismatched:
        report["incomplete"] = True

    ref_label = f"branch: {branch}" if branch else f"detached at {commit[:8]}"
    print(f"Recovered to {original_path} ({ref_label})")
    if trash_files:
        print(f"Restored {len(trash_files)} untracked file(s) from trash")
    bar = "!" * 72
    if missing:
        print("\n".join([
            bar,
            f"UNCOMMITTED EDITS WERE NOT REAPPLIED to {original_path}",
            "  The archive records tracked uncommitted changes, but the patch that "
            "should hold them is missing or incomplete, so nothing was applied rather "
            "than guessing which file is the patch.",
            f"  The archive is kept; inspect it by hand: {where}",
            bar,
        ]), file=sys.stderr)
    elif patch_outcome is None:
        pass  # nothing uncommitted was tracked, so there was nothing to reapply
    elif patch_outcome[0]:
        head = (_run_git(Path(original_path), ["rev-parse", "HEAD"], timeout=15) or "").strip()
        if patch_outcome[3] and head == commit:
            print("Reapplied the uncommitted tracked changes saved at archive time, "
                  "staged and unstaged as recorded. Patches do not carry intent-to-add "
                  "or skip-worktree flags.")
        elif head != commit:
            print("Reapplied the uncommitted tracked changes saved at archive time "
                  "onto a branch that has MOVED since (it was at "
                  f"{commit[:8]}). Review them with `git diff HEAD`.")
        else:
            print("Reapplied the uncommitted tracked changes saved at archive time "
                  "from a single-patch archive, as UNSTAGED changes: that format never "
                  "recorded the staged/unstaged split.")
    else:
        _, detail, rolled_back, _exact = patch_outcome
        lines = [
            bar,
            f"UNCOMMITTED EDITS WERE NOT REAPPLIED to {original_path}",
            f"  git apply failed: {detail or '(no output)'}",
        ]
        if rolled_back:
            lines.append("  The worktree was reset to its clean checkout; nothing was half-applied.")
        else:
            lines.append(
                "  AND the rollback to the clean checkout FAILED: the worktree may hold a "
                "PARTIAL application with conflict markers. Check `git status` before "
                "doing anything else."
            )
        for dest in kept:
            lines.append(f"  Saved edits kept in {dest}")
        for err in keep_errors:
            lines.append(f"  Could not place {err}; the archive still holds it.")
        lines.extend(_retry_lines(kept, original_path, trash_files))
        lines.append(bar)
        # Any path in `detail` or a kept name may carry surrogate escapes; never let
        # the message that says where the edits went be the thing that raises.
        text = "\n".join(lines).encode("utf-8", "backslashreplace").decode("utf-8")
        print(text, file=sys.stderr)
    if mismatched:
        shown = mismatched[:10]
        more = f" (and {len(mismatched) - 10} more)" if len(mismatched) > 10 else ""
        print("\n".join([
            bar,
            f"THE RECOVERED TREE DIFFERS FROM THE ARCHIVE in {len(mismatched)} path(s){more}:",
            *(f"  {p}" for p in shown),
            f"  Compare against {where} before discarding it.",
            bar,
        ]).encode("utf-8", "backslashreplace").decode("utf-8"), file=sys.stderr)
    return True


def _retry_lines(kept: list[Path], worktree: str, restored: set[str]) -> list[str]:
    """Commands that retry the kept patches by hand, in order.

    Every file a patch touches that was ALSO restored from the archive as an
    untracked copy must be moved aside first: the rollback removed it, the
    copy-only-missing step put the archived copy back, and `git apply` refuses to
    create a file that exists. Moving it aside — never excluding it — is the only
    retry that is right in every shape. `--exclude` skips the whole change for that
    path, which MEASURED loses a rename (source stays tracked, destination stays
    untracked) and would equally drop a later edit to a file an earlier patch
    creates. The moved-aside copies keep the final working-tree content until the
    retry has succeeded.
    """
    if not kept:
        return []
    q_wt = shlex.quote(str(worktree))
    touched: set[str] = set()
    unknown = False
    for dest in kept:
        paths = _patch_paths(dest, worktree)
        if paths is None:
            unknown = True
        else:
            touched |= paths
    aside = sorted(touched & restored)
    out = ["  Retry by hand, in this order:"]
    if aside:
        # `test ! -e` first, so running the block twice can never overwrite a
        # `.restored` copy — those hold the only final working-tree content.
        moves = " && ".join(
            f"test ! -e {_shell_word(p + '.restored')} && "
            f"mv -- {_shell_word(p)} {_shell_word(p + '.restored')}"
            for p in aside
        )
        out.append(f"    cd {q_wt} && {moves}")
    for dest in kept:
        mode = " --3way" if dest.name.startswith(_INDEX_PATCH_NAME) else ""
        out.append(f"    git -C {q_wt} apply {' '.join(_APPLY_EXACT)}{mode} "
                   f"{shlex.quote(dest.name)}")
    if aside:
        out.append("  The .restored copies hold the final working-tree content; delete "
                   "them once the retry has succeeded.")
    if unknown:
        out.append("  (If git reports that a file already exists, a file the patch creates "
                   "was restored from the archive: move it aside the same way first.)")
    out.append("  If an apply still CONFLICTS (the branch moved on), `git apply --3way` "
               "merges it and stages the result, or `git apply --reject` writes the "
               "hunks that do not fit to *.rej files.")
    return out


def _shell_word(text: str) -> str:
    """``text`` as ONE bash word naming exactly the same bytes.

    A filename that is not valid UTF-8 arrives here with surrogate escapes. Printed
    as-is it raises on a strict UTF-8 stream; printed lossily the command would name
    a different file. Bash's ``$'…'`` form with ``\\xHH`` escapes carries the exact
    bytes in plain ASCII.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raw = os.fsencode(text)
        body = "".join(
            chr(b) if 32 <= b < 127 and chr(b) not in "'\\" else f"\\x{b:02x}" for b in raw
        )
        return f"$'{body}'"
    return shlex.quote(text)


# Every lock the reaper writes starts with this, followed by the archive's entry
# name and "; ". `_restore_from_dir` reads it back to tell generations apart.
_LOCK_PREFIX = "archived by the reaper -> "
_PATCH_NAME = ".dirty.patch"
_INDEX_PATCH_NAME = ".dirty.index.patch"
_ALT = ".archived-"


def _reattach_target(trash_path: Path, original_path: str, repo_root: Path) -> Path | None:
    """The preserved registration an archived tree still belongs to, or None.

    Every check must hold, or the tree is rebuilt instead: the tree's ``.git`` is
    a regular FILE naming an admin directory; that directory sits under this
    repository's own ``worktrees/`` and has a HEAD; and its ``gitdir`` points back
    at ``original_path``. The last one is what makes the registration THIS tree's
    rather than one a later ``git worktree add`` reused.
    """
    dotgit = trash_path / ".git"
    if dotgit.is_symlink() or not dotgit.is_file():
        return None
    try:
        text = dotgit.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not text.startswith("gitdir:"):
        return None
    raw = text[len("gitdir:"):].strip()
    admin = Path(raw) if os.path.isabs(raw) else Path(original_path) / raw
    common = _run_git(repo_root, ["rev-parse", "--path-format=absolute", "--git-common-dir"],
                      timeout=15)
    if not common or not common.strip():
        return None
    admin = Path(os.path.realpath(admin))
    if admin.parent != Path(os.path.realpath(Path(common.strip()) / "worktrees")):
        return None
    if not (admin / "HEAD").is_file():
        return None
    try:
        pointed = (admin / "gitdir").read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None
    back = Path(pointed) if os.path.isabs(pointed) else admin / pointed
    if os.path.realpath(back) != os.path.realpath(Path(original_path) / ".git"):
        return None
    return admin


def _place_tree(src: Path, dest: Path) -> bool:
    """Put the tree at ``src`` at ``dest``; True when it was MOVED, False when COPIED.

    A rename when both sit on one filesystem. Across filesystems a move is a copy
    followed by deleting the source, and `shutil.move` does both: when that delete
    failed part way (a read-only directory is enough), the source was left PARTIAL
    while the only complete copy was the one at ``dest``, which the caller then
    removed as a failed copy. MEASURED: files lost everywhere and the entry left
    unrecoverable. So across filesystems this COPIES and never touches the source.
    A copy that fails is removed before the error propagates, so no caller can
    mistake a partial copy for the tree.
    """
    try:
        os.rename(src, dest)
        return True
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    try:
        shutil.copytree(src, dest, symlinks=True)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)  # our partial copy; the source is intact
        raise
    return False


def _unplace_tree(dest: Path, src: Path, moved: bool) -> bool:
    """Undo `_place_tree`; True when ``src`` holds the tree again and ``dest`` is gone."""
    if moved:
        try:
            os.rename(dest, src)
        except OSError:
            return False
        return True
    # A copy the source still duplicates, so removing it loses nothing.
    shutil.rmtree(dest, ignore_errors=True)
    return not os.path.lexists(dest)


def _registration_lock(original_path: str, repo_root: Path) -> tuple[bool, str | None]:
    """``(read, reason)`` for the registration of ``original_path``.

    Read from the admin directories themselves, the way `_reattach_target` finds
    them: the registration is the one whose ``gitdir`` points back at
    ``original_path/.git``. ``reason`` is None when there is no such registration
    or it is not locked; ``read`` is False only when the question could not be
    answered at all.
    """
    common = _run_git(repo_root, ["rev-parse", "--path-format=absolute", "--git-common-dir"],
                      timeout=15)
    if not common or not common.strip():
        return False, None
    want = os.path.realpath(Path(original_path) / ".git")
    try:
        admins = list((Path(common.strip()) / "worktrees").iterdir())
    except FileNotFoundError:
        return True, None
    except OSError:
        return False, None
    for admin in admins:
        try:
            pointed = (admin / "gitdir").read_text(errors="surrogateescape").strip()
        except OSError:
            continue
        back = pointed if os.path.isabs(pointed) else str(admin / pointed)
        if os.path.realpath(back) != want:
            continue
        try:
            return True, (admin / "locked").read_text(errors="surrogateescape")
        except FileNotFoundError:
            return True, None
        except OSError:
            return False, None
    return True, None


def _restore_preserved_meta(dest: Path, meta: dict) -> None:
    """Give the worktree its own ``.trash_meta.json`` back under its real name.

    Archiving renames a worktree's own file of that name aside, so the reaper's can
    take the name; this is the reverse. Only the RECORDED aside name is renamed, and
    only when nothing holds the real name.
    """
    preserved = meta.get("preserved_meta")
    meta_file = dest / ".trash_meta.json"
    if (isinstance(preserved, str) and preserved.startswith(".trash_meta.json.from-worktree-")
            and "/" not in preserved and os.path.lexists(dest / preserved)
            and not os.path.lexists(meta_file)):
        try:
            (dest / preserved).rename(meta_file)
        except OSError as e:
            print(f"Could not give {dest / preserved} back its name .trash_meta.json ({e}); "
                  "it is the worktree's own file, set aside at archive time.", file=sys.stderr)


def _reattach(
    trash_path: Path, meta: dict, repo_root: Path, original_path: str, admin: Path,
    report: dict, where: Path,
) -> str:
    """Move the archived tree back under its registration.

    Returns ``done``; ``fallback`` when git answered that the registration is NOT
    this tree's, so rebuilding is right; ``abort`` when the tree could not be placed
    or git could not be asked, so nothing is rebuilt (the rebuild replaces the
    registration, and with it the only complete copy of the staged state, so it is
    reserved for a registration PROVEN unusable); ``stuck`` when the tree could be
    moved neither in nor back. Only ``done`` removes anything, and only the
    reaper's own files.
    """
    dest = Path(original_path)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        moved = _place_tree(trash_path, dest)
    except (OSError, shutil.Error) as e:
        if os.path.lexists(dest):
            print(f"Could not place the archived tree ({e}), and part of a copy remains at "
                  f"{dest}. The archive is intact at {where}; remove {dest} and retry.",
                  file=sys.stderr)
            report["incomplete"] = True
            return "stuck"
        print(f"Could not place the archived tree ({e}). Nothing was changed; the archive "
              f"is intact at {where}. Retry --recover once the cause is fixed.",
              file=sys.stderr)
        return "abort"

    _run_git(repo_root, ["worktree", "unlock", original_path], timeout=10)

    # A deleted branch leaves HEAD naming a ref that no longer exists. The lock keeps
    # the REGISTRATION, not the commit: once the branch is gone, only this worktree's
    # HEAD reflog keeps it reachable, and that expires (gc.reflogExpireUnreachable,
    # 30 days by default). While the commit is still here, detach at it and keep the
    # index; once gc has collected it, the check below says so.
    commit = meta.get("commit", "")
    branch = meta.get("branch", "")
    note = ""
    if (
        commit
        and _run_git(dest, ["rev-parse", "--verify", "-q", "HEAD"], timeout=15) is None
        and _run_git(repo_root, ["cat-file", "-e", f"{commit}^{{commit}}"], timeout=15) is not None
        and _run_git(dest, ["update-ref", "--no-deref", "HEAD", commit], timeout=15) is not None
    ):
        note = (f"Branch {branch!r} no longer exists; the worktree is DETACHED at "
                f"{commit[:8]}. To restore the name: git -C {original_path} switch -c {branch}")

    # VERIFY THE BINDING, and nothing else. `git status` was the check once: it walks
    # the whole tree, so on a slow disk it timed out and a healthy reattach went to
    # the rebuild, destroying the index (MEASURED); and it decoded names strictly,
    # so a raw non-UTF-8 name raised after the move and the unlock (MEASURED). What
    # proves the tree is back under its own registration is that git resolves it to
    # that admin directory. Timeout: this reads two small files; 15 s is generous.
    git_dir = _run_git(dest, ["rev-parse", "--absolute-git-dir"], timeout=15)
    if git_dir is None or os.path.realpath(git_dir.strip()) != os.path.realpath(admin):
        _run_git(repo_root, ["worktree", "lock", "--reason",
                             f"{_LOCK_PREFIX}{trash_path.name}; reattach did not verify, "
                             "still recoverable with --recover",
                             original_path], timeout=15)
        if not _unplace_tree(dest, trash_path, moved):
            print(f"THE PLACED TREE AT {dest} DID NOT VERIFY and could not be taken back "
                  f"out. The archive is intact at {where}; remove or inspect {dest} by hand "
                  "before retrying.", file=sys.stderr)
            report["incomplete"] = True
            return "stuck"
        if git_dir is None:
            print("git could not be asked whether the archived tree belongs to its "
                  f"registration. Nothing was changed; the archive is intact at {where}. "
                  "Retry --recover.", file=sys.stderr)
            return "abort"
        print("The archived tree does not belong to its registration any more; rebuilding "
              "it from its saved patches instead.", file=sys.stderr)
        return "fallback"

    # The reaper's own files go; the worktree's own files stay. Only names the
    # archive RECORDED are removed, since a guessed name could be the user's file.
    leftovers = []
    for key, base in (("patch_file", _PATCH_NAME), ("index_patch_file", _INDEX_PATCH_NAME)):
        name = meta.get(key)
        if isinstance(name, str) and _is_reaper_name(name, base):
            ours = dest / name
            if ours.is_file() and not ours.is_symlink():
                try:
                    ours.unlink()
                except OSError:
                    leftovers.append(ours)
    meta_file = dest / ".trash_meta.json"
    if meta_file.is_file() and not meta_file.is_symlink():
        try:
            meta_file.unlink()
        except OSError:
            leftovers.append(meta_file)
    _restore_preserved_meta(dest, meta)
    if not moved and trash_path.parent == TRASH_DIR:
        print(f"The trash entry was COPIED, not moved (it is on another filesystem), and "
              f"is kept at {trash_path}. Delete it once you have checked the worktree.",
              file=sys.stderr)

    report["mode"] = "reattached"
    print(f"Reattached {original_path} to its preserved registration: index, "
          "staged/unstaged split and untracked files are exactly as archived.")
    if leftovers:
        print("Could not remove the reaper's own file(s) from the tree: "
              + ", ".join(str(x) for x in leftovers), file=sys.stderr)
    head = (_run_git(dest, ["rev-parse", "--verify", "-q", "HEAD"], timeout=15) or "").strip()
    if not head:
        report["incomplete"] = True
        gone = commit and _run_git(
            repo_root, ["cat-file", "-e", f"{commit}^{{commit}}"], timeout=15) is None
        if gone:
            print(f"BUT ITS COMMIT {commit[:8]} NO LONGER EXISTS in this repository: the "
                  "branch was deleted and gc has since collected the commit. The files and "
                  "the index are back, with no commit under them, so `git status` compares "
                  f"them against an empty history. Recover {commit[:8]} from a clone or a "
                  "remote that still has it before committing here.", file=sys.stderr)
        else:
            print(f"BUT ITS HEAD DOES NOT RESOLVE. The files and the index are back; point "
                  f"HEAD at the recorded commit with: git -C {original_path} update-ref "
                  f"--no-deref HEAD {commit}", file=sys.stderr)
    elif note:
        print(note, file=sys.stderr)
    elif commit and head != commit:
        report["incomplete"] = True
        print(f"The branch has MOVED since archiving (was {commit[:8]}, now {head[:8]}). "
              "The tree and index are as archived, so `git status` also shows the "
              f"difference between {commit[:8]} and {head[:8]} as changes here, and a "
              "commit now would record that difference as this tree's own work, undoing "
              "the branch's move. Rebase or merge the archived work first.", file=sys.stderr)
    if ("patch_format" not in meta and "patch_file" not in meta
            and meta.get("had_tracked_patch")):
        print("This archive predates recorded patch names, so any `.dirty.patch*` file "
              "in the tree was left alone: it may be the reaper's saved patch, whose "
              "edits the tree already holds.", file=sys.stderr)
    return "done"


def _is_reaper_owned(rel: Path) -> bool:
    """The entry's own ``.git`` pointer and metadata file, at the ROOT only.

    A ``.git`` or ``.trash_meta.json`` further down belongs to the worktree: an
    untracked nested clone, or a file of that name. Skipping those at any depth
    deleted them with a legacy entry whose check had just passed. MEASURED.
    """
    parts = rel.parts
    return bool(parts) and (parts[0] == ".git" or rel == Path(".trash_meta.json"))


def _unrestored(trash_path: Path, original_path: str, skip: set[Path],
                meta: dict | None = None) -> list[str]:
    """Paths in the trash entry whose recovered counterpart differs, relative.

    Regular files are compared by bytes and executable bit, symlinks by target.
    Empty directories are not compared: git does not track them and the copy does
    not recreate them. The worktree's own set-aside ``.trash_meta.json`` is compared
    under the name it is restored to.
    """
    out: list[str] = []
    root = Path(original_path)
    preserved = (meta or {}).get("preserved_meta")
    for item in trash_path.rglob("*"):
        rel = item.relative_to(trash_path)
        if _is_reaper_owned(rel):
            continue
        if not (item.is_symlink() or item.is_file()):
            continue
        if item.resolve() in skip:
            continue
        target = root / rel
        if isinstance(preserved, str) and rel == Path(preserved):
            target = root / ".trash_meta.json"
        try:
            if item.is_symlink():
                same = target.is_symlink() and os.readlink(target) == os.readlink(item)
            else:
                same = (not target.is_symlink() and target.is_file()
                        and target.read_bytes() == item.read_bytes()
                        and (target.stat().st_mode & 0o111) == (item.stat().st_mode & 0o111))
        except OSError:
            same = False
        if not same:
            out.append(str(rel))
    return sorted(out)


def _is_reaper_name(name: object, base: str) -> bool:
    """True for ``base`` or ``base.archived-N`` — the only names the reaper writes."""
    if not isinstance(name, str):
        return False
    return name == base or (name.startswith(base + _ALT) and name[len(base) + len(_ALT):].isdigit())


def _write_recovery_patch(trash_path: Path, base: str, data: bytes) -> str | None:
    """Write one recovery patch into a trash entry; its name, or None if none was written.

    COLLISION-CHECKED, because the name is not reserved. A worktree may contain an
    untracked file of that name, and an unconditional write would destroy it — in the
    archive AND in the worktree, since the archive is made from the moved directory.
    Falling back to ``base.archived-N`` keeps both, and the name used is RECORDED in
    the metadata so recovery never has to guess which file is ours.

    LEXISTS, not exists: a DANGLING symlink under the name reads as absent to
    ``Path.exists()``, and the open below would then follow it and create its target
    anywhere on the filesystem (MEASURED). O_NOFOLLOW|O_EXCL is the guard AT the
    write, independent of that check. 0600 from creation: the patch is a verbatim
    diff and can hold a secret staged but not yet committed.

    A WRITE THAT FAILS PART-WAY IS REMOVED. Left behind, a truncated file under a
    reaper name reads at recovery as a user's file at best and as our patch at
    worst; either way the complete edits are misrepresented.
    """
    target: Path | None = trash_path / base
    if os.path.lexists(target):
        target = None
        for n in range(1, 1000):
            alt = trash_path / f"{base}{_ALT}{n}"
            if not os.path.lexists(alt):
                target = alt
                break
        if target is not None:
            _log(f"  NOTE {trash_path.name} already contains {base} — saving the "
                 f"recovery patch as {target.name} so the original survives")
    if target is None:
        _log(f"  WARN could not find a free name for {base} in {trash_path.name}; the "
             "uncommitted changes are still inside the archive, but no patch was written")
        return None
    try:
        fd = os.open(
            str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            _PRIVATE_FILE_MODE,
        )
    except OSError as e:
        _log(f"  WARN could not save {target.name} for {trash_path.name}: {e}")
        return None
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except OSError as e:
        with contextlib.suppress(OSError):
            target.unlink()
        _log(f"  WARN could not save {target.name} for {trash_path.name}: {e} "
             "(the partial file was removed)")
        return None
    _log(f"  saved uncommitted tracked changes → {trash_path}/{target.name}")
    return target.name


def _saved_patches_in(trash_path: Path, meta: dict) -> tuple[Path | None, Path | None, bool]:
    """``(staged_patch, unstaged_patch, missing)`` inside an unpacked trash entry.

    ``missing`` is True when the archive says tracked changes were saved but the
    patch holding them cannot be identified or is absent. Recovery then applies
    NOTHING and keeps the trash: applying a wrongly identified file stages
    somebody else's changes and reports success.

    * An archive that records its names (``patch_format`` 2, or any
      ``patch_file`` key) is AUTHORITATIVE: a recorded name is used as written,
      after checking it is a name the reaper writes, and a None means nothing of
      ours was written for that half.
    * An older archive records no names (MEASURED 2026-09-25: 0 of 204 metadata
      files on this install carry one). It has a single HEAD→worktree patch, and
      ``had_tracked_patch`` False means there is none of ours at all. Otherwise
      ours is the highest-numbered ``.dirty.patch.archived-N`` if any exists (ours
      took the first free number), else ``.dirty.patch``. This guess is WRONG in
      one legacy shape and cannot be made right without the metadata: a worktree
      that owned a numbered file but no ``.dirty.patch`` had ours saved as
      ``.dirty.patch``, and the guess picks the user's file. If that file is not a
      valid patch for this tree the apply fails and is rolled back.

    Only a regular file qualifies. The reaper writes with O_NOFOLLOW, so a symlink
    under any of these names is the user's and is never fed to ``git apply``.
    """
    def _regular(p: Path) -> Path | None:
        return p if (not p.is_symlink() and p.is_file()) else None

    if meta.get("patch_format") == 2 or "patch_file" in meta:
        if meta.get("patch_capture_ok") is False and meta.get("had_uncommitted_changes"):
            return None, None, True  # the capture failed; nothing records the edits
        found: list[Path | None] = []
        missing = False
        for key, base, expected_key in (
            ("index_patch_file", _INDEX_PATCH_NAME, "index_patch_expected"),
            ("patch_file", _PATCH_NAME, "patch_expected"),
        ):
            name = meta.get(key)
            path = (_regular(trash_path / name)
                    if isinstance(name, str) and _is_reaper_name(name, base) else None)
            expected = meta.get(expected_key)
            if expected is None:  # single-patch archive written with patch_file only
                expected = key == "patch_file" and bool(meta.get("had_tracked_patch"))
            if expected and path is None:
                missing = True
            found.append(path)
        return found[0], found[1], missing

    if "had_tracked_patch" not in meta:
        # An archive from before patches were recorded at all. A `.dirty.patch` in
        # it may be ours or the user's, and nothing says which; with none, there is
        # nothing missing — the archive simply never had one.
        guess = _regular(trash_path / _PATCH_NAME)
        return None, guess, False
    if meta.get("had_tracked_patch") is False:
        return None, None, False
    numbered = []
    for p in trash_path.glob(_PATCH_NAME + _ALT + "*"):
        suffix = p.name[len(_PATCH_NAME + _ALT):]
        if suffix.isdigit():
            numbered.append((int(suffix), p))
    guess = _regular(max(numbered)[1]) if numbered else _regular(trash_path / _PATCH_NAME)
    return None, guess, guess is None


# The patches ignore user DIFF config; these make the apply ignore user APPLY
# config too. `apply.whitespace=fix` silently stripped trailing whitespace from the
# reapplied edits, and `=error` refused them (both MEASURED); `apply.ignoreWhitespace`
# would let context match lines that differ. A recovery writes the saved bytes.
_APPLY_EXACT = ("--whitespace=nowarn", "--no-ignore-whitespace")


def _git_apply(args: list[str], patch: Path, worktree: str) -> tuple[bool, str]:
    """Run one ``git apply``; ``(ok, what git said)``, selected rather than cut."""
    try:
        result = subprocess.run(
            ["git", "apply", *_APPLY_EXACT, *args, str(patch)],
            capture_output=True, text=True, errors="replace",
            cwd=worktree, timeout=600, env=_git_env(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return False, str(e)
    # git repeats "Falling back to direct application..." once per file, which says
    # nothing, and a large patch can report on hundreds of paths. Keep whole lines
    # and say how many were omitted.
    said = [ln.strip() for ln in (result.stderr + result.stdout).splitlines()
            if ln.strip() and ln.strip() != "Falling back to direct application..."]
    if len(said) > 10:
        said = [*said[:10], f"<{len(said) - 10} more lines omitted>"]
    return result.returncode == 0, "; ".join(said)


def _reapply_patches(
    staged: Path | None, unstaged: Path | None, worktree: str,
) -> tuple[bool, str, bool, bool]:
    """Apply the saved patches to a freshly recreated worktree.

    Returns ``(applied, detail, rolled_back, exact_split)``.

    With a staged patch: it goes in with ``--3way``, which lands it in the index,
    and then the unstaged patch goes in with a plain apply, which touches only the
    working tree — so each change comes back in the state it was archived in.
    Without one (nothing was staged, or an older single-patch archive, whose base
    is the same HEAD): a plain apply first, which is atomic and keeps the changes
    unstaged, and ``--3way`` only if that does not fit, which can merge onto a moved
    branch but stages the result.

    ``--3way`` is not atomic. MEASURED on git 2.43 against a conflicting base:
    exit 1, conflict markers in the conflicting file, and the patch's OTHER hunks
    applied anyway; ``--check --3way`` exits 0 on that same patch, so it cannot
    serve as a dry run. Any failure is therefore rolled back with ``reset --hard
    HEAD``. That is safe HERE because the tree was created moments ago and holds
    only its checkout plus whatever these applies wrote; the caller runs this
    before copying any untracked file back in.

    Timeout: generous because a ``--binary`` patch of a large worktree can be big,
    and bounded so a hung git cannot hold ``--recover`` open indefinitely. A timeout
    is handled like any other failure.
    """
    if staged is not None:
        ok, detail = _git_apply(["--3way"], staged, worktree)
        if ok and unstaged is not None:
            ok, detail = _git_apply([], unstaged, worktree)
        exact = True
    else:
        ok, detail = _git_apply([], unstaged, worktree)
        exact = ok
        if not ok:
            ok, detail = _git_apply(["--3way"], unstaged, worktree)
    if ok:
        return True, detail, False, exact
    rolled_back = _run_git(Path(worktree), ["reset", "-q", "--hard", "HEAD"], timeout=120) is not None
    return False, detail, rolled_back, exact


def _patch_paths(patch: Path, worktree: str) -> set[str] | None:
    """Every path the patch touches — both sides of a rename — or None.

    Read from git itself (``apply --numstat -z``), not from the patch text. With
    ``-z`` each record is ``added<TAB>deleted<TAB>path<NUL>``; a rename or copy has an
    empty path field followed by ``src<NUL>dst<NUL>``. BYTES, decoded the way the
    filesystem layer decodes names (``os.fsdecode``): git emits raw filename bytes,
    and a strict UTF-8 decode raised on a non-UTF-8 name and aborted recovery while
    it was printing the retry instructions. None when git cannot read the patch.
    """
    try:
        result = subprocess.run(
            ["git", "apply", "--numstat", "-z", str(patch)],
            capture_output=True, cwd=worktree, timeout=120, env=_git_env(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    toks = result.stdout.split(b"\0")
    paths: set[str] = set()
    i = 0
    while i < len(toks):
        parts = toks[i].split(b"\t", 2)
        i += 1
        if len(parts) < 3:
            continue
        if parts[2]:
            paths.add(os.fsdecode(parts[2]))
        else:
            paths.update(os.fsdecode(t) for t in toks[i:i + 2] if t)
            i += 2
    return paths


def _keep_unapplied_patch(
    patch: Path, worktree: str, base: str = _PATCH_NAME,
) -> tuple[Path | None, str]:
    """Copy a patch that did not apply into the worktree, where a human will look.

    ``base`` if that name is free, otherwise the first free ``base.archived-N``,
    the same fallback the archive side uses. O_EXCL|O_NOFOLLOW so it can neither
    replace nor write through an entry that appeared first, and 0600 because the
    patch can hold anything the working tree did. A copy that fails part-way is
    removed, so the worktree never shows a truncated patch as the saved edits.
    Returns ``(path, "")`` or ``(None, error)``.
    """
    try:
        data = patch.read_bytes()
    except OSError as e:
        return None, str(e)
    root = Path(worktree)
    for name in [base, *(f"{base}{_ALT}{n}" for n in range(1, 1000))]:
        dest = root / name
        if os.path.lexists(dest):
            continue
        try:
            fd = os.open(
                str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                _PRIVATE_FILE_MODE,
            )
        except FileExistsError:
            continue
        except OSError as e:
            return None, str(e)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except OSError as e:
            with contextlib.suppress(OSError):
                dest.unlink()
            return None, str(e)
        return dest, ""
    return None, "no free name for the patch in the worktree"


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
        # 2 when the worktree came back but its uncommitted edits did not, so a
        # script or a dispatched session cannot read a partial recovery as success.
        report: dict = {}
        if not _recover(args.recover, repo_root, report):
            return 1
        return 2 if report.get("incomplete") else 0

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
