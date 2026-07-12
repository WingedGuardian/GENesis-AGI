"""Git-repository health detection — the outage-class detector (F.1).

The thin-pool outage zeroed ``.git/config``, ``packed-refs``, and ~30 loose
objects with ZERO detection, silently disabling the guardian's ``REVERT_CODE``
recovery lever (which needs healthy local git). These probes catch that class:

- ``check_git_cheap`` — fast structural plumbing (config/HEAD/refs/packed-refs)
  + a rootfs read-only probe, safe to run on every awareness tick.
- ``check_git_deep`` — ``git fsck --connectivity-only``, a slower reachability
  scan for a daily job.

Both write a verdict to the shared mount (``<shared>/guardian/git_health.json``)
so the host guardian can enrich its own alert with the failure detail.

Never raises into the caller — a health probe that crashes the tick is worse
than the condition it detects.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from genesis.env import genesis_home, repo_root

logger = logging.getLogger(__name__)

# Local git plumbing returns in milliseconds on a healthy disk; the ONLY way it
# takes longer is a wedged / read-only filesystem — which is precisely the
# condition being detected. 10 s bounds the awareness tick without ever killing
# legitimate work; a timeout is itself reported as a failure, never swallowed.
_CHEAP_TIMEOUT_S = 10
# `git fsck --connectivity-only` on this repo's ~118 MB .git is seconds-to-a-
# minute healthy; 900 s gives 10x headroom on an IO-pressured pool while bounding
# the daily job. A timeout is emitted as a signal, not a silent skip.
_DEEP_TIMEOUT_S = 900

_VERDICT_DIR = "guardian"
_VERDICT_FILE = "git_health.json"


@dataclass(frozen=True)
class GitHealthReport:
    """Outcome of a git-health probe. ``ok`` iff ``failures`` is empty."""

    ok: bool
    failures: list[str]
    details: dict
    kind: str  # "cheap" | "deep"
    checked_at: str

    def to_json(self) -> dict:
        return {
            "version": 1,
            "ok": self.ok,
            "failures": list(self.failures),
            "kind": self.kind,
            "checked_at": self.checked_at,
            "details": self.details,
        }


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _run_git(repo: Path, *args: str, timeout: float) -> tuple[int, str, str]:
    """Run a git plumbing command; never raises. Returns (rc, stdout, stderr).

    rc = -1 on timeout (the wedged-fs signal), -2 on any other exec failure.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as exc:  # git missing, repo path gone, etc.
        return -2, "", str(exc)


def _mount_is_readonly(path: Path, mounts_text: str | None = None) -> bool:
    """True if the filesystem containing ``path`` is mounted read-only.

    Longest-prefix match over /proc/mounts (mirrors awareness/loop._fs_type_for),
    reading the mount OPTIONS field. ``mounts_text`` is injectable for tests.
    Returns False when it can't be determined — never false-alarms on a probe
    failure (the write-probe is the authoritative RO signal).
    """
    try:
        if mounts_text is None:
            mounts_text = Path("/proc/mounts").read_text()
    except OSError:
        return False
    target = str(path)
    best, ro = "", False
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        mnt, opts = parts[1], parts[3]
        if (target == mnt or target.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
            best = mnt
            ro = "ro" in opts.split(",")
    return ro


def _rootfs_writable(probe_dir: Path) -> bool:
    """Write-probe: create + unlink a dotfile in ``probe_dir`` (the resolved git
    dir). Catches an RO remount that /proc/mounts hasn't reflected yet, and any
    other write failure. The git dir is git-internal so this never touches the
    working tree.
    """
    probe = probe_dir / f".genesis-health-probe-{os.getpid()}"
    try:
        probe.write_text("x")
        probe.unlink()
        return True
    except OSError:
        return False


def _dedup(items: list[str]) -> list[str]:
    seen: list[str] = []
    for x in items:
        if x not in seen:
            seen.append(x)
    return seen


async def check_git_cheap(repo: Path | None = None) -> GitHealthReport:
    """Fast structural git-integrity + rootfs-writability check (per-tick safe)."""
    repo = repo or repo_root()
    return await asyncio.to_thread(_check_git_cheap_sync, repo)


def _check_git_cheap_sync(repo: Path) -> GitHealthReport:
    failures: list[str] = []
    details: dict = {}

    # Resolve the real git dir via git itself, so the file-level checks below are
    # correct for a normal repo (.git/ is a dir) AND a linked worktree (.git is a
    # FILE pointing at the main repo's .git/worktrees/<name>; --git-common-dir
    # returns the shared .git either way). A zeroed config does not break this —
    # locating .git doesn't read config content.
    rc, gcd, _ = _run_git(repo, "rev-parse", "--git-common-dir", timeout=_CHEAP_TIMEOUT_S)
    git_common: Path | None
    if rc == -1:
        failures.append("cheap_timeout")
        git_common = None
    elif rc != 0 or not gcd.strip():
        failures.append("git_dir_unresolvable")
        git_common = None
    else:
        gc = Path(gcd.strip())
        git_common = gc if gc.is_absolute() else (repo / gc)

    # .git/config parseable and carries a remote url (a zeroed/garbage config
    # makes `git config --get` return non-zero → config_invalid).
    rc, out, _ = _run_git(repo, "config", "--get", "remote.origin.url", timeout=_CHEAP_TIMEOUT_S)
    if rc == -1:
        failures.append("cheap_timeout")
    elif rc != 0 or not out.strip():
        failures.append("config_invalid")
    else:
        details["remote_url_present"] = True

    # HEAD resolves to a real commit.
    rc, _, _ = _run_git(repo, "rev-parse", "--verify", "HEAD^{commit}", timeout=_CHEAP_TIMEOUT_S)
    if rc == -1:
        failures.append("cheap_timeout")
    elif rc != 0:
        failures.append("head_unresolvable")

    # Refs are enumerable.
    rc, _, _ = _run_git(repo, "for-each-ref", "--count=1", timeout=_CHEAP_TIMEOUT_S)
    if rc == -1:
        failures.append("cheap_timeout")
    elif rc != 0:
        failures.append("refs_unreadable")

    # packed-refs zeroed — the exact incident signature: the file still "exists"
    # so higher-level git may not scream immediately, but it's been nulled.
    if git_common is not None:
        pr = git_common / "packed-refs"
        try:
            if pr.exists():
                head = pr.read_bytes()[:512]
                if len(head) == 0 or head[:1] == b"\x00":
                    failures.append("packed_refs_corrupt")
        except OSError:
            failures.append("packed_refs_unreadable")

    # Rootfs read-only / unwritable (thin-pool-exhaustion symptom). Probe the
    # resolved git dir (a real directory in both repo and worktree layouts);
    # fall back to the repo dir if we couldn't resolve it.
    probe_dir = git_common if (git_common is not None and git_common.is_dir()) else repo
    if _mount_is_readonly(repo) or not _rootfs_writable(probe_dir):
        failures.append("rootfs_readonly")

    failures = _dedup(failures)
    return GitHealthReport(
        ok=not failures, failures=failures, details=details, kind="cheap", checked_at=_utc_now_iso()
    )


async def check_git_deep(repo: Path | None = None) -> GitHealthReport:
    """Deep reachability scan (`git fsck --connectivity-only`) for a daily job."""
    repo = repo or repo_root()
    return await asyncio.to_thread(_check_git_deep_sync, repo)


def _check_git_deep_sync(repo: Path) -> GitHealthReport:
    failures: list[str] = []
    details: dict = {}
    # --connectivity-only checks that every reachable object is present without
    # rehashing every object (which --full would, too heavy for a scheduled job).
    # Missing reachable objects → non-zero. Dangling objects → exit 0 (benign).
    rc, out, err = _run_git(
        repo, "fsck", "--no-progress", "--connectivity-only", timeout=_DEEP_TIMEOUT_S
    )
    if rc == -1:
        failures.append("fsck_timeout")
    elif rc != 0:
        failures.append("fsck_failed")
        details["fsck_stderr"] = (err or out or "")[:2000]
    return GitHealthReport(
        ok=not failures, failures=failures, details=details, kind="deep", checked_at=_utc_now_iso()
    )


def write_git_health_verdict(
    report: GitHealthReport, shared_dir: Path | None = None
) -> Path | None:
    """Atomically write the verdict to ``<shared>/guardian/git_health.json`` (0600).

    Returns the path, or None if the shared mount is absent (no-guardian install).
    Never raises.
    """
    try:
        shared = shared_dir if shared_dir is not None else (genesis_home() / "shared")
        if not shared.exists():
            return None
        dest_dir = shared / _VERDICT_DIR
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / _VERDICT_FILE
        tmp = dest_dir / f".{_VERDICT_FILE}.tmp"
        tmp.write_text(json.dumps(report.to_json(), indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
        return dest
    except Exception:
        logger.debug("Failed to write git_health verdict", exc_info=True)
        return None
