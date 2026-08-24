#!/usr/bin/env python3
"""Local persistent allowlist of branches confirmed pushed to a remote.

Consumed by ``git_push_guard.py``'s re-push gate. A branch whose FIRST push was
approved (and which is therefore already on the remote) must not re-prompt on
later pushes (user directive #1262). The canonical "already on the remote" check
is a live ``git ls-remote``, but that is a network call that FAIL-CLOSES to a
prompt on any transient failure — so a flaky network re-prompts an
already-approved branch. This module caches the "confirmed on remote" fact
LOCALLY so re-pushes are decided offline.

SECURITY INVARIANT
------------------
This allowlist can only ever RELAX a re-push of a branch already confirmed
present on the remote. The SOLE writer is ``git_push_guard`` on a live
``ls-remote`` HIT — it records only branches ls-remote just proved are on the
remote, i.e. branches whose first push was already approved. A never-pushed
branch is never recorded, so its first push always prompts. Reads FAIL-OPEN to
"not recorded" (→ fall back to ls-remote → prompt); a corrupt/absent/partial
file can only cause an EXTRA prompt, never a silent first push.

KEYING
------
Keyed on (branch name, remote PUSH-URL set) — NOT remote name (names vary per
clone). A record matches only when the push's resolved push-urls INTERSECT the
recorded set, so the same branch name on a DIFFERENT repo is never conflated. An
empty push-url set (unresolvable remote) never matches and is never recorded.

TRUST WINDOW (conscious acceptance)
-----------------------------------
A recorded entry is trusted for ``RETENTION_DAYS`` regardless of whether the
remote branch still exists — checking liveness would reintroduce the network
dependency this module exists to remove. Consequence: re-pushing a branch whose
remote copy was deleted within the window (e.g. a merged PR whose branch was
auto-deleted) is offline-allowed rather than re-prompted. Entries older than
``RETENTION_DAYS`` are pruned on write, bounding the window.

Stdlib-only (hooks must not import ``genesis.*``). FAIL-OPEN everywhere: any
error degrades to "not recorded" / no-op and NEVER raises. A raising import or
call from a hook would exit 1 → CC treats non-2 as non-blocking → every
fail-closed gate in ``git_push_guard`` would silently vanish (see its
``review_state`` soft-import at lines 109-115 for the same lesson).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

_VERSION = 1
RETENTION_DAYS = 90


def _state_path() -> Path:
    """``<GENESIS_HOME or ~/.genesis>/pushed_branches.json``.

    ``GENESIS_HOME`` IS the ``.genesis`` dir (mirrors ``genesis.env.genesis_home``);
    unset → ``~/.genesis``.
    """
    base = os.environ.get("GENESIS_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".genesis"
    return root / "pushed_branches.json"


def _load(path: Path) -> dict:
    """Parsed envelope, or a fresh empty one on ANY error (fail-open)."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("branches"), dict):
            return data
    except Exception:
        pass
    return {"version": _VERSION, "branches": {}}


def _now() -> datetime:
    return datetime.now(UTC)


def _entry_is_fresh(entry: object, now: datetime) -> bool:
    """Whether ``entry``'s ISO ``ts`` is within ``RETENTION_DAYS``.

    A missing / non-string / unparseable ts → NOT fresh, so the entry is dropped
    on the next write and read as "not recorded" — the safe direction (a
    re-prompt, never a silent allow).
    """
    if not isinstance(entry, dict):
        return False
    ts = entry.get("ts")
    if not isinstance(ts, str):
        return False
    try:
        when = datetime.fromisoformat(ts)
    except Exception:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (now - when).days < RETENTION_DAYS


def is_recorded(push_urls: set[str], branch: str) -> bool:
    """True iff ``branch`` is recorded, still fresh, AND its recorded push-urls
    INTERSECT ``push_urls``.

    Empty ``push_urls`` (unresolvable remote) → False (fall back to ls-remote).
    Any error → False (fail-open to the prompt path).
    """
    try:
        if not push_urls or not branch:
            return False
        data = _load(_state_path())
        entry = data.get("branches", {}).get(branch)
        if not _entry_is_fresh(entry, _now()):
            return False
        recorded = entry.get("urls")
        if not isinstance(recorded, list):
            return False
        return bool(set(recorded) & set(push_urls))
    except Exception:
        return False


def record(push_urls: set[str], branch: str) -> None:
    """Record ``branch`` as confirmed-on-remote for ``push_urls`` (REPLACE the
    url set), pruning stale entries in the same write.

    No-op if ``push_urls`` is empty (can't key it safely) or ``branch`` is falsy.
    Atomic tmp-write + ``os.replace``. FAIL-OPEN — never raises. Concurrent
    writers can at worst DROP an entry (last self-consistent map wins) → a
    redundant prompt, never a phantom record.
    """
    try:
        if not push_urls or not branch:
            return
        path = _state_path()
        now = _now()
        data = _load(path)  # fresh re-read minimizes the lost-update window
        branches = data.setdefault("branches", {})
        # Prune stale entries on every write — no separate GC job needed.
        for name in [n for n, e in list(branches.items()) if not _entry_is_fresh(e, now)]:
            del branches[name]
        branches[branch] = {
            "urls": sorted(push_urls),  # REPLACE, not union — tighter allowlist
            "ts": now.isoformat(),
        }
        data["version"] = _VERSION
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".pushed_branches.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
            os.replace(tmp, path)
        finally:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except Exception:
                pass
    except Exception:
        return
