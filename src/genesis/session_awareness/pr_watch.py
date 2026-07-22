"""PR-watch core — surface the upstream-PR-steward's own owner notifications
inside foreground CC sessions.

The ``upstream-pr-steward`` campaign already notifies the owner (Telegram) when
a tracked external PR changes (merged/closed/new maintainer comment/nudge), and
logs each ping to ``outreach_history`` (category ``notification``). But those
pings are easy to miss on Telegram. This module lets a SessionStart hook mirror
the *unseen* ones inline as a one-line nudge.

Design notes:
- **Read-only** against ``genesis.db`` (``file:...?mode=ro`` — WAL-aware; never
  ``immutable=1`` which would miss un-checkpointed writes). Every failure path
  degrades to "surface nothing" — a hook must never block session start.
- **Seen-state is a home-anchored JSON sidecar**, not the DB: ``opened_at`` in
  ``outreach_history`` is unwired (never populated), so it cannot be the seen
  signal. The sidecar records which notification ids this inline surface has
  already shown, with the timestamp of first surfacing, so a change keeps
  reappearing for ``resurface_days`` and then stops nagging.
- The notification volume is tiny (the steward pings only on material change,
  every ~2 days), so there is no first-run "backlog dump" risk — an empty
  sidecar simply surfaces the recent unseen pings, capped by ``max_surface``.
  (This is the deliberate divergence from the discarded pr_status-diff design,
  whose first run had to baseline the whole roster silently.)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from genesis.env import genesis_home

logger = logging.getLogger(__name__)

_SIDECAR_VERSION = 1
# Steward campaign is literally named "…steward"; every owner ping it sends
# carries that in the topic. Case-insensitive LIKE (SQLite default for ASCII).
_TOPIC_LIKE = "%steward%"


def db_path() -> Path:
    """The PROD DB, home-anchored — NOT repo_root()-anchored.

    ``genesis.env.genesis_db_path()`` resolves ``repo_root()/data/genesis.db``,
    which in a worktree session points at an EMPTY ``<worktree>/data/`` — a
    silent-coverage trap (same reason ``genesis_session_context._charter_db_path``
    is home-anchored). outreach_history only ever lives in the prod DB, so read
    that directly. ``GENESIS_DB_PATH`` overrides for tests/E2E/relocated installs.
    """
    explicit = os.environ.get("GENESIS_DB_PATH")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / "genesis" / "data" / "genesis.db"


def sidecar_path() -> Path:
    """Home-anchored seen-state file — shared across worktrees (the human's
    'last seen' is per-human, not per-checkout)."""
    return genesis_home() / "pr_watch" / "seen.json"


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def read_steward_notifications(
    db_file: Path, lookback_days: int, now: datetime
) -> list[dict[str, Any]]:
    """Recent steward owner-notifications, newest first. Any error -> []."""
    cutoff = now.timestamp() - lookback_days * 86400
    try:
        if not db_file.exists():
            return []
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=2)
        try:
            conn.execute("PRAGMA busy_timeout=300")
            rows = conn.execute(
                "SELECT id, topic, delivered_at FROM outreach_history "
                "WHERE category = 'notification' AND topic LIKE ? "
                "AND delivered_at IS NOT NULL "
                "ORDER BY delivered_at DESC",
                (_TOPIC_LIKE,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    for rid, topic, delivered_at in rows:
        ts = _parse_ts(delivered_at)
        if ts is None or ts.timestamp() < cutoff:
            continue
        out.append({"id": rid, "topic": topic or "", "delivered_at": delivered_at})
    return out


def load_sidecar(path: Path) -> tuple[dict[str, dict[str, Any]], bool]:
    """Return (surfaced-map, existed). Corrupt/absent -> ({}, False)."""
    try:
        if not path.exists():
            return {}, False
        data = json.loads(path.read_text())
        surfaced = data.get("surfaced") if isinstance(data, dict) else None
        if not isinstance(surfaced, dict):
            return {}, False
        # Keep only well-formed entries.
        clean = {str(k): v for k, v in surfaced.items() if isinstance(v, dict)}
        return clean, True
    except Exception:
        return {}, False


def save_sidecar(path: Path, surfaced: dict[str, dict[str, Any]]) -> None:
    """Atomic write (tmp + rename). Fail-open — never raise."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": _SIDECAR_VERSION, "surfaced": surfaced})
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".seen-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except Exception:
        logger.debug("pr_watch sidecar write failed", exc_info=True)


def render_clause(notif: dict[str, Any]) -> str:
    """Condense a notification into one short human clause with a date hint."""
    topic = str(notif.get("topic") or "").strip()
    first_line = next((ln.strip() for ln in topic.splitlines() if ln.strip()), "")
    if len(first_line) > 72:
        first_line = first_line[:71].rstrip() + "…"
    ts = _parse_ts(notif.get("delivered_at"))
    if ts is not None:
        return f"{first_line} ({ts.strftime('%b %-d')})"
    return first_line or "(update)"


def select_to_surface(
    notifs: list[dict[str, Any]],
    surfaced: dict[str, dict[str, Any]],
    now: datetime,
    resurface_days: int,
    max_surface: int,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Decide which notifications to surface this run and compute the next
    sidecar.

    Per notification (already lookback-filtered, newest first):
    - unseen -> surface, record first_ts=now
    - seen, first surfaced <= resurface_days ago -> resurface
    - seen, aged past resurface_days -> keep in sidecar, do NOT surface

    The next sidecar is built ONLY from the current notification set, so any id
    that has fallen outside the lookback window is pruned automatically (no
    unbounded growth, no retention step).
    """
    resurface_cutoff = now.timestamp() - resurface_days * 86400
    new_surfaced: dict[str, dict[str, Any]] = {}
    to_show: list[dict[str, Any]] = []

    for n in notifs:
        nid = str(n["id"])
        prev = surfaced.get(nid)
        if prev is None:
            new_surfaced[nid] = {"first_ts": now.isoformat()}
            to_show.append(n)
            continue
        first_ts = _parse_ts(prev.get("first_ts")) or now
        new_surfaced[nid] = {"first_ts": prev.get("first_ts") or now.isoformat()}
        if first_ts.timestamp() >= resurface_cutoff:
            to_show.append(n)

    lines = [render_clause(n) for n in to_show[: max(max_surface, 0)]]
    overflow = len(to_show) - len(lines)
    if overflow > 0:
        lines.append(f"+{overflow} more")
    return lines, new_surfaced


def format_injection(lines: list[str]) -> str:
    """The single line injected into the session. Empty -> ''."""
    if not lines:
        return ""
    n = len([ln for ln in lines if not ln.startswith("+")])
    noun = "update" if n == 1 else "updates"
    return (
        f"[PRs] {n} external-PR {noun} you may not have seen — "
        + " · ".join(lines)
        + '. Ask "show PRs" to review.'
    )
