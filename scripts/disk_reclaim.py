#!/usr/bin/env python3
"""Regenerable-cache reclamation — frees disk by clearing caches that rebuild.

Companion to ``worktree_lifecycle.py`` (which reaps merged worktrees). This
handles the OTHER disk accumulators: regenerable caches and stale output.

Three tiers, cleared in order until the disk is comfortable:

  CHEAP   — always cleared on ``--apply``. Rebuild is free/near-free
            (package-manager download caches).
  MEDIUM  — cleared only when disk usage >= ``--if-above`` PCT. Rebuild is
            expensive (a CBM / GitNexus reindex), so we only pay it under
            genuine pressure.
  SYSTEM  — best-effort, needs sudo + write access to /var. Silently skipped
            when unavailable (e.g. inside the hardened systemd service, which
            runs with NoNewPrivileges + ProtectSystem=strict).

NEVER touches: the git repo tree, .venv, data/, config/, secrets, browser
profiles (they hold logins), ~/tau3-bench, embedding_cache (re-embedding costs
API calls), or any user file. Every target is matched against a strict
allowlist and re-validated at delete time.

Usage:
    disk_reclaim.py                         # dry-run (report only) — default
    disk_reclaim.py --apply                 # clear CHEAP tier
    disk_reclaim.py --apply --if-above 90   # also clear MEDIUM tier if >= 90%

Note: ~/.genesis/output is intentionally NOT touched — those are generated
deliverables (not regenerable cache), managed by the user, not this tool.

Designed to run:
  - Daily via genesis-disk-hygiene.timer (HOME-scoped, hardened, no sudo)
  - Reactively via the remediation registry at >= 90% (full privileges)

Stdlib-only (no genesis imports) so it runs even when the server is unhealthy.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

HOME = Path.home()
LOG_DIR = HOME / ".genesis" / "logs"
MOUNT = "/"

# Paths that must NEVER be inside a reclaim target, even by accident. A target
# is rejected if it equals or contains (is an ancestor of) any of these.
_PROTECTED = {
    HOME,
    HOME / "genesis",
    HOME / "genesis" / ".venv",
    HOME / "genesis" / "data",
    HOME / "genesis" / "src",
    HOME / "genesis" / ".git",
    HOME / ".genesis",
    HOME / ".genesis" / "camoufox-profile",
    HOME / ".genesis" / "browser-profile",
    HOME / ".genesis" / "embedding_cache",
    HOME / "tau3-bench",
    HOME / ".ssh",
    Path("/"),
}


@dataclass(frozen=True)
class CacheTarget:
    """A directory whose entire contents are safe to delete (it regenerates)."""

    description: str
    path: Path
    tier: str  # "cheap" | "medium"


# Directories cleared wholesale (they are pure regenerable caches). Each is
# removed with shutil.rmtree; the owning tool recreates it on next use.
_CACHE_TARGETS: list[CacheTarget] = [
    CacheTarget("npm content cache", HOME / ".npm" / "_cacache", "cheap"),
    CacheTarget("npm logs", HOME / ".npm" / "_logs", "cheap"),
    CacheTarget("uv download cache", HOME / ".cache" / "uv", "cheap"),
    CacheTarget("pip cache", HOME / ".cache" / "pip", "cheap"),
    CacheTarget("codebase-memory-mcp index (forces reindex)",
                HOME / ".cache" / "codebase-memory-mcp", "medium"),
    CacheTarget("code-graph cache (forces reindex)",
                HOME / ".cache" / "code-graph", "medium"),
    CacheTarget("GitNexus index (forces reanalyze)",
                HOME / "genesis" / ".gitnexus", "medium"),
]


# ─── Helpers ─────────────────────────────────────────────────────────────


def _log(msg: str) -> None:
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    line = f"{ts} {msg}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / "disk_reclaim.log").open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


def _disk_pct(mount: str = MOUNT) -> float:
    """Return percent used of the filesystem holding *mount*.

    Uses statvfs f_bavail (space available to non-root) so this MATCHES
    ``genesis.observability.health.probe_disk`` and ``df`` Use%. This alignment
    is load-bearing: the reactive ``--if-above`` gate must fire at the same
    threshold the remediation ``probe_disk`` critical uses, or the medium-tier
    purge would no-op exactly when remediation triggered it. (shutil.disk_usage
    counts reserved blocks as free, under-reporting usage by ~5%.)
    """
    st = os.statvfs(mount)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - free
    return (used / total * 100) if total > 0 else 0.0


def _dir_size(path: Path) -> int:
    """Sum of file sizes under *path* (no symlink following)."""
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            fp = Path(root) / name
            try:
                if not fp.is_symlink():
                    total += fp.stat().st_size
            except OSError:
                continue
    return total


def _is_safe_target(path: Path) -> bool:
    """True only if *path* is safe to rmtree.

    Rejects symlinks, non-existent paths, and anything that equals or is an
    ancestor of a PROTECTED path (deleting it would nuke protected data).
    A target may live *under* a protected dir (e.g. ~/.genesis/output), but it
    must not BE or CONTAIN one.
    """
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if path.is_symlink():
        _log(f"REFUSE {path}: is a symlink")
        return False
    for prot in _PROTECTED:
        try:
            prot_r = prot.resolve()
        except OSError:
            continue
        if resolved == prot_r:
            _log(f"REFUSE {path}: equals protected path {prot}")
            return False
        # target must not be an ancestor of a protected path
        if prot_r != resolved and prot_r.is_relative_to(resolved):
            _log(f"REFUSE {path}: contains protected path {prot}")
            return False
    return True


# ─── Reclaim operations ──────────────────────────────────────────────────


def _clear_cache(target: CacheTarget, *, apply: bool) -> int:
    """Remove a cache directory. Returns bytes actually reclaimed.

    Resilient to per-file permission errors (e.g. stray root-owned entries left
    by a past ``sudo npm``): deletes everything it can, skips what it can't, and
    reports the *actual* delta rather than assuming the whole tree was removed.
    Package caches tolerate partial deletion — a cache miss just triggers a
    redownload / rebuild.
    """
    path = target.path
    if not path.exists():
        return 0
    if not _is_safe_target(path):
        return 0
    size = _dir_size(path)
    if size == 0:
        return 0
    if not apply:
        _log(f"WOULD CLEAR [{target.tier}] {target.description}: "
             f"{_fmt_bytes(size)} ({path})")
        return size

    skipped: list[str] = []

    def _onexc(_func, failed_path, exc):
        # Called by rmtree for each entry it cannot remove; record + continue.
        skipped.append(str(failed_path))

    # Python 3.12 uses onexc; keep onerror as a fallback for older interpreters.
    try:
        shutil.rmtree(path, onexc=_onexc)  # type: ignore[call-arg]
    except TypeError:
        def _onerror(_func, failed_path, _exc_info):
            skipped.append(str(failed_path))
        shutil.rmtree(path, onerror=_onerror)

    remaining = _dir_size(path) if path.exists() else 0
    reclaimed = max(size - remaining, 0)
    if skipped:
        _log(f"CLEARED [{target.tier}] {target.description}: "
             f"{_fmt_bytes(reclaimed)} reclaimed, {len(skipped)} entries skipped "
             f"(permission) ({path})")
    else:
        _log(f"CLEARED [{target.tier}] {target.description}: "
             f"{_fmt_bytes(reclaimed)} ({path})")
    return reclaimed


def _system_clean(*, apply: bool) -> int:
    """Best-effort /var reclaim via sudo. Skipped silently if unavailable.

    Returns bytes reclaimed (measured via disk delta, approximate).
    """
    # Only attempt if passwordless sudo is available (fails fast, no prompt).
    try:
        probe = subprocess.run(
            ["sudo", "-n", "true"], capture_output=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    if probe.returncode != 0:
        _log("SKIP system tier: no passwordless sudo (expected in hardened service)")
        return 0

    before = shutil.disk_usage(MOUNT).free
    cmds = [
        ["sudo", "-n", "apt-get", "clean"],
        ["sudo", "-n", "journalctl", "--vacuum-size=100M"],
    ]
    for cmd in cmds:
        if not apply:
            _log(f"WOULD RUN: {' '.join(cmd)}")
            continue
        try:
            subprocess.run(cmd, capture_output=True, timeout=60)
            _log(f"RAN: {' '.join(cmd)}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            _log(f"ERROR running {' '.join(cmd)}: {exc}")
    if not apply:
        return 0
    reclaimed = shutil.disk_usage(MOUNT).free - before
    return max(reclaimed, 0)


# ─── Main ────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerable-cache reclamation")
    parser.add_argument("--apply", action="store_true",
                        help="Actually delete (default is dry-run)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only (default; overrides --apply)")
    parser.add_argument("--if-above", type=float, default=101.0, metavar="PCT",
                        help="Also clear MEDIUM tier when disk usage >= PCT "
                             "(default 101 = never)")
    parser.add_argument("--fail-above", type=float, default=101.0, metavar="PCT",
                        help="Exit non-zero if usage is still >= PCT after "
                             "applying (signals 'cleaned but still critical' to "
                             "the remediation registry so it escalates)")
    parser.add_argument("--system", action="store_true",
                        help="Also attempt best-effort /var clean via sudo")
    args = parser.parse_args()

    apply = args.apply and not args.dry_run
    pct = _disk_pct()
    include_medium = pct >= args.if_above

    _log(f"Disk reclaim starting (usage={pct:.1f}%, mode={'APPLY' if apply else 'DRY-RUN'}, "
         f"medium_tier={'ON' if include_medium else 'OFF'} @>={args.if_above})")

    total = 0
    for target in _CACHE_TARGETS:
        if target.tier == "medium" and not include_medium:
            if target.path.exists():
                _log(f"HOLD [medium] {target.description}: disk {pct:.1f}% "
                     f"< {args.if_above}% threshold ({target.path})")
            continue
        total += _clear_cache(target, apply=apply)

    if args.system:
        total += _system_clean(apply=apply)

    verb = "Reclaimed" if apply else "Reclaimable"
    new_pct = _disk_pct()
    _log(f"Disk reclaim complete: {verb} {_fmt_bytes(total)} "
         f"(disk {pct:.1f}% -> {new_pct:.1f}%)")

    # Signal "cleaned but still critical" so the remediation registry escalates.
    if apply and new_pct >= args.fail_above:
        _log(f"STILL CRITICAL: disk {new_pct:.1f}% >= {args.fail_above}% after "
             f"reclaim — escalating (exit 2)")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
