#!/usr/bin/env python3
"""Worktree lifecycle manager — automated stale cleanup with trash bin.

Identifies and trashes worktrees that are:
1. Not in use by any process (no /proc/*/cwd inside them)
2. Not locked (`git worktree lock`), with no in-progress Git operation
   (rebase/merge/cherry-pick/revert/bisect), and not containing a nested
   worktree — each signals deliberate/in-progress work to preserve
3. Inactive for 14+ days (no file modifications)
4. Merged into main — branch merged (PR on GitHub) OR zero unique commits.
   A detached-HEAD worktree (no branch) is judged by whether its HEAD commit
   is already in main; without this it would default to branch "unknown" and
   never be reaped.

Trashed worktrees are recoverable for 7 days (see _recover for the recovery
contract). After that, permanently deleted along with their branches.

Usage:
    worktree_lifecycle.py                    # Run: trash stale, purge old trash
    worktree_lifecycle.py --dry-run          # Show what would happen
    worktree_lifecycle.py --list-trash       # Show trash contents with age
    worktree_lifecycle.py --recover <name>   # Recover a trashed worktree

Run daily by the genesis-disk-hygiene.timer systemd unit (via
scripts/disk_hygiene.sh, alongside disk_reclaim.py). Also runnable by hand.

Stdlib-only (no genesis package imports). Uses gh CLI for PR status.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

STALE_DAYS = 14
TRASH_RETENTION_DAYS = 7
TRASH_DIR = Path.home() / ".genesis" / "worktree-trash"
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
    """Return PIDs with CWD inside dir_path (excluding self + parent)."""
    exclude = {os.getpid(), os.getppid()}
    pids: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in exclude:
            continue
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
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True, cwd=str(repo_root), timeout=10,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    worktrees: list[dict] = []
    current: dict = {}
    is_first = True

    for line in result.stdout.splitlines():
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


def _is_merged(ref: str, repo_root: Path, *, is_branch: bool = True) -> bool:
    """Check whether a worktree's work is already in ``main``.

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

    Returns False on any error (fail-safe: keep the worktree).
    """
    # Method 1: git merge-base (branch ref or raw SHA) — the ONLY method for a
    # detached HEAD (see docstring: patch-id/PR methods are unsafe for a bare SHA).
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ref, "main"],
            capture_output=True, cwd=str(repo_root), timeout=10,
        )
        if result.returncode == 0:
            return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    if not is_branch:
        return False  # detached HEAD: ancestor-only, no patch-id/PR fallbacks

    # Method 2: gh pr list (handles squash merges) — branch heads only.
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--head", ref, "--state", "merged",
             "--limit", "1", "--json", "number"],
            capture_output=True, text=True, cwd=str(repo_root), timeout=30,
        )
        if result.returncode == 0:
            prs = json.loads(result.stdout)
            if prs:
                return True
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
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
                return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return False


# Per-worktree admin-dir markers for an in-progress Git operation. Each lives
# under the worktree's OWN git dir (git resolves them per-worktree), so a paused
# rebase/merge/cherry-pick/revert/bisect in one worktree is detectable there.
_IN_PROGRESS_MARKERS = (
    "rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD",
    "REVERT_HEAD", "BISECT_LOG", "sequencer",
)


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


# ---------------------------------------------------------------------------
# Trash operations
# ---------------------------------------------------------------------------


def _trash_worktree(
    wt: dict, repo_root: Path, *, dry_run: bool = False,
) -> bool:
    """Move a worktree to the trash directory.

    Returns True if trashed (or would be trashed in dry-run).
    """
    wt_path = Path(wt["path"])
    branch = wt.get("branch", "")
    detached = wt.get("detached", False)
    name = wt_path.name
    date_str = datetime.now(UTC).strftime("%Y%m%d")
    trash_name = f"{name}-{date_str}"
    trash_path = TRASH_DIR / trash_name

    # Avoid name collisions
    counter = 1
    while trash_path.exists():
        trash_path = TRASH_DIR / f"{name}-{date_str}-{counter}"
        counter += 1

    if dry_run:
        _log(f"WOULD TRASH {wt_path}: → {trash_path}")
        return True

    try:
        TRASH_DIR.mkdir(parents=True, exist_ok=True)

        # Write metadata to staging file BEFORE the move. If the move
        # fails we just have a harmless orphan file. If the process
        # dies after the move but before metadata lands inside the
        # trash dir, we still have it at the staging path.
        meta = {
            "original_path": str(wt_path),
            "branch": branch,
            "commit": wt.get("head", ""),
            "detached": detached,
            "trashed_at": datetime.now(UTC).isoformat(),
        }
        staging_meta = TRASH_DIR / f".{trash_path.name}.meta.staging"
        staging_meta.write_text(json.dumps(meta, indent=2))

        # Move worktree to trash
        shutil.move(str(wt_path), str(trash_path))

        # Move staging metadata into the trash entry
        final_meta = trash_path / ".trash_meta.json"
        staging_meta.rename(final_meta)

        # Clean git's worktree registration
        subprocess.run(
            ["git", "worktree", "prune"],
            capture_output=True, cwd=str(repo_root), timeout=10,
        )

        ref_label = f"branch={branch}" if branch else f"detached {wt.get('head', '')[:8]}"
        _log(f"TRASH {wt_path}: {ref_label}, recoverable for {TRASH_RETENTION_DAYS}d at {trash_path}")
        return True
    except (OSError, shutil.Error) as e:
        _log(f"ERROR trashing {wt_path}: {e}")
        return False


def _purge_old_trash(repo_root: Path, *, dry_run: bool = False) -> None:
    """Permanently delete trash entries older than TRASH_RETENTION_DAYS."""
    if not TRASH_DIR.exists():
        return

    now = time.time()

    for entry in sorted(TRASH_DIR.iterdir()):
        if not entry.is_dir():
            continue

        meta_path = entry / ".trash_meta.json"
        trashed_at: float | None = None

        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                trashed_at_str = meta.get("trashed_at", "")
                if trashed_at_str:
                    dt = datetime.fromisoformat(trashed_at_str)
                    trashed_at = dt.timestamp()
            except (json.JSONDecodeError, ValueError, OSError):
                pass

        # Fallback: use the directory's mtime
        if trashed_at is None:
            try:
                trashed_at = entry.stat().st_mtime
            except OSError:
                continue

        age_days = (now - trashed_at) / 86400
        if age_days < TRASH_RETENTION_DAYS:
            continue

        # Old enough to purge
        branch = ""
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                branch = meta.get("branch", "")
            except (json.JSONDecodeError, OSError):
                pass

        if dry_run:
            _log(f"WOULD PURGE {entry.name}: trashed {age_days:.0f}d ago"
                 + (f", branch={branch}" if branch else ""))
            continue

        try:
            shutil.rmtree(str(entry))
            _log(f"PURGE {entry.name}: trashed {age_days:.0f}d ago")
        except OSError as e:
            _log(f"ERROR purging {entry.name}: {e}")
            continue

        # Delete the branch if it still exists
        if branch:
            subprocess.run(
                ["git", "branch", "-D", branch],
                capture_output=True, cwd=str(repo_root), timeout=10,
            )


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def _recover(name: str, repo_root: Path) -> bool:
    """Recover a worktree from the trash.

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
    if not TRASH_DIR.exists():
        print(f"No trash directory found at {TRASH_DIR}", file=sys.stderr)
        return False

    # Find matching trash entry
    matches = [e for e in TRASH_DIR.iterdir()
               if e.is_dir() and e.name.startswith(name)]
    if not matches:
        print(f"No trash entry matching '{name}'", file=sys.stderr)
        return False
    if len(matches) > 1:
        print(f"Multiple matches for '{name}':", file=sys.stderr)
        for m in matches:
            print(f"  {m.name}", file=sys.stderr)
        print("Be more specific.", file=sys.stderr)
        return False

    trash_path = matches[0]
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

    # Recreate the worktree: detached at its commit, or checked out on its branch.
    if detached or not branch:
        add_cmd = ["git", "worktree", "add", "--detach", original_path, commit]
    else:
        add_cmd = ["git", "worktree", "add", original_path, branch]
    result = subprocess.run(
        add_cmd,
        capture_output=True, text=True, cwd=str(repo_root), timeout=30,
    )

    if result.returncode != 0:
        # Branch might not exist — fall back to just moving files back
        print(f"git worktree add failed: {result.stderr.strip()}", file=sys.stderr)
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


def _list_trash() -> None:
    """Show trash contents with age and metadata."""
    if not TRASH_DIR.exists():
        print("No trash directory found.")
        return

    entries = sorted(TRASH_DIR.iterdir())
    if not entries:
        print("Trash is empty.")
        return

    now = time.time()
    print(f"{'Name':<40} {'Age':>6} {'Branch':<30} {'Original Path'}")
    print("-" * 120)

    for entry in entries:
        if not entry.is_dir():
            continue

        meta_path = entry / ".trash_meta.json"
        branch = ""
        original = ""
        age_days = 0.0

        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                branch = meta.get("branch", "")
                original = meta.get("original_path", "")
                trashed_at_str = meta.get("trashed_at", "")
                if trashed_at_str:
                    dt = datetime.fromisoformat(trashed_at_str)
                    age_days = (now - dt.timestamp()) / 86400
            except (json.JSONDecodeError, ValueError, OSError):
                pass

        if age_days == 0:
            with contextlib.suppress(OSError):
                age_days = (now - entry.stat().st_mtime) / 86400

        purge_in = TRASH_RETENTION_DAYS - age_days
        age_str = f"{age_days:.0f}d"
        status = f" (purge in {purge_in:.0f}d)" if purge_in > 0 else " (OVERDUE)"

        print(f"{entry.name:<40} {age_str:>6}{status}  {branch:<30} {original}")


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
    args = parser.parse_args()

    if args.list_trash:
        _list_trash()
        return 0

    repo_root = _repo_root()

    if args.recover:
        return 0 if _recover(args.recover, repo_root) else 1

    # Normal run: trash stale worktrees + purge old trash
    _log("Worktree lifecycle check starting")

    worktrees = _list_worktrees(repo_root)
    _log(f"Found {len(worktrees)} linked worktree(s)")

    for wt in worktrees:
        wt_path = wt.get("path", "")
        branch = wt.get("branch")  # None for a detached HEAD
        head = wt.get("head", "")

        if not wt_path or not Path(wt_path).exists():
            _log(f"SKIP {wt_path}: directory does not exist (ghost entry)")
            continue

        # Operator protection: never reap a locked worktree or one with a paused
        # Git operation (rebase/merge/…). Both signal deliberate/in-progress work;
        # reaping would discard a `git worktree lock` or destroy sequencer state.
        if wt.get("locked"):
            _log(f"SKIP {wt_path}: locked (git worktree lock)")
            continue
        if _has_in_progress_op(wt_path):
            _log(f"SKIP {wt_path}: in-progress or unresolvable git state (rebase/merge/broken .git)")
            continue

        # Never reap a worktree that CONTAINS another linked worktree: trashing
        # moves the whole directory, dragging the nested worktree (and bypassing
        # ITS locked/in-progress protections, which are checked on its own row).
        wt_prefix = wt_path.rstrip("/") + os.sep
        nested = [o["path"] for o in worktrees
                  if o.get("path") and o["path"] != wt_path
                  and o["path"].rstrip("/").startswith(wt_prefix)]
        if nested:
            _log(f"SKIP {wt_path}: contains nested worktree(s): {', '.join(nested[:3])}")
            continue

        # Check 1: active processes
        pids = _find_processes_in_dir(wt_path)
        if pids:
            pid_str = ", ".join(str(p) for p in pids[:5])
            _log(f"SKIP {wt_path}: active processes (PIDs: {pid_str})")
            continue

        # Check 2: recent activity
        last_activity = _last_activity_time(wt_path)
        age_days = (time.time() - last_activity) / 86400
        if age_days < STALE_DAYS:
            _log(f"SKIP {wt_path}: activity {age_days:.0f}d ago (< {STALE_DAYS}d)")
            continue

        # Check 3: work already merged into main. A detached HEAD (no branch)
        # is judged by its HEAD commit, not the missing branch name.
        ref, is_branch = (branch, True) if branch else (head, False)
        if not ref or not _is_merged(ref, repo_root, is_branch=is_branch):
            label = f"branch '{branch}'" if branch else f"detached HEAD {head[:8]}"
            _log(f"SKIP {wt_path}: {label} not merged")
            continue

        # All checks passed — trash it
        _trash_worktree(wt, repo_root, dry_run=args.dry_run)

    # Purge old trash entries
    _purge_old_trash(repo_root, dry_run=args.dry_run)

    _log("Worktree lifecycle check complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
