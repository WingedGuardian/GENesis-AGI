"""Offline git-bundle lifeline — CONTAINER-side publish (F.4).

Publishes a *verified* ``git bundle`` of the main repo to the shared mount so the
host guardian can archive it OUTSIDE the container's blast radius
(``guardian/bundle_watch.py``). If both the container's local git AND the network
are gone, the host still holds a self-contained, ``git clone``-able snapshot of
the repo — the offline recovery path the thin-pool outage proved was missing
(``claude -p`` and a GitHub re-clone are both internet-bound, and snapshot
rollback needs a healthy pool).

Runs CONTAINER-side (the repo lives here), driven DAILY from the awareness tick
(``_publish_repo_bundle_if_due`` — a monotonic-interval guard, mirroring
``git_health``'s deep-fsck cadence, NOT an ``IntervalTrigger`` that resets on
restart) plus an operator ``--force`` CLI (``python -m genesis.guardian.repo_bundle
--force``).

Placement note: this lives under ``guardian/`` (drift-covered by
``watchdog._GUARDIAN_PATHS``) though it executes container-side — the same split
as ``credential_bridge``, whose ``propagate_*`` functions also run container-side
from the awareness tick.

Two safety gates, both load-bearing:
- **Health gate** — refuse to publish when ``git_health.check_git_cheap`` reports
  the repo unhealthy, so a known-good bundle is never overwritten by an attempt
  from a degraded repo (capturing a snapshot WHILE healthy is the whole point).
- **Verify gate** — ``git bundle verify`` the freshly-created bundle BEFORE it
  replaces the last good one; an unverifiable bundle is worse than a stale one.

Never raises into the caller — a lifeline that crashes the tick is worse than a
stale bundle.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from genesis.env import genesis_home, repo_root
from genesis.observability.git_health import check_git_cheap

logger = logging.getLogger(__name__)

# Where the container publishes (under ~/.genesis/shared/guardian/); the host
# sees the same bytes at <state_path>/shared/guardian/repo-bundle/.
_SHARED_SUBDIR = ("guardian", "repo-bundle")
_STAMP_NAME = "BUNDLE_STAMP"
_BUNDLE_PREFIX = "genesis-"
_BUNDLE_SUFFIX = ".bundle"
_STAMP_SCHEMA = 1

# `git bundle create --all` on this repo's ~59 MB store is <2 s / 18 MB (spike-
# measured 2026-07-14); 1800 s is vast headroom that only trips on a genuinely
# wedged fs, itself a signal. `verify` is even cheaper; 600 s bounds it. A short
# `rev-parse` uses the git_health cheap budget.
_CREATE_TIMEOUT_S = 1800
_VERIFY_TIMEOUT_S = 600
_REVPARSE_TIMEOUT_S = 10
# Refuse to build a bundle unless free space is at least this multiple of the
# object store — the bundle is smaller than .git, so 2x is deliberately generous.
_FREE_SPACE_MULTIPLE = 2


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _run_git(repo: Path, *args: str, timeout: float) -> tuple[int, str, str]:
    """Run a git command; never raises. Returns (rc, stdout, stderr).

    rc = -1 on timeout (the wedged-fs signal), -2 on any other exec failure.
    Mirrors ``git_health._run_git`` (kept local to avoid coupling to a private
    symbol in another module)."""
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


def _git_store_size(repo: Path) -> int:
    """Sum of regular-file bytes under the git common dir (.git). Best-effort:
    an unreadable tree returns what it could sum, never raises."""
    rc, gcd, _ = _run_git(repo, "rev-parse", "--git-common-dir", timeout=_REVPARSE_TIMEOUT_S)
    if rc != 0 or not gcd.strip():
        git_dir = repo / ".git"
    else:
        gc = Path(gcd.strip())
        git_dir = gc if gc.is_absolute() else (repo / gc)
    total = 0
    for root, _dirs, files in os.walk(git_dir):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _read_stamp(dest_dir: Path) -> dict | None:
    try:
        obj = json.loads((dest_dir / _STAMP_NAME).read_text())
        return obj if isinstance(obj, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write_stamp(dest_dir: Path, stamp: dict) -> None:
    """Atomic 0600 stamp write (same-dir tmp + os.replace). Written LAST in a
    publish so its presence means 'a complete bundle round finished'."""
    tmp = dest_dir / f".{_STAMP_NAME}.tmp"
    tmp.write_text(json.dumps(stamp, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest_dir / _STAMP_NAME)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _prune_to_newest(dest_dir: Path, keep_name: str) -> None:
    """Delete every ``genesis-*.bundle`` in ``dest_dir`` except ``keep_name``."""
    for f in dest_dir.glob(f"{_BUNDLE_PREFIX}*{_BUNDLE_SUFFIX}"):
        if f.name == keep_name:
            continue
        try:
            f.unlink()
        except OSError:
            logger.debug("repo_bundle: could not prune old bundle %s", f, exc_info=True)


async def publish_repo_bundle(
    *,
    force: bool = False,
    repo: Path | None = None,
    shared_dir: Path | None = None,
) -> dict | None:
    """Publish a verified bundle of ``repo`` to the shared mount. Never raises.

    Returns a result dict describing the action, or None when there is no shared
    mount (a no-guardian install). ``force`` bypasses the cadence/HEAD-unchanged
    skip only; the health gate and the verify gate ALWAYS apply.
    """
    repo = repo or repo_root()
    shared_base = shared_dir or (genesis_home() / "shared")
    try:
        if not shared_base.exists():
            logger.debug("repo_bundle: shared mount %s absent — skipping", shared_base)
            return None

        # HEALTH GATE — never overwrite the last good bundle with an attempt from
        # a degraded repo. check_git_cheap is ms-scale and already threaded.
        report = await check_git_cheap(repo)
        if not report.ok:
            logger.warning(
                "repo_bundle: repo unhealthy (%s) — refusing to publish; run scripts/git_repair.py",
                ", ".join(report.failures),
            )
            return {
                "action": "refused",
                "reason": "git_unhealthy",
                "failures": list(report.failures),
            }

        # All remaining work is blocking (git subprocess + file IO) — one thread
        # hop keeps the awareness tick responsive.
        return await asyncio.to_thread(_publish_core_sync, repo, shared_base, force)
    except Exception:
        logger.warning("repo_bundle: publish failed", exc_info=True)
        return {"action": "error", "reason": "exception"}


def _publish_core_sync(repo: Path, shared_base: Path, force: bool) -> dict:
    """Synchronous publish core (runs in a worker thread). Assumes the health
    gate already passed. Never raises (wrapped by the caller)."""
    dest_dir = shared_base.joinpath(*_SHARED_SUBDIR)
    dest_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(dest_dir, 0o700)

    rc, head_out, _ = _run_git(repo, "rev-parse", "HEAD", timeout=_REVPARSE_TIMEOUT_S)
    if rc != 0 or not head_out.strip():
        return {"action": "refused", "reason": "head_unresolvable"}
    head = head_out.strip()
    now = _utc_now_iso()

    stamp = _read_stamp(dest_dir)

    # HEAD unchanged and not forced → the bundle content is still current. Rewrite
    # ONLY the tiny stamp's last_verified_at (records this healthy check for the
    # host freshness alert) — do NOT rebuild the bundle. This decouples freshness
    # from commit activity: a quiet-commit period stays "fresh" as long as the
    # daily healthy check keeps advancing last_verified_at.
    if not force and stamp and stamp.get("head") == head and stamp.get("bundle"):
        bundle_path = dest_dir / str(stamp["bundle"])
        if bundle_path.exists():
            stamp["last_verified_at"] = now
            _write_stamp(dest_dir, stamp)
            return {"action": "verified_unchanged", "head": head}
        # Stamp claims a bundle that vanished → fall through and rebuild.

    # Free-space guard: refuse when free < 2x the object store (bundle is smaller
    # than .git, so this is generous; a full mount must not be half-filled).
    store_size = _git_store_size(repo)
    try:
        free = shutil.disk_usage(dest_dir).free
    except OSError:
        free = 0
    if store_size and free < _FREE_SPACE_MULTIPLE * store_size:
        logger.warning(
            "repo_bundle: insufficient free space (%d < %dx %d) — skipping",
            free,
            _FREE_SPACE_MULTIPLE,
            store_size,
        )
        return {
            "action": "refused",
            "reason": "insufficient_space",
            "free": free,
            "store_size": store_size,
        }

    # Create into a same-directory .partial so the final os.replace is atomic (a
    # ~/tmp→shared replace would be cross-device and fail). --all bundles every
    # ref + HEAD → clone-safe (spike-verified: reclone HEAD matches container).
    tmp = dest_dir / f".bundle-{os.getpid()}.partial"
    try:
        rc, _, err = _run_git(
            repo,
            "bundle",
            "create",
            str(tmp),
            "--all",
            timeout=_CREATE_TIMEOUT_S,
        )
        if rc != 0 or not tmp.exists():
            logger.warning("repo_bundle: bundle create failed (rc=%s): %s", rc, err[:200])
            return {"action": "refused", "reason": "create_failed", "rc": rc}

        # VERIFY GATE — never publish a bundle that does not verify.
        rc, _, err = _run_git(
            repo,
            "bundle",
            "verify",
            str(tmp),
            timeout=_VERIFY_TIMEOUT_S,
        )
        if rc != 0:
            logger.warning("repo_bundle: bundle verify failed (rc=%s): %s", rc, err[:200])
            return {"action": "refused", "reason": "verify_failed", "rc": rc}

        size = tmp.stat().st_size
        digest = _sha256(tmp)
        os.chmod(tmp, 0o600)
        bundle_name = f"{_BUNDLE_PREFIX}{head[:12]}{_BUNDLE_SUFFIX}"
        os.replace(tmp, dest_dir / bundle_name)
    finally:
        # A create/verify failure leaves the .partial behind — clean it up.
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()

    # Keep only the just-published bundle on the shared side (host archive keeps N).
    _prune_to_newest(dest_dir, bundle_name)

    # Stamp LAST (completeness marker). last_verified_at == created_at on a build.
    _write_stamp(
        dest_dir,
        {
            "version": _STAMP_SCHEMA,
            "head": head,
            "bundle": bundle_name,
            "size": size,
            "sha256": digest,
            "created_at": now,
            "last_verified_at": now,
        },
    )
    logger.info("repo_bundle: published %s (%d bytes) for HEAD %s", bundle_name, size, head[:12])
    return {"action": "published", "head": head, "bundle": bundle_name, "size": size}


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m genesis.guardian.repo_bundle [--force]``.

    Operator on-demand publish. ``--force`` bypasses the HEAD-unchanged skip; the
    health + verify gates still apply (an unhealthy repo is refused, not
    overwritten). Exit 0 on published/verified_unchanged, 1 on refused/error.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in argv
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    result = asyncio.run(publish_repo_bundle(force=force))
    print(
        json.dumps(
            result if result is not None else {"action": "skipped", "reason": "no_shared_mount"}
        )
    )
    if result is None:
        return 0  # no-guardian install: nothing to do is not an error
    return 0 if result.get("action") in ("published", "verified_unchanged") else 1


if __name__ == "__main__":
    raise SystemExit(main())
