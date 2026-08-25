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
clone). A record matches only when EVERY current push-url is covered by the
recorded set (subset, not a partial intersection): git pushes to every configured
push URL, so a newly-added destination must re-prompt rather than ride an old
url's cache hit. The same branch name on a DIFFERENT repo is never conflated. URLs
are SANITIZED before storing/matching — userinfo/credentials are stripped (a token
in ``https://user:<PAT>@host`` is never persisted to the plaintext state file),
and a RELATIVE local path (ambiguous across worktrees) makes the whole decision
fall back to ls-remote. An empty push-url set (unresolvable remote) never matches
and is never recorded.

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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

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


def _sanitize_url(url: str) -> str | None:
    """A credential-free, STABLE key form of a push URL, or None if the URL must
    NOT be used for an offline cache decision.

    - Scheme URL (``scheme://[userinfo@]host/path``): parsed with the CANONICAL
      stdlib ``urllib.parse.urlsplit`` (never a hand-rolled split of an unbounded
      grammar). The key drops the ``userinfo`` (``https://user:<PAT>@host/repo`` →
      ``https://host/repo``, so a token is never persisted or keyed on), lowercases
      the case-insensitive scheme + host, preserves host port and IPv6 brackets and
      the path, and drops the fragment. A form with no host (``https://user@/x``,
      ``file:///x``) → None (fall back to ls-remote).
    - scp-like ssh (``[user@]host:path``): git's OWN rule distinguishes this from a
      local path by "a ``:`` with NO ``/`` before it" (a slash before the first
      colon means a local path). We honor that exactly, then strip any ``user@``
      prefix. A colon that appears AFTER a slash (e.g. ``backups/2026-01-01T12:00/
      repo.git``) is therefore a RELATIVE LOCAL path, not scp — critical, since a
      relative path is ambiguous across worktrees.
    - Absolute local path (``/…``): stable, kept as-is.
    - RELATIVE local path (``../x``, ``./x``, ``~/x``, ``x/y.git``, ``x/y:z``): None.
      Git resolves it relative to each repo, so the same raw string can denote
      DIFFERENT repositories across worktrees; the caller must fall back to
      ls-remote (a prompt) rather than risk a cross-repo silent push.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    u = url.strip()
    # Scheme URL — parse with the CANONICAL stdlib parser, never a hand-split of an
    # unbounded grammar. ``urlsplit`` lowercases the scheme + host, drops the
    # userinfo (so an embedded ``user:<PAT>@`` token never reaches the key), and
    # separates host/port/path/query correctly (incl. IPv6 literals).
    if "://" in u:
        try:
            parts = urlsplit(u)
            host = parts.hostname  # already lowercased, userinfo-stripped
        except Exception:
            return None
        if not parts.scheme or not host:
            return None  # no scheme/host (e.g. ``https://user@/x``, ``file:///…``) → don't key
        if ":" in host:  # IPv6 literal — urlsplit strips the brackets; restore them
            host = f"[{host}]"
        if parts.port is not None:
            host = f"{host}:{parts.port}"
        # Credential-free rebuild: userinfo dropped, fragment dropped, path+query kept.
        return urlunsplit((parts.scheme.lower(), host, parts.path, parts.query, ""))
    # Absolute local path.
    if u.startswith("/"):
        return u
    # scp-like ssh — only when a ':' appears with NO '/' before it (git's rule).
    colon = u.find(":")
    slash = u.find("/")
    if colon > 0 and (slash == -1 or colon < slash) and not u.startswith((".", "~")):
        authority = u[:colon]  # [user@]host
        path = u[colon:]  # ':path'
        if "@" in authority:  # strip scp userinfo (git@… etc.) — never persist it
            authority = authority.rsplit("@", 1)[1]
        return f"{authority}{path}" if authority else None
    # Relative local path (incl. a colon that sits AFTER a slash) — do not key.
    return None


def _keyable_urls(push_urls: set[str]) -> set[str] | None:
    """The sanitized, credential-free key set for ``push_urls`` — or None if the
    set is empty OR ANY member is non-keyable (a relative local path).

    None means "cannot decide offline": git pushes to EVERY push URL, so if even
    one destination is ambiguous the whole push must fall back to ls-remote (a
    prompt) rather than be offline-allowed or partially recorded.
    """
    if not push_urls:
        return None
    out: set[str] = set()
    for u in push_urls:
        s = _sanitize_url(u)
        if s is None:
            return None
        out.add(s)
    return out


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
    # Require a NON-NEGATIVE age as well as < RETENTION_DAYS: a FUTURE ts (a clock
    # that was ahead at record time and later corrected, or a tampered/corrupt
    # state file) would otherwise read as fresh far beyond the window — its
    # negative age has a negative ``.days``, which is always < RETENTION_DAYS.
    return timedelta(0) <= (now - when) < timedelta(days=RETENTION_DAYS)


def is_recorded(push_urls: set[str], branch: str) -> bool:
    """True iff ``branch`` is recorded, still fresh, AND EVERY current push-url is
    covered by the recorded set (subset, not a partial intersection).

    Subset — not intersection — because git pushes to EVERY configured push URL:
    if a new destination was added after recording, a partial-intersection hit
    would silently publish the branch to that never-approved destination. Requiring
    all current urls ⊆ recorded forces a re-prompt when a new push url appears.

    Empty ``push_urls`` or any non-keyable (relative-path) url → False (fall back
    to ls-remote). Any error → False (fail-open to the prompt path).
    """
    try:
        if not branch:
            return False
        keyable = _keyable_urls(push_urls)
        if not keyable:  # empty or an ambiguous relative-path url → can't decide offline
            return False
        data = _load(_state_path())
        entry = data.get("branches", {}).get(branch)
        if not _entry_is_fresh(entry, _now()):
            return False
        recorded = entry.get("urls")
        if not isinstance(recorded, list):
            return False
        return keyable <= set(recorded)
    except Exception:
        return False


def record(push_urls: set[str], branch: str) -> None:
    """Record ``branch`` as confirmed-on-remote for ``push_urls`` (REPLACE the
    url set), pruning stale entries in the same write.

    No-op if ``branch`` is falsy, ``push_urls`` is empty, or ANY url is non-keyable
    (a relative local path — see ``_sanitize_url``): an ambiguous destination must
    never be cached. Stored urls are SANITIZED (userinfo/credentials stripped) so
    the plaintext state file never persists a token. Atomic tmp-write +
    ``os.replace``. FAIL-OPEN — never raises. Concurrent writers can at worst DROP
    an entry (last self-consistent map wins) → a redundant prompt, never a phantom
    record.
    """
    try:
        if not branch:
            return
        keyable = _keyable_urls(push_urls)
        if not keyable:  # empty or an ambiguous relative-path url → don't cache
            return
        path = _state_path()
        now = _now()
        data = _load(path)  # fresh re-read minimizes the lost-update window
        branches = data.setdefault("branches", {})
        # Prune stale entries on every write — no separate GC job needed.
        for name in [n for n, e in list(branches.items()) if not _entry_is_fresh(e, now)]:
            del branches[name]
        branches[branch] = {
            "urls": sorted(keyable),  # sanitized, credential-free; REPLACE not union
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
