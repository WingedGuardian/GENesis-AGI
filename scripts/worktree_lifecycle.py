#!/usr/bin/env python3
"""Worktree lifecycle manager — automated stale cleanup with trash bin.

Identifies and trashes worktrees that are:
1. Not in use by any process (no /proc/*/cwd inside them)
2. Inactive for 14+ days (no file modifications)
3. Branch is merged (PR merged on GitHub) OR has zero unique commits

Trashed worktrees are recoverable for 7 days. After that, permanently
deleted along with their branches.

Usage:
    worktree_lifecycle.py                    # Run: trash stale, purge old trash
    worktree_lifecycle.py --dry-run          # Show what would happen
    worktree_lifecycle.py --list-trash       # Show trash contents with age
    worktree_lifecycle.py --recover <name>   # Recover a trashed worktree

Designed for daily cron:
    0 4 * * * .venv/bin/python scripts/worktree_lifecycle.py

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

    Returns list of dicts with keys: path, branch, head.
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


def _branch_is_merged(branch: str, repo_root: Path) -> bool:
    """Check if a branch's PR is merged on GitHub.

    Three methods tried in order:
    1. git merge-base --is-ancestor (fast, works for non-squash merges)
    2. gh pr list --head <branch> --state merged (handles squash merges)
    3. Zero unique commits vs main (git cherry)

    Returns False on any error (fail-safe: keep the worktree).
    """
    # Method 1: git merge-base
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", branch, "main"],
            capture_output=True, cwd=str(repo_root), timeout=10,
        )
        if result.returncode == 0:
            return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Method 2: gh pr list (handles squash merges)
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--head", branch, "--state", "merged",
             "--limit", "1", "--json", "number"],
            capture_output=True, text=True, cwd=str(repo_root), timeout=30,
        )
        if result.returncode == 0:
            prs = json.loads(result.stdout)
            if prs:
                return True
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass

    # Method 3: zero unique commits
    try:
        result = subprocess.run(
            ["git", "cherry", "main", branch],
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
    branch = wt.get("branch", "unknown")
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

        # Write metadata before moving (in case move fails)
        meta = {
            "original_path": str(wt_path),
            "branch": branch,
            "commit": wt.get("head", ""),
            "trashed_at": datetime.now(UTC).isoformat(),
        }

        # Move worktree to trash
        shutil.move(str(wt_path), str(trash_path))

        # Write metadata into trash entry
        meta_path = trash_path / ".trash_meta.json"
        meta_path.write_text(json.dumps(meta, indent=2))

        # Clean git's worktree registration
        subprocess.run(
            ["git", "worktree", "prune"],
            capture_output=True, cwd=str(repo_root), timeout=10,
        )

        _log(f"TRASH {wt_path}: branch={branch}, recoverable for {TRASH_RETENTION_DAYS}d at {trash_path}")
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
    """Recover a worktree from the trash."""
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

    if not original_path or not branch:
        print(f"Incomplete metadata in {meta_path}", file=sys.stderr)
        return False

    # Check if original path is already occupied
    if Path(original_path).exists():
        print(f"Original path already exists: {original_path}", file=sys.stderr)
        return False

    # Try to recreate worktree from the branch
    result = subprocess.run(
        ["git", "worktree", "add", original_path, branch],
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

    # Worktree recreated from branch — now copy any uncommitted files
    # that were in the trash but not in the branch
    trash_files = set()
    for item in trash_path.rglob("*"):
        if item.is_file() and ".git" not in item.parts:
            rel = item.relative_to(trash_path)
            if rel.name == ".trash_meta.json":
                continue
            target = Path(original_path) / rel
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(item), str(target))
                trash_files.add(str(rel))

    # Clean up trash entry
    shutil.rmtree(str(trash_path))

    print(f"Recovered to {original_path} (branch: {branch})")
    if trash_files:
        print(f"Restored {len(trash_files)} uncommitted file(s) from trash")
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
        branch = wt.get("branch", "unknown")

        if not wt_path or not Path(wt_path).exists():
            _log(f"SKIP {wt_path}: directory does not exist (ghost entry)")
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

        # Check 3: branch merged
        if not _branch_is_merged(branch, repo_root):
            _log(f"SKIP {wt_path}: branch '{branch}' not merged")
            continue

        # All checks passed — trash it
        _trash_worktree(wt, repo_root, dry_run=args.dry_run)

    # Purge old trash entries
    _purge_old_trash(repo_root, dry_run=args.dry_run)

    _log("Worktree lifecycle check complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
