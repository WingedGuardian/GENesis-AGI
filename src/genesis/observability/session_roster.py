"""Observed session liveness — computed at read time, never stored.

The roster's identity half lives in ``session_heartbeats`` (written in-session
by hooks that hold the process ancestry). This module is the other half: given
such a row, OBSERVE what is true right now. Stored liveness is stale by
construction — the ``cc_sessions.status`` column, days wrong on live sessions,
is the cautionary tale — so liveness is derived fresh on every read.

The spine (owner decision, 2026-09-05), three orthogonal facts never collapsed:

- **ALIVE** — ``/proc/<pid>`` exists with ``comm == "claude"``; verified
  against the stored ``pid_started_at`` when present so a recycled pid is
  rejected rather than called someone else's session.
- **REACHABLE** — alive AND the session's cross-session messaging socket path
  (``<sock_dir>/<pid>.sock``) exists on disk. A bound-but-unlinked socket
  (the 2026-09-05 incident) renders as ``live-no-sock`` — visible, not healed.
- **IDLE-FOR** — a duration, never a judgement: an idle session in an open
  terminal is ALIVE (owner correction: idle is not dead).

Sandbox note: ``comm``/``stat`` are world-readable, so all of this works under
genesis-server's ``ProtectSystem`` sandbox; nothing here reads ``environ``
(ptrace-gated — the ``cc_slots`` lesson). Stdlib-only leaf, deliberately, so
sync hook code can import it as freely as async server code.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

_DEF_SOCK_DIR = str(Path.home() / ".genesis" / "cc-tmp" / "cc-socks")
_DEF_ACTIVITY_DIR = str(Path.home() / ".genesis" / "session-activity")

# Tolerance when matching a stored pid_started_at against the observed start
# time: the two are computed from the same /proc counters but at different
# times and through float tick math, so exact equality is wrong. Seconds.
_START_MATCH_TOLERANCE_S = 2.0


def _observed_start_epoch(pid: int, proc_root: str) -> float | None:
    """Start time of a pid as an epoch float, or None (gone/unreadable)."""
    try:
        stat_text = (Path(proc_root) / str(pid) / "stat").read_text()
        fields = stat_text.rsplit(")", 1)[1].split()
        ticks = int(fields[19])  # starttime = overall field 22
        btime = None
        for line in (Path(proc_root) / "stat").read_text().splitlines():
            if line.startswith("btime "):
                btime = int(line.split()[1])
                break
        if btime is None:
            return None
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except Exception:
        return None


def observe(
    row: dict,
    *,
    now: datetime | None = None,
    proc_root: str = "/proc",
    sock_dir: str = _DEF_SOCK_DIR,
    activity_dir: str = _DEF_ACTIVITY_DIR,
) -> dict:
    """Return ``row`` plus observed liveness fields.

    Adds: ``alive`` (True/False/None-for-no-pid), ``alive_verified`` (start
    time matched — GROUNDWORK(session-roster-pr2): rendered by the MCP tool
    and dashboard surfaces as live-vs-live-unverified), ``reachable``,
    ``idle_s``, ``liveness`` in {"live", "live-no-sock", "gone", "unknown"}. Every observation fails open
    to the honest unknown rather than raising — this runs inside hooks.
    """
    out = dict(row)
    now = now or datetime.now(UTC)
    pid = row.get("pid")

    alive: bool | None
    alive_verified = False
    if pid is None:
        alive = None
    else:
        try:
            # read_bytes + bytes compare, deliberately: comm content is
            # attacker-influenced (any process can prctl arbitrary bytes) and
            # read_text() raises UnicodeDecodeError — a ValueError, sailing
            # past an OSError catch (probe-confirmed in review).
            alive = (
                Path(proc_root) / str(pid) / "comm"
            ).read_bytes().strip() == b"claude"
        except OSError:
            alive = False
        if alive:
            stored = row.get("pid_started_at")
            if stored:
                # Guarded end-to-end: a malformed row (non-int pid, non-str
                # stamp — legacy or tampered) must degrade, not raise. The
                # renderer's blanket catch masked this; the PR-2 MCP/dashboard
                # consumers have no such net (security review).
                try:
                    observed = _observed_start_epoch(int(pid), proc_root)
                except (TypeError, ValueError):
                    observed = None
                try:
                    stored_epoch = datetime.fromisoformat(stored).timestamp()
                except (TypeError, ValueError):
                    stored_epoch = None
                if observed is not None and stored_epoch is not None:
                    if abs(observed - stored_epoch) <= _START_MATCH_TOLERANCE_S:
                        alive_verified = True
                    else:
                        alive = False  # recycled pid: not this session's process

    reachable = False
    if alive:
        reachable = (Path(sock_dir) / f"{pid}.sock").exists()

    idle_s: float | None = None
    try:
        updated = datetime.fromisoformat(row["updated_at"])
        idle_s = max(0.0, (now - updated).total_seconds())
    except (KeyError, ValueError, TypeError):
        pass
    if pid is not None and idle_s is not None:
        try:
            marker_age = now.timestamp() - (Path(activity_dir) / str(pid)).stat().st_mtime
            if 0 <= marker_age < idle_s:
                idle_s = marker_age
        except OSError:
            pass

    if alive is None:
        liveness = "unknown"
    elif not alive:
        liveness = "gone"
    elif reachable:
        liveness = "live"
    else:
        liveness = "live-no-sock"

    out.update(
        alive=alive,
        alive_verified=alive_verified,
        reachable=reachable,
        idle_s=idle_s,
        liveness=liveness,
    )
    return out


def observe_all(rows: list[dict], **kwargs) -> list[dict]:
    """observe() over a roster query result."""
    return [observe(r, **kwargs) for r in rows]
