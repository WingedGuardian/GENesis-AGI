"""Observe old process trees without granting global termination authority.

Names, age, activity markers and terminal attachment are discovery hints, not
proof of ownership or abandonment. This hourly sweep records candidates for
operator inspection and never signals processes, including with legacy arm
flags. Claude, Codex, OpenCode and browser helpers receive the same protection.
Explicitly owned job launchers retain their separate cancellation contracts.
The legacy observation type is retained for existing consumers and its TTL.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")

# ── Policy constants ────────────────────────────────────────────────────
_CLAUDE_AGE_FLOOR_SECS = 168 * 3600  # 7d — floor before a claude proc is even considered
_CLAUDE_IDLE_WINDOW_SECS = 168 * 3600  # 7d — "active within" window (marker freshness)

_GENESIS_DIR = Path.home() / ".genesis"
_MARKER_DIR = _GENESIS_DIR / "session-activity"
_STATE_PATH = _GENESIS_DIR / "reaper_state.json"

# Legacy controls are read only to explain ignored arm requests. No flag can
# authorize signaling from global discovery.
_ENV_HARD_DISABLE = "GENESIS_REAPER_KILL_DISABLED"

# Legacy environment arm request (diagnostic only).
_ENV_ARM = "GENESIS_REAPER_ARMED"

# Legacy persisted arm request; set_operator_armed can only clear it.
_STATE_ARMED_KEY = "armed_by_operator"


# ── Pure decision core (fully unit-testable) ────────────────────────────
def classify_claude_pid(
    *,
    age_secs: float,
    now: float,
    marker_mtime: float | None,
    controlling_tty: str | None,
    live_ttys: set[str],
    age_floor_secs: float = _CLAUDE_AGE_FLOOR_SECS,
    idle_window_secs: float = _CLAUDE_IDLE_WINDOW_SECS,
) -> tuple[bool, str]:
    """Decide whether a ``claude`` process is a reap candidate.

    Returns ``(should_reap, reason)``. Spares (``should_reap=False``) win
    on the first matching guard, in priority order: young → fresh marker →
    live terminal. Only a process that clears all three is a candidate.
    """
    if age_secs <= age_floor_secs:
        return False, "young"
    if marker_mtime is not None and (now - marker_mtime) < idle_window_secs:
        return False, "fresh-marker"
    if controlling_tty is not None and controlling_tty in live_ttys:
        return False, "live-tty"
    return True, "stale-detached"


# ── I/O primitives (module-level so tests can monkeypatch them) ──────────
async def _pgrep(flag: str, pattern: str) -> list[int]:
    """Return PIDs matching ``pgrep <flag> <pattern>`` (empty on no match)."""
    proc = await asyncio.create_subprocess_exec(
        "pgrep",
        flag,
        pattern,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if not stdout.strip():
        return []
    return [int(p) for p in stdout.decode().split() if p.strip().isdigit()]


def _read_uptime() -> float:
    with open("/proc/uptime") as f:
        return float(f.read().split()[0])


def proc_starttime_ticks(pid: int, proc_root: Path = Path("/proc")) -> int | None:
    """starttime of ``pid`` (clock ticks since boot, stat field 22), or None.

    ``(pid, starttime)`` is a reuse-proof process identity: starttime never
    changes for a live process, so a matching pair proves the pid was not
    recycled. ``proc_root`` is for tests.
    """
    stat_path = proc_root / str(pid) / "stat"
    if not stat_path.exists():
        return None
    try:
        raw = stat_path.read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    # comm field (field 2) is parenthesised and may contain spaces / ')';
    # split after the LAST ')'. field 22 (start_time) is index 19 of the
    # remainder (fields 3..N).
    after_comm = raw[raw.rfind(")") + 2 :]
    parts = after_comm.split()
    if len(parts) < 20:
        return None
    try:
        return int(parts[19])
    except ValueError:
        return None


def _proc_age_secs(pid: int, uptime_secs: float, clock_ticks: int) -> float | None:
    """Age of ``pid`` in seconds from ``/proc/<pid>/stat`` field 22, or None."""
    start_ticks = proc_starttime_ticks(pid)
    if start_ticks is None:
        return None
    return uptime_secs - (start_ticks / clock_ticks)


def _marker_mtime(pid: int) -> float | None:
    """mtime (epoch secs) of the activity marker for ``pid``, or None."""
    marker = _MARKER_DIR / str(pid)
    try:
        return marker.stat().st_mtime
    except (FileNotFoundError, NotADirectoryError):
        return None


def _normalize_tty(raw: str) -> str | None:
    """Normalise a tty spec to ``pts/N`` / ``ttyN`` form, or None if not a tty."""
    t = raw.strip()
    if not t or t == "?":
        return None
    if t.startswith("/dev/"):
        t = t[len("/dev/") :]
    return t or None


async def _process_tty(pid: int) -> str | None:
    """Controlling terminal of ``pid`` (``pts/5``) via ``ps``, or None."""
    proc = await asyncio.create_subprocess_exec(
        "ps",
        "-o",
        "tty=",
        "-p",
        str(pid),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    return _normalize_tty(stdout.decode())


def _attached_pane_ttys(listing: str) -> set[str]:
    """Pane ttys of CLIENT-ATTACHED tmux sessions from ``list-panes -a -F
    '#{session_attached} #{pane_tty}'`` output. Pure — unit-tested directly.

    A malformed line is treated as attached: on ANY parse drift the reaper
    must fail toward sparing, never toward reaping. That covers both a
    non-integer first field and a tty-only line (e.g. output shaped like
    the pre-WS-D2 ``#{pane_tty}`` format).
    """
    ttys: set[str] = set()
    for line in listing.splitlines():
        fields = line.split(None, 1)
        if not fields:
            continue
        if len(fields) == 1:
            # Single-field line: format drift back to tty-only. Fail-spare —
            # count it as attached (adding junk can only ever spare).
            norm = _normalize_tty(fields[0])
            if norm:
                ttys.add(norm)
            continue
        attached_raw, tty_raw = fields
        try:
            attached = int(attached_raw)
        except ValueError:
            attached = 1  # fail-spare: unknown format counts as attached
        if attached < 1:
            continue
        norm = _normalize_tty(tty_raw)
        if norm:
            ttys.add(norm)
    return ttys


async def _live_ttys() -> set[str]:
    """Union of ATTACHED tmux pane ttys and utmp (``who``) login ttys.

    This is the live-terminal discriminator: a process whose controlling
    tty is in this set has a human plausibly looking at it (verified
    2026-07-11 — interactive sessions in-set, reparented zombies
    out-of-set). WS-D2 (2026-07-16): a tmux pane counts only while its
    session has an attached client — under the persistent cc-N slot model
    every interactive claude lives in tmux forever, so bare pane existence
    spared everything and idle detached slots could never be reaped. A
    detached slot mid-long-turn is still spared by its fresh activity
    marker (checked before the tty guard).
    """
    ttys: set[str] = set()
    # tmux panes of attached sessions only
    with contextlib.suppress(Exception):
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{session_attached} #{pane_tty}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        ttys |= _attached_pane_ttys(stdout.decode())
    # utmp login ttys
    with contextlib.suppress(Exception):
        proc = await asyncio.create_subprocess_exec(
            "who",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        for line in stdout.decode().splitlines():
            fields = line.split()
            if len(fields) >= 2:
                norm = _normalize_tty(fields[1])
                if norm:
                    ttys.add(norm)
    return ttys


async def _get_descendants(pid: int, depth: int = 0) -> list[int]:
    """Return all descendant PIDs (children-first / bottom-up)."""
    if depth >= 10:
        return []
    proc = await asyncio.create_subprocess_exec(
        "pgrep",
        "-P",
        str(pid),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if not stdout.strip():
        return []
    children = [int(p) for p in stdout.decode().split() if p.strip().isdigit()]
    result: list[int] = []
    for child in children:
        result.extend(await _get_descendants(child, depth + 1))
    result.extend(children)
    return result


# ── Persistent dry-run / arm state ──────────────────────────────────────
def _load_state() -> dict:
    try:
        state = json.loads(_STATE_PATH.read_text())
        return state if isinstance(state, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, NotADirectoryError):
        return {}


def _save_state(state: dict) -> None:
    with contextlib.suppress(OSError):
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(_STATE_PATH)


_ENV_AFFIRMATIVE = frozenset({"1", "true", "yes", "on"})


def _operator_armed(state: dict) -> bool:
    """Read a legacy arm request for diagnostics, never as kill authority."""
    if state.get(_STATE_ARMED_KEY):
        return True
    return os.environ.get(_ENV_ARM, "").strip().lower() in _ENV_AFFIRMATIVE


def set_operator_armed(armed: bool) -> None:
    """Clear a legacy arm flag; global discovery can no longer be armed."""
    if armed:
        raise ValueError("Global process discovery is observation-only; use owned job cancellation")
    state = _load_state()
    state.pop(_STATE_ARMED_KEY, None)
    _save_state(state)


def _gc_markers(live_pids: set[int]) -> None:
    """Delete activity markers for PIDs that are no longer alive."""
    with contextlib.suppress(FileNotFoundError, NotADirectoryError):
        for marker in _MARKER_DIR.iterdir():
            if not marker.name.isdigit():
                continue
            if int(marker.name) in live_pids:
                continue
            # A missing name match is not evidence of death (new CLI names,
            # permission failures and incomplete discovery must spare markers).
            try:
                Path(f"/proc/{marker.name}").stat()
            except FileNotFoundError:
                with contextlib.suppress(OSError):
                    marker.unlink()
            except OSError:
                continue


# ── Orchestrator ────────────────────────────────────────────────────────
async def run_reaper(rt: GenesisRuntime, *, now: float | None = None) -> None:
    """Record discovery hints only, regardless of legacy operator arm state."""
    from genesis.browser.types import BROWSER_PGREP_PATTERNS

    now = now if now is not None else datetime.now(UTC).timestamp()
    clock_ticks = os.sysconf("SC_CLK_TCK")

    hard_disabled = bool(os.environ.get(_ENV_HARD_DISABLE))
    state = _load_state()
    if _operator_armed(state) and not hard_disabled:
        logger.warning("Legacy process reaper arm ignored: global discovery is observation-only")

    my_pid = os.getpid()
    protected = {my_pid, os.getppid()}

    # (pgrep_flag, pattern, max_age_hours, label, is_claude)
    targets: list[tuple[str, str, int, str, bool]] = [
        ("-f", "opencode-ai", 24, "opencode-ai", False),
        ("-x", "claude", 168, "claude", True),
        ("-x", "codex", 168, "codex", True),
        ("-x", "opencode", 168, "opencode", True),
    ]
    for bp in BROWSER_PGREP_PATTERNS:
        targets.append(("-f", bp, 4, f"browser:{bp}", False))

    try:
        uptime_secs = _read_uptime()
        live_ttys = await _live_ttys()
        all_live_pids: set[int] = set()
        # candidates: (root_pid, label, reason, is_claude, tree)
        candidates: list[tuple[int, str, str, bool, list[int]]] = []

        for flag, pattern, max_age_h, label, is_claude in targets:
            pids = await _pgrep(flag, pattern)
            max_age = max_age_h * 3600
            for pid in pids:
                all_live_pids.add(pid)
                if pid <= 1 or pid in protected:
                    continue
                age = _proc_age_secs(pid, uptime_secs, clock_ticks)
                if age is None:
                    continue
                if is_claude:
                    tty = await _process_tty(pid)
                    marker = _marker_mtime(pid)
                    should_reap, reason = classify_claude_pid(
                        age_secs=age,
                        now=now,
                        marker_mtime=marker,
                        controlling_tty=tty,
                        live_ttys=live_ttys,
                    )
                else:
                    should_reap = age > max_age
                    reason = "stale" if should_reap else "young"
                if not should_reap:
                    continue
                tree = await _get_descendants(pid)
                tree.append(pid)
                candidates.append((pid, label, reason, is_claude, tree))

        _gc_markers(all_live_pids)

        if not candidates:
            rt.record_job_success("process_reaper")
            return

        for root, label, reason, _is_claude, tree in candidates:
            logger.warning(
                "Process discovery OBSERVE ONLY: pid %d (%s, reason=%s, tree=%s)",
                root, label, reason, tree,
            )
        await _record_observation(rt, candidates, dry_run=True)
        rt.record_job_success("process_reaper")
    except Exception as exc:  # noqa: BLE001 — job boundary
        rt.record_job_failure("process_reaper", exc=exc)
        logger.exception("Process reaper failed")


async def _record_observation(
    rt: GenesisRuntime,
    candidates: list[tuple[int, str, str, bool, list[int]]],
    *,
    dry_run: bool,
) -> None:
    if rt._db is None:
        return
    with contextlib.suppress(Exception):
        from uuid import uuid4

        from genesis.db.crud import observations

        await observations.create(
            rt._db,
            id=f"reaper-{uuid4().hex[:8]}",
            source="process_reaper",
            type="process_reaper_would_kill",  # legacy audit type; never a kill promise
            priority="low",
            content=json.dumps(
                {
                    "dry_run": True,
                    "enforcement": "observation-only",
                    "count": len(candidates),
                    "processes": [
                        {"pid": root, "label": label, "reason": reason}
                        for root, label, reason, _, _ in candidates
                    ],
                }
            ),
            created_at=datetime.now(UTC).isoformat(),
        )


def _wire_process_reaper(scheduler, rt) -> None:
    """Register the hourly process reaper on the learning scheduler.

    CronTrigger (not IntervalTrigger): IntervalTrigger resets on server
    restart, so the reaper would never fire if the server restarts within
    the hour. Runs at :15 past the hour to avoid hour-boundary collisions.
    """
    from apscheduler.triggers.cron import CronTrigger

    async def _reap_stale_processes() -> None:
        await run_reaper(rt)

    scheduler.add_job(
        _reap_stale_processes,
        CronTrigger(minute=15),
        id="process_reaper",
        max_instances=1,
        misfire_grace_time=600,
    )
