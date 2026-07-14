#!/usr/bin/env python3
"""Index-request marker store — the ONE authority for the code-intel queue.

Triggers (post-commit hook, setup_claude_config, the gitnexus surplus job,
install.sh, disk_reclaim) no longer spawn indexers. They drop a *marker* here;
the idle-gated runner (``code_intel_runner.sh``) is the only thing that ever
consumes one and invokes the locked+capped entrypoint. This decouples "code
changed, reindex eventually" from "reindex NOW" — the latter is what storms the
container (see ``~/.genesis/handoffs/reindex-idle-gate-fixes.md``).

Stdlib-only, ON PURPOSE: ``disk_reclaim.py`` imports this and must run even when
the server / venv is unhealthy (mirrors disk_reclaim's own no-genesis-imports
rule). Bash callers shell out to the CLI below.

Marker filename is ``<sha1-16-of-canonical-repo-path>.json`` — the SAME hash the
entrypoint keys its single-flight lock on (``code_intel_index.sh`` canonicalizes
with ``pwd -P`` then ``printf '%s' … | sha1sum | cut -c1-16``). The parity is
load-bearing (a runner marker must map to the repo whose lock the host freeze
holds); ``marker_hash`` reproduces it with ``os.path.realpath`` + ``hashlib`` and
a cross-language test asserts byte-identical output.

State files in the marker dir, all keyed by the same hash:
  <hash>.json           canonical pending request (what ``list`` reports)
  <hash>.inflight.json  claimed by the runner for an in-flight index (move-aside)
  <hash>.failed.json    euthanized after too many genuine failures (never retried)
  .last-full-<hash>     timestamp of the last successful FULL index (weekly gate)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# Genuine-failure attempts before a marker is euthanized to <hash>.failed.json.
# An escalated-full failure does NOT count (it falls back to fast); only a
# genuinely-broken run burns this budget.
MAX_ATTEMPTS = 5
# A fast marker older than this with no recent full index escalates to full.
FULL_INTERVAL_S = 7 * 24 * 3600
# After a full escalation FAILS, back off full-escalation for this long so runs
# fall back to cheap fast indexing (keeps the incremental graph fresh) instead
# of re-escalating to a doomed full every tick. cbm 0.9 can't resume a killed
# full, so a too-big/always-killed full must not thrash — it retries ~daily.
FULL_BACKOFF_S = 24 * 3600
VALID_TOOLS = ("cbm", "gitnexus", "both")
VALID_MODES = ("fast", "moderate", "full")


def marker_dir() -> Path:
    """Marker directory, honoring GENESIS_HOME (matches the entrypoint's lock dir root)."""
    base = os.environ.get("GENESIS_HOME") or str(Path.home() / ".genesis")
    return Path(base) / "index-requests"


def canonical_repo(repo_path: str) -> str:
    """Canonical physical path — the string the hash is taken over.

    Must equal the entrypoint's ``cd "$REPO_PATH" && pwd -P``. os.path.realpath
    resolves symlinks and normalizes exactly as pwd -P does for a real dir.
    """
    return os.path.realpath(repo_path)


def marker_hash(repo_path: str) -> str:
    """sha1-16 of the canonical repo path — identical to the entrypoint lock hash."""
    canonical = canonical_repo(repo_path)
    # sha1 is not a security choice here: it must byte-match the entrypoint's
    # `printf '%s' … | sha1sum | cut -c1-16` so a marker maps to the repo whose
    # single-flight lock the host freeze holds. Changing the algo breaks parity.
    return hashlib.sha1(canonical.encode()).hexdigest()[:16]  # noqa: S324


def _union_tools(a: str, b: str) -> str:
    """Widen tools on coalesce: any disagreement means we need both."""
    return a if a == b else "both"


def _highest_mode(a: str, b: str) -> str:
    """Coalesce keeps the more thorough mode (full > moderate > fast)."""
    order = {"fast": 0, "moderate": 1, "full": 2}
    return a if order.get(a, 0) >= order.get(b, 0) else b


def _atomic_write(path: Path, payload: dict) -> None:
    """Write JSON via tmp-in-same-dir + os.replace (atomic on POSIX)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with _suppress(OSError):
            os.unlink(tmp)
        raise


class _suppress:
    """Tiny contextlib.suppress clone (kept inline to stay import-light)."""

    def __init__(self, *excs):
        self._excs = excs

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, self._excs)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


# ─── Operations ──────────────────────────────────────────────────────────


def write_marker(repo_path: str, tools: str, mode: str) -> Path:
    """Create or coalesce a pending marker. Returns the marker path.

    Coalesce rule: preserve the EARLIEST requested_at (oldest pending work sets
    the max-defer clock), union tools, keep the more thorough mode, carry
    attempts. requested_at is deliberately NOT a change token — the runner uses
    move-aside, not compare-before-delete, so a coalesce during an in-flight
    index is never lost.
    """
    if tools not in VALID_TOOLS:
        raise ValueError(f"tools must be one of {VALID_TOOLS}, got {tools!r}")
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {VALID_MODES}, got {mode!r}")
    canonical = canonical_repo(repo_path)
    h = marker_hash(repo_path)
    path = marker_dir() / f"{h}.json"
    now = time.time()
    existing = _read_json(path) or {}
    payload = {
        "repo_path": canonical,
        "tools": _union_tools(existing.get("tools", tools), tools),
        "mode": _highest_mode(existing.get("mode", mode), mode),
        "requested_at": min(existing.get("requested_at", now), now),
        "attempts": int(existing.get("attempts", 0)),
    }
    _atomic_write(path, payload)
    return path


def list_markers() -> list[dict]:
    """All pending canonical markers (not inflight/failed), with derived age."""
    d = marker_dir()
    if not d.is_dir():
        return []
    out = []
    now = time.time()
    for p in sorted(d.glob("*.json")):
        name = p.name
        if name.endswith(".inflight.json") or name.endswith(".failed.json"):
            continue
        data = _read_json(p)
        if not data or "repo_path" not in data:
            continue
        data = dict(data)
        data["hash"] = p.stem
        data["age_s"] = max(0, int(now - data.get("requested_at", now)))
        out.append(data)
    return out


def claim(h: str) -> dict | None:
    """Move-aside a pending marker to <hash>.inflight.json (atomic). Returns its data.

    A concurrent trigger recreating <hash>.json after this is fine — it becomes
    the next tick's work; this in-flight copy is what the current run operates on.
    """
    d = marker_dir()
    canonical_path = d / f"{h}.json"
    inflight = d / f"{h}.inflight.json"
    data = _read_json(canonical_path)
    if data is None:
        return None
    try:
        os.replace(canonical_path, inflight)  # atomic move-aside
    except OSError:
        return None
    data = dict(data)
    data["hash"] = h
    return data


def consume(h: str) -> None:
    """Success: drop the in-flight copy. Any canonical recreated meanwhile survives."""
    with _suppress(OSError):
        (marker_dir() / f"{h}.inflight.json").unlink()


def restore(h: str, *, attempts_inc: bool = False) -> str:
    """Return an in-flight marker to pending, coalescing with any new canonical.

    Used for rc=75 (frozen — keep, no penalty), rc=3 (tools missing — keep, no
    penalty), and genuine failures (attempts_inc=True). Returns the resulting
    state: "pending" or "failed" (euthanized at MAX_ATTEMPTS).
    """
    d = marker_dir()
    inflight = d / f"{h}.inflight.json"
    canonical = d / f"{h}.json"
    inflight_data = _read_json(inflight)
    if inflight_data is None:
        return "pending"  # nothing to restore (already consumed?) — no-op
    attempts = int(inflight_data.get("attempts", 0)) + (1 if attempts_inc else 0)
    # Coalesce with any canonical a concurrent trigger recreated during the run.
    new_canon = _read_json(canonical)
    if new_canon:
        inflight_data = {
            "repo_path": inflight_data["repo_path"],
            "tools": _union_tools(
                inflight_data.get("tools", "both"), new_canon.get("tools", "both")
            ),
            "mode": _highest_mode(inflight_data.get("mode", "fast"), new_canon.get("mode", "fast")),
            "requested_at": min(
                inflight_data.get("requested_at", time.time()),
                new_canon.get("requested_at", time.time()),
            ),
            "attempts": attempts,
        }
    else:
        inflight_data = dict(inflight_data)
        inflight_data["attempts"] = attempts
    inflight_data.pop("hash", None)
    inflight_data.pop("age_s", None)
    if attempts >= MAX_ATTEMPTS:
        _atomic_write(d / f"{h}.failed.json", inflight_data)
        with _suppress(OSError):
            inflight.unlink()
        with _suppress(OSError):
            canonical.unlink()
        return "failed"
    _atomic_write(canonical, inflight_data)
    with _suppress(OSError):
        inflight.unlink()
    return "pending"


def repend_stale_inflight() -> list[str]:
    """Re-pend any orphaned ``<hash>.inflight.json`` and return the hashes.

    A runner that dies between claim and the rc handler (OOM, host stop, unit
    TimeoutStartSec) leaves a marker stranded as ``.inflight`` — ``list_markers``
    skips those, so a quiet repo would never be reindexed until a new trigger.
    The runner self-flocks (one tick at a time), so any inflight present when a
    fresh tick starts is necessarily from a DEAD previous run — safe to restore.
    """
    d = marker_dir()
    if not d.is_dir():
        return []
    out = []
    suffix = ".inflight.json"
    for p in sorted(d.glob(f"*{suffix}")):
        h = p.name[: -len(suffix)]
        restore(h)  # merge back into any canonical, no penalty
        out.append(h)
    return out


def last_full_path(h: str) -> Path:
    """Path of the marker recording when a FULL index last succeeded for this
    repo. Its age gates weekly-full escalation (see ``should_escalate_full``)."""
    return marker_dir() / f".last-full-{h}"


def full_backoff_path(h: str) -> Path:
    """Path of the marker recording when a FULL escalation last FAILED. Its
    presence within ``FULL_BACKOFF_S`` suppresses re-escalation so a doomed full
    falls back to cheap fast runs instead of thrashing."""
    return marker_dir() / f".full-backoff-{h}"


def _age_or_none(p: Path) -> float | None:
    try:
        return time.time() - float(p.read_text().strip())
    except (OSError, ValueError):
        return None


def should_escalate_full(h: str) -> bool:
    """Escalate a fast run to full only when it's both DUE and not backed off.

    Due: no successful full recorded, or the last is older than FULL_INTERVAL_S.
    Backed off: a full failed within FULL_BACKOFF_S — retry fast meanwhile so a
    doomed full (too big for the wall cap, cbm can't resume) doesn't thrash.
    """
    last_full_age = _age_or_none(last_full_path(h))
    due = last_full_age is None or last_full_age >= FULL_INTERVAL_S
    if not due:
        return False
    backoff_age = _age_or_none(full_backoff_path(h))
    backed_off = backoff_age is not None and backoff_age < FULL_BACKOFF_S
    return not backed_off


def stamp_full(h: str) -> None:
    """Record a successful full index; clears any full-escalation backoff."""
    _atomic_write_text(last_full_path(h), f"{time.time()}\n")
    with _suppress(OSError):
        full_backoff_path(h).unlink()


def mark_full_backoff(h: str) -> None:
    """Record a failed full escalation so runs fall back to fast for a while."""
    _atomic_write_text(full_backoff_path(h), f"{time.time()}\n")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with _suppress(OSError):
            os.unlink(tmp)
        raise


# ─── CLI (bash callers) ──────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Code-intel index-request markers")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_write = sub.add_parser("write", help="create/coalesce a pending marker")
    p_write.add_argument("--repo", required=True)
    p_write.add_argument("--tools", default="both", choices=VALID_TOOLS)
    p_write.add_argument("--mode", default="fast", choices=VALID_MODES)

    p_hash = sub.add_parser("hash", help="print the sha1-16 lock/marker hash")
    p_hash.add_argument("--repo", required=True)

    sub.add_parser(
        "list", help="TSV of pending markers: hash\\trepo\\ttools\\tmode\\tattempts\\tage_s"
    )

    for name in ("claim", "consume", "should-escalate", "stamp-full", "mark-full-backoff"):
        sp = sub.add_parser(name)
        sp.add_argument("--hash", required=True)

    sub.add_parser("reconcile-inflight", help="re-pend orphaned inflight markers")

    p_restore = sub.add_parser("restore")
    p_restore.add_argument("--hash", required=True)
    p_restore.add_argument("--attempts-inc", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "write":
        path = write_marker(args.repo, args.tools, args.mode)
        print(path)
        return 0
    if args.cmd == "hash":
        print(marker_hash(args.repo))
        return 0
    if args.cmd == "list":
        for m in list_markers():
            print(
                f"{m['hash']}\t{m['repo_path']}\t{m['tools']}\t{m['mode']}"
                f"\t{m['attempts']}\t{m['age_s']}"
            )
        return 0
    if args.cmd == "claim":
        data = claim(args.hash)
        if data is None:
            return 1
        print(f"{data['repo_path']}\t{data['tools']}\t{data['mode']}\t{data.get('attempts', 0)}")
        return 0
    if args.cmd == "consume":
        consume(args.hash)
        return 0
    if args.cmd == "restore":
        state = restore(args.hash, attempts_inc=args.attempts_inc)
        print(state)
        return 0
    if args.cmd == "should-escalate":
        return 0 if should_escalate_full(args.hash) else 1
    if args.cmd == "stamp-full":
        stamp_full(args.hash)
        return 0
    if args.cmd == "mark-full-backoff":
        mark_full_backoff(args.hash)
        return 0
    if args.cmd == "reconcile-inflight":
        repended = repend_stale_inflight()
        if repended:
            print(f"re-pended {len(repended)} stale inflight marker(s)")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
